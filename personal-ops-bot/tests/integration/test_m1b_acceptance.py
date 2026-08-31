"""The M1B acceptance scenarios, as regression tests.

Each scenario in the milestone definition is one test here. Demonstrating them
once in a terminal proves they worked on the day; writing them down as tests is
what keeps them true through M1C, when tools start changing the runtime
underneath all of this.

They are deliberately written at the level the scenario is stated -- through the
runtime and the CLI, asserting on what was committed and what the owner saw --
rather than at the level of the units involved. A scenario that passed only
because its unit tests were mocked into agreement would be worth nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.reaper import sweep_orphaned_runs
from app.agent.repository import AgentRunRepository
from app.agent.runtime import AgentRuntime
from app.cli import _session, render
from app.core.agent_state import RunState, is_terminal
from app.core.clock import FrozenClock
from app.core.conversation import Role
from app.core.llm import LLMUnavailable
from app.domains.conversations import ConversationRepository, ConversationService
from app.observability.logging import configure_logging
from app.providers.llm.fake import FakeLLM
from tests.conftest import make_container

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AT = datetime(2026, 8, 19, 9, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def migrated_dsn(postgres_dsn: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": postgres_dsn, "APP_ENV": "dev"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return postgres_dsn


@pytest.fixture
async def sessions(migrated_dsn: str):
    engine = create_async_engine(migrated_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(AT)


@pytest.fixture
async def owner(sessions, clock):
    """A distinct owner per scenario, so scenarios cannot interfere."""
    async with sessions() as session, session.begin():
        repo = ConversationRepository(session, clock)
        user = await repo.create_user_with_identity(
            display_name="owner", provider="cli", external_id=f"acc-{uuid4()}"
        )
        convo = await ConversationService(session, clock).start_conversation(user_id=user.id)
        return user, convo


def make_runtime(sessions, clock, llm: FakeLLM, **kw) -> AgentRuntime:
    async def no_sleep(_seconds: float) -> None:
        return None

    return AgentRuntime(
        session_factory=sessions,
        llm=llm,
        clock=clock,
        model="fake-model-1",
        max_tokens=512,
        sleep=no_sleep,
        **kw,
    )


# ---------------------------------------------------------------------------
# Scenario 1 - Basic conversation
#   CLI -> user message -> persisted -> agent run -> provider -> assistant
#   response -> persisted -> displayed
# ---------------------------------------------------------------------------


async def test_scenario_1_a_message_becomes_a_persisted_answer(sessions, clock, owner) -> None:
    user, convo = owner
    llm = FakeLLM(script={"what is 2+2": "4."})

    result = await make_runtime(sessions, clock, llm).handle_user_message(
        conversation_id=convo.id, user_id=user.id, content="what is 2+2"
    )

    # displayed
    assert render(result) == "4."
    # the provider was actually asked
    assert llm.call_count == 1
    # every link in the chain landed
    async with sessions() as session:
        runs = AgentRunRepository(session, clock)
        run = await runs.get_run(result.run_id)
        calls = await runs.calls_for_run(result.run_id)
        history = await ConversationService(session, clock).history(convo.id, limit=10)

    assert run is not None and run.state == RunState.COMPLETE.value
    assert len(calls) == 1 and calls[0].ok
    assert [(m.role, m.content) for m in history] == [
        (Role.USER, "what is 2+2"),
        (Role.ASSISTANT, "4."),
    ]


# ---------------------------------------------------------------------------
# Scenario 2 - Resume
#   conversation -> exit -> restart -> resume -> previous messages available
# ---------------------------------------------------------------------------


async def test_scenario_2_a_conversation_resumes_in_a_new_process(
    sessions, clock, monkeypatch, capsys
) -> None:
    class _NoDisposeEngine:
        async def dispose(self) -> None:
            return None

    def feed(lines: list[str]) -> None:
        queue = list(lines)

        def fake_input(_p: str = "") -> str:
            if not queue:
                raise EOFError
            return queue.pop(0)

        monkeypatch.setattr("builtins.input", fake_input)

    def container(llm: FakeLLM):
        return make_container(
            session_factory=sessions,
            llm=llm,
            clock=clock,
            engine=_NoDisposeEngine(),
            runtime=make_runtime(sessions, clock, llm),
        )

    first = FakeLLM(default="Noted.")
    feed(["I live in Bangalore."])
    assert await _session(container(first), check_db=False, start_new=True) == 0
    capsys.readouterr()

    # A second process: new container, new runtime, new provider. Only the
    # database is shared.
    clock.advance(timedelta(minutes=10))
    second = FakeLLM(default="Bangalore.")
    feed(["Where do I live?"])
    assert await _session(container(second), check_db=False, start_new=False) == 0

    assert "(resumed)" in capsys.readouterr().out
    sent = second.last_request
    assert sent is not None
    assert "I live in Bangalore." in [m.content for m in sent.messages]


# ---------------------------------------------------------------------------
# Scenario 3 - FakeLLM
#   test -> FakeLLM -> deterministic response -> no API call
# ---------------------------------------------------------------------------


async def test_scenario_3_the_same_conversation_replays_identically(sessions, clock, owner) -> None:
    """Determinism is the claim, so the test runs it twice and compares.

    'No API call' is structural rather than asserted: FakeLLM has no network
    code and no credentials, and an import contract stops any vendor SDK
    reaching this layer at all.
    """
    user, _unused_conversation = owner  # each replay gets its own conversation
    digests = []

    for _ in range(2):
        async with sessions() as session, session.begin():
            fresh = await ConversationService(session, clock).start_conversation(user_id=user.id)
        llm = FakeLLM(script={"ping": "pong"})
        result = await make_runtime(sessions, clock, llm).handle_user_message(
            conversation_id=fresh.id, user_id=user.id, content="ping"
        )
        assert result.reply == "pong"
        async with sessions() as session:
            calls = await AgentRunRepository(session, clock).calls_for_run(result.run_id)
        digests.append(calls[0].prompt_digest)

    # Identical input produced an identical prompt, byte for byte.
    assert digests[0] == digests[1]


# ---------------------------------------------------------------------------
# Scenario 4 - Provider failure
#   unavailable -> run marked failed -> useful error -> no false success
# ---------------------------------------------------------------------------


async def test_scenario_4_an_outage_fails_loudly_and_invents_nothing(
    sessions, clock, owner
) -> None:
    user, convo = owner
    llm = FakeLLM(outcomes=[LLMUnavailable("connection refused")] * 10)

    result = await make_runtime(sessions, clock, llm).handle_user_message(
        conversation_id=convo.id, user_id=user.id, content="are you there?"
    )

    # marked failed
    assert result.state is RunState.FAILED and not result.ok
    # useful error, naming the problem and what survived
    shown = render(result)
    assert "unreachable" in shown and "beyond your message" in shown
    # no false success: no assistant message was written
    async with sessions() as session:
        history = await ConversationService(session, clock).history(convo.id, limit=10)
        run = await AgentRunRepository(session, clock).get_run(result.run_id)
    assert [m.role for m in history] == [Role.USER]
    assert run is not None and run.completed_at is not None  # terminal, not stuck


# ---------------------------------------------------------------------------
# Scenario 5 - Traceability
#   given a run_id: conversation, user message, LLM call, assistant response
# ---------------------------------------------------------------------------


async def test_scenario_5_a_run_id_reconstructs_the_whole_turn(
    sessions, clock, owner, capsys
) -> None:
    user, convo = owner
    configure_logging(level="INFO", json_output=True)
    capsys.readouterr()

    result = await make_runtime(sessions, clock, FakeLLM(default="the answer")).handle_user_message(
        conversation_id=convo.id, user_id=user.id, content="the question"
    )
    logs = [
        json.loads(line)
        for line in capsys.readouterr().err.strip().splitlines()
        if line.startswith("{")
    ]

    # One query, starting from nothing but the run id.
    async with sessions() as session:
        row = (
            await session.execute(
                text("""
                SELECT r.conversation_id, r.state, r.stop_reason,
                       trigger.content AS user_message,
                       reply.content   AS assistant_message,
                       l.provider, l.model, l.tokens_in, l.tokens_out, l.prompt_digest
                FROM agent_runs r
                JOIN messages trigger ON trigger.id = r.trigger_message_id
                JOIN messages reply   ON reply.created_by_run_id = r.id
                JOIN llm_calls l      ON l.run_id = r.id
                WHERE r.id = :run_id
                """),
                {"run_id": result.run_id},
            )
        ).one()

    assert row.conversation_id == convo.id
    assert row.user_message == "the question"
    assert row.assistant_message == "the answer"
    assert row.state == RunState.COMPLETE.value
    assert row.provider == "fake" and row.tokens_in and row.tokens_out
    assert len(row.prompt_digest) == 64

    # ...and the logs join to the same run.
    correlated = [line for line in logs if line.get("run_id") == str(result.run_id)]
    assert correlated, "no log line carried this run_id"
    assert all(line["conversation_id"] == str(convo.id) for line in correlated)


# ---------------------------------------------------------------------------
# Scenario 6 - Configuration safety
#   no unrelated component can access the provider secret
# ---------------------------------------------------------------------------


def test_scenario_6_the_secret_is_unwrapped_in_exactly_one_place() -> None:
    """`get_secret_value()` is greppable on purpose (app/config/settings.py).

    This asserts the grep: bootstrap is the composition root and the only place
    a credential is read out of Settings. A second call site would mean a
    component had started handling raw secrets, which is how one ends up in a
    log line or a prompt.
    """
    call_sites = subprocess.run(
        ["grep", "-rln", "--include=*.py", "get_secret_value", "app/"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    ).stdout.split()

    assert sorted(call_sites) == [
        "app/bootstrap.py",  # the composition root
        "app/config/settings.py",  # the definition itself (database_host)
    ]


def test_scenario_6_the_runtime_holds_no_settings_and_no_credentials(sessions, clock) -> None:
    """The Agent Runtime is constructed from primitives. If it held Settings it
    would hold every secret in the process, transitively."""
    from app.config.settings import Settings

    runtime = make_runtime(sessions, clock, FakeLLM())
    held = vars(runtime)
    assert not any(isinstance(v, Settings) for v in held.values())
    assert not any("key" in name.lower() or "secret" in name.lower() for name in held)


def test_scenario_6_a_model_request_cannot_carry_a_secret() -> None:
    """The object that crosses the provider boundary has a closed field set."""
    import dataclasses

    from app.core.llm import ModelRequest

    assert {f.name for f in dataclasses.fields(ModelRequest)} == {
        "messages",
        "model",
        "max_tokens",
        "system",
        "temperature",
    }


# ---------------------------------------------------------------------------
# Scenario 7 - Restart safety
#   a process restart must not corrupt persisted conversation state
# ---------------------------------------------------------------------------


async def test_scenario_7_a_crash_after_the_model_replied_leaves_no_partial_turn(
    sessions, clock, owner, monkeypatch
) -> None:
    """The hardest case: the model answered, and we died before recording it.

    Commit boundary C says llm_calls, the assistant message and COMPLETE land
    together or not at all. This kills the process (as an exception) inside that
    transaction and checks nothing partial survived.
    """
    user, convo = owner
    llm = FakeLLM(default="an answer nobody will see")

    async def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("process died mid-commit")

    monkeypatch.setattr(ConversationService, "record_assistant_message", explode)

    with pytest.raises(RuntimeError):
        await make_runtime(sessions, clock, llm).handle_user_message(
            conversation_id=convo.id, user_id=user.id, content="a question"
        )

    async with sessions() as session:
        history = await ConversationService(session, clock).history(convo.id, limit=10)
        stranded = (
            await session.execute(
                text("SELECT id, state FROM agent_runs WHERE conversation_id = :c"),
                {"c": convo.id},
            )
        ).all()

    # The owner's message survived (commit boundary A).
    assert [(m.role, m.content) for m in history] == [(Role.USER, "a question")]
    # No assistant message was written, and the run did not reach COMPLETE.
    assert len(stranded) == 1
    assert not is_terminal(RunState(stranded[0].state))


async def test_scenario_7_the_reaper_makes_a_crashed_turn_consistent(
    sessions, clock, owner, monkeypatch
) -> None:
    """And the state left behind is recoverable rather than permanent."""
    user, convo = owner

    async def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("process died mid-commit")

    monkeypatch.setattr(ConversationService, "record_assistant_message", explode)
    with pytest.raises(RuntimeError):
        await make_runtime(sessions, clock, FakeLLM()).handle_user_message(
            conversation_id=convo.id, user_id=user.id, content="a question"
        )
    monkeypatch.undo()

    clock.advance(timedelta(seconds=600))
    await sweep_orphaned_runs(session_factory=sessions, clock=clock, older_than_s=300)

    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT state, stop_reason, completed_at FROM agent_runs "
                    "WHERE conversation_id = :c"
                ),
                {"c": convo.id},
            )
        ).all()

    assert len(rows) == 1
    assert rows[0].state == RunState.FAILED.value
    assert rows[0].stop_reason == "orphaned"
    assert rows[0].completed_at is not None


async def test_scenario_7_the_conversation_is_usable_again_after_a_crash(
    sessions, clock, owner, monkeypatch
) -> None:
    """The real requirement: a crashed turn must not poison the conversation."""
    user, convo = owner

    async def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("process died mid-commit")

    monkeypatch.setattr(ConversationService, "record_assistant_message", explode)
    with pytest.raises(RuntimeError):
        await make_runtime(sessions, clock, FakeLLM()).handle_user_message(
            conversation_id=convo.id, user_id=user.id, content="lost turn"
        )
    monkeypatch.undo()

    # A fresh process picks the conversation straight back up.
    clock.advance(timedelta(minutes=1))
    llm = FakeLLM(default="still working")
    result = await make_runtime(sessions, clock, llm).handle_user_message(
        conversation_id=convo.id, user_id=user.id, content="are you ok?"
    )

    assert result.ok and result.reply == "still working"
    # The interrupted turn's message is still in history, in order.
    sent = llm.last_request
    assert sent is not None
    assert [m.content for m in sent.messages] == ["lost turn", "are you ok?"]
