"""The Agent Runtime, end to end against a real PostgreSQL and a scripted model.

Integration rather than unit tests because almost everything asserted here is a
property of what got *committed*: which state a run ended in, that a failed
attempt still produced an `llm_calls` row, that a reply and its run land
together. A mocked session would be asserting that the code called the methods
the test expected, which is a restatement of the implementation.

The model is `FakeLLM` throughout -- deterministic, free, and able to fail on
demand, which is the only way to test the paths §15 asks about.
"""

from __future__ import annotations

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
from app.core.agent_state import RunState
from app.core.clock import FrozenClock
from app.core.conversation import Role
from app.core.llm import (
    LLMAuthError,
    LLMMalformedResponse,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
)
from app.domains.conversations import ConversationRepository, ConversationService
from app.providers.llm.fake import FakeLLM

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
def clock() -> FrozenClock:
    return FrozenClock(AT)


@pytest.fixture
async def sessions(migrated_dsn: str):
    engine = create_async_engine(migrated_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def conversation(sessions, clock):
    """A fresh owner and conversation per test."""
    async with sessions() as session, session.begin():
        repo = ConversationRepository(session, clock)
        user = await repo.create_user_with_identity(
            display_name="owner", provider="cli", external_id=f"t-{uuid4()}"
        )
        convo = await ConversationService(session, clock).start_conversation(user_id=user.id)
        return user, convo


def runtime(sessions, clock, llm: FakeLLM, **kw) -> AgentRuntime:
    slept: list[float] = []

    async def no_sleep(seconds: float) -> None:
        slept.append(seconds)

    built = AgentRuntime(
        session_factory=sessions,
        llm=llm,
        clock=clock,
        model="fake-model-1",
        max_tokens=512,
        sleep=no_sleep,
        **kw,
    )
    built.slept = slept  # type: ignore[attr-defined]
    return built


# -- the happy path ---------------------------------------------------------


async def test_a_successful_turn_completes_and_persists_everything(
    sessions, clock, conversation
) -> None:
    user, convo = conversation
    llm = FakeLLM(script={"hello": "Hi there."})
    result = await runtime(sessions, clock, llm).handle_user_message(
        conversation_id=convo.id, user_id=user.id, content="hello"
    )

    assert result.ok
    assert result.state is RunState.COMPLETE
    assert result.reply == "Hi there."

    async with sessions() as session:
        run = await AgentRunRepository(session, clock).get_run(result.run_id)
        assert run is not None
        assert run.state == RunState.COMPLETE.value
        assert run.completed_at is not None
        assert run.stop_reason == "end_turn"

        history = await ConversationService(session, clock).history(convo.id, limit=10)

    # Both turns are stored, in order, and the reply is attributed to the run.
    assert [(m.role, m.content) for m in history] == [
        (Role.USER, "hello"),
        (Role.ASSISTANT, "Hi there."),
    ]


async def test_the_assistant_message_points_back_at_its_run(sessions, clock, conversation) -> None:
    """§26 Scenario 5: from an answer you can reach its whole trace."""
    user, convo = conversation
    result = await runtime(sessions, clock, FakeLLM()).handle_user_message(
        conversation_id=convo.id, user_id=user.id, content="hi"
    )

    async with sessions() as session:
        row = await session.execute(
            text(
                "SELECT created_by_run_id FROM messages "
                "WHERE conversation_id = :c AND role = 'assistant'"
            ),
            {"c": convo.id},
        )
        assert row.scalar_one() == result.run_id


async def test_one_llm_call_row_is_written_per_attempt(sessions, clock, conversation) -> None:
    user, convo = conversation
    result = await runtime(sessions, clock, FakeLLM()).handle_user_message(
        conversation_id=convo.id, user_id=user.id, content="hi"
    )

    async with sessions() as session:
        calls = await AgentRunRepository(session, clock).calls_for_run(result.run_id)

    assert len(calls) == 1
    call = calls[0]
    assert call.ok is True
    assert call.step_no == 1
    assert call.provider == "fake"
    assert call.tokens_in and call.tokens_out
    assert len(call.prompt_digest) == 64  # sha256 hex
    assert call.context_summary["messages_included"] == 1


# -- conversation memory ----------------------------------------------------


async def test_history_is_replayed_so_the_model_can_answer_from_it(
    sessions, clock, conversation
) -> None:
    """The §20 scenario, minus the process restart (covered separately).

    The assertion is on what the *provider received*, not on what the model
    said -- with a scripted model those are different claims, and only the
    first one is about our code.
    """
    user, convo = conversation
    llm = FakeLLM()
    agent = runtime(sessions, clock, llm)

    await agent.handle_user_message(
        conversation_id=convo.id,
        user_id=user.id,
        content="My favourite programming language is Python.",
    )
    clock.advance(timedelta(seconds=30))
    await agent.handle_user_message(
        conversation_id=convo.id, user_id=user.id, content="What is my favourite?"
    )

    sent = llm.last_request
    assert sent is not None
    contents = [m.content for m in sent.messages]
    assert "My favourite programming language is Python." in contents
    assert contents[-1] == "What is my favourite?"
    # The transcript alternates and ends on the user's turn.
    assert [m.role for m in sent.messages][-2:] == [Role.ASSISTANT, Role.USER]


async def test_the_system_prompt_never_enters_the_transcript(sessions, clock, conversation) -> None:
    user, convo = conversation
    llm = FakeLLM()
    await runtime(sessions, clock, llm).handle_user_message(
        conversation_id=convo.id, user_id=user.id, content="hi"
    )

    sent = llm.last_request
    assert sent is not None and sent.system
    assert all(m.role is not Role.SYSTEM for m in sent.messages)
    assert all(sent.system != m.content for m in sent.messages)


async def test_history_beyond_the_window_is_dropped_and_the_trace_says_so(
    sessions, clock, conversation
) -> None:
    """§22: bounded context, with an observable rule rather than silent loss."""
    user, convo = conversation
    llm = FakeLLM()
    agent = runtime(sessions, clock, llm, window_messages=4)

    for i in range(5):
        clock.advance(timedelta(seconds=10))
        await agent.handle_user_message(
            conversation_id=convo.id, user_id=user.id, content=f"turn {i}"
        )

    sent = llm.last_request
    assert sent is not None
    assert len(sent.messages) == 4  # the window, not the whole conversation

    async with sessions() as session:
        runs = AgentRunRepository(session, clock)
        result_calls = await runs.calls_for_run(
            (
                await session.execute(
                    text(
                        "SELECT id FROM agent_runs WHERE conversation_id = :c "
                        "ORDER BY started_at DESC LIMIT 1"
                    ),
                    {"c": convo.id},
                )
            ).scalar_one()
        )
    summary = result_calls[0].context_summary
    assert summary["messages_included"] == 4
    assert summary["messages_dropped"] > 0
    assert summary["messages_total"] == 9  # 5 user + 4 assistant, before this reply


# -- failure paths ----------------------------------------------------------


async def test_a_provider_outage_fails_the_run_rather_than_answering_emptily(
    sessions, clock, conversation
) -> None:
    """§26 Scenario 4, and the §15A rule: never a false success."""
    user, convo = conversation
    llm = FakeLLM(outcomes=[LLMUnavailable("503")] * 5)
    result = await runtime(sessions, clock, llm).handle_user_message(
        conversation_id=convo.id, user_id=user.id, content="hi"
    )

    assert not result.ok
    assert result.state is RunState.FAILED
    assert result.reply is None
    assert result.failure_kind == "LLMUnavailable"

    async with sessions() as session:
        run = await AgentRunRepository(session, clock).get_run(result.run_id)
        assert run is not None and run.state == RunState.FAILED.value
        assert run.completed_at is not None
        history = await ConversationService(session, clock).history(convo.id, limit=10)

    # The owner's message survived; no assistant message was invented.
    assert [m.role for m in history] == [Role.USER]


async def test_a_retryable_failure_is_retried_then_succeeds(sessions, clock, conversation) -> None:
    user, convo = conversation
    llm = FakeLLM(outcomes=[LLMUnavailable("503"), "recovered"])
    agent = runtime(sessions, clock, llm)
    result = await agent.handle_user_message(
        conversation_id=convo.id, user_id=user.id, content="hi"
    )

    assert result.ok and result.reply == "recovered"
    assert llm.call_count == 2

    async with sessions() as session:
        calls = await AgentRunRepository(session, clock).calls_for_run(result.run_id)

    # Both attempts are in the trace -- a turn that needed a retry is a
    # different event from one that did not, and only this shows it.
    assert [(c.step_no, c.ok, c.error_kind) for c in calls] == [
        (1, False, "LLMUnavailable"),
        (2, True, None),
    ]


async def test_a_non_retryable_failure_is_not_retried(sessions, clock, conversation) -> None:
    """A bad key will still be a bad key on the second attempt; retrying it
    just spends the turn's budget to fail identically."""
    user, convo = conversation
    llm = FakeLLM(outcomes=[LLMAuthError("bad key"), "never reached"])
    result = await runtime(sessions, clock, llm).handle_user_message(
        conversation_id=convo.id, user_id=user.id, content="hi"
    )

    assert result.state is RunState.FAILED
    assert llm.call_count == 1


async def test_exhausted_retries_fail_after_the_configured_attempts(
    sessions, clock, conversation
) -> None:
    user, convo = conversation
    llm = FakeLLM(outcomes=[LLMUnavailable("503")] * 10)
    agent = runtime(sessions, clock, llm, max_attempts=4)
    result = await agent.handle_user_message(
        conversation_id=convo.id, user_id=user.id, content="hi"
    )

    assert result.state is RunState.FAILED
    assert llm.call_count == 4
    assert agent.slept == [1.0, 2.0, 4.0]  # exponential, 3 waits for 4 attempts


async def test_a_rate_limit_hint_overrides_the_backoff(sessions, clock, conversation) -> None:
    """Retrying sooner than a 429 asked is how a rate limit becomes a longer one."""
    user, convo = conversation
    llm = FakeLLM(outcomes=[LLMRateLimited("slow down", retry_after_s=9.5), "ok now"])
    agent = runtime(sessions, clock, llm)
    await agent.handle_user_message(conversation_id=convo.id, user_id=user.id, content="hi")
    assert agent.slept == [9.5]


async def test_a_timeout_lands_in_timed_out_not_failed(sessions, clock, conversation) -> None:
    """The state machine distinguishes them, so the trace should too."""
    user, convo = conversation
    llm = FakeLLM(outcomes=[LLMTimeout("deadline exceeded")] * 5)
    result = await runtime(sessions, clock, llm).handle_user_message(
        conversation_id=convo.id, user_id=user.id, content="hi"
    )

    assert result.state is RunState.TIMED_OUT
    async with sessions() as session:
        run = await AgentRunRepository(session, clock).get_run(result.run_id)
    assert run is not None and run.state == RunState.TIMED_OUT.value


async def test_a_malformed_response_is_a_failure_not_a_silent_empty_reply(
    sessions, clock, conversation
) -> None:
    user, convo = conversation
    llm = FakeLLM(outcomes=[LLMMalformedResponse("no content blocks")] * 3)
    result = await runtime(sessions, clock, llm).handle_user_message(
        conversation_id=convo.id, user_id=user.id, content="hi"
    )
    assert result.state is RunState.FAILED
    assert result.reply is None


# -- the reaper -------------------------------------------------------------
#
# The reaper is deliberately global: it sweeps every stale run in the database,
# because a crash does not respect test boundaries either. That makes these
# tests interfere unless the table is cleared first -- a run another test left
# in CONTEXT_BUILDING becomes sweepable as soon as this one advances the clock.
# Isolating here rather than weakening the assertions to `>= 1`, since the exact
# count is the interesting part.


@pytest.fixture
async def only_my_runs(sessions):
    """Clear the run tables so a sweep count means what the test says it does."""
    async with sessions() as session, session.begin():
        await session.execute(text("UPDATE messages SET created_by_run_id = NULL"))
        await session.execute(text("DELETE FROM llm_calls"))
        await session.execute(text("DELETE FROM agent_runs"))
    return None


async def test_the_reaper_closes_runs_a_crash_left_in_flight(
    sessions, clock, conversation, only_my_runs
) -> None:
    """The failure mode the runtime's no-transaction-across-the-model-call
    choice creates, and the sweep that pays for it (§5.3)."""
    user, convo = conversation
    async with sessions() as session, session.begin():
        runs = AgentRunRepository(session, clock)
        run = await runs.create_run(
            conversation_id=convo.id, user_id=user.id, trigger_message_id=None
        )
        await runs.transition(run, RunState.CONTEXT_BUILDING)
        await runs.transition(run, RunState.MODEL_CALLING)
        stranded = run.id

    clock.advance(timedelta(seconds=600))
    swept = await sweep_orphaned_runs(session_factory=sessions, clock=clock, older_than_s=300)
    assert swept == 1

    async with sessions() as session:
        run = await AgentRunRepository(session, clock).get_run(stranded)
    assert run is not None
    assert run.state == RunState.FAILED.value
    assert run.stop_reason == "orphaned"
    assert run.completed_at is not None


async def test_the_reaper_leaves_recent_runs_alone(
    sessions, clock, conversation, only_my_runs
) -> None:
    """A run that is merely slow is not a run that died."""
    user, convo = conversation
    async with sessions() as session, session.begin():
        runs = AgentRunRepository(session, clock)
        run = await runs.create_run(
            conversation_id=convo.id, user_id=user.id, trigger_message_id=None
        )
        await runs.transition(run, RunState.CONTEXT_BUILDING)
        live = run.id

    clock.advance(timedelta(seconds=10))
    assert await sweep_orphaned_runs(session_factory=sessions, clock=clock, older_than_s=300) == 0

    async with sessions() as session:
        run = await AgentRunRepository(session, clock).get_run(live)
    assert run is not None and run.state == RunState.CONTEXT_BUILDING.value


async def test_the_reaper_can_close_a_run_stranded_in_its_very_first_state(
    sessions, clock, conversation, only_my_runs
) -> None:
    """A process can die before doing anything. Without a RECEIVED -> FAILED
    edge such a run could never be closed and would sit in the reaper's index
    forever -- which is why that edge exists."""
    user, convo = conversation
    async with sessions() as session, session.begin():
        run = await AgentRunRepository(session, clock).create_run(
            conversation_id=convo.id, user_id=user.id, trigger_message_id=None
        )
        stranded = run.id

    clock.advance(timedelta(seconds=600))
    assert await sweep_orphaned_runs(session_factory=sessions, clock=clock, older_than_s=300) == 1

    async with sessions() as session:
        run = await AgentRunRepository(session, clock).get_run(stranded)
    assert run is not None and run.state == RunState.FAILED.value


async def test_the_reaper_does_not_touch_completed_runs(
    sessions, clock, conversation, only_my_runs
) -> None:
    user, convo = conversation
    result = await runtime(sessions, clock, FakeLLM()).handle_user_message(
        conversation_id=convo.id, user_id=user.id, content="hi"
    )
    clock.advance(timedelta(seconds=600))
    await sweep_orphaned_runs(session_factory=sessions, clock=clock, older_than_s=300)

    async with sessions() as session:
        run = await AgentRunRepository(session, clock).get_run(result.run_id)
    assert run is not None and run.state == RunState.COMPLETE.value
    assert run.stop_reason == "end_turn"  # not overwritten with 'orphaned'


# -- observability and invariants -------------------------------------------


async def test_correlation_ids_reach_every_log_line_of_the_turn(
    sessions, clock, conversation, capsys
) -> None:
    """§14: `run_id` and `conversation_id` are bound once, at the top of the
    turn, and every line emitted below carries them -- including ones from
    modules that know nothing about runs. That is what makes a trace joinable
    to its logs."""
    import json

    from app.observability.logging import configure_logging

    user, convo = conversation
    configure_logging(level="INFO", json_output=True)
    capsys.readouterr()  # discard anything logged during setup

    result = await runtime(sessions, clock, FakeLLM()).handle_user_message(
        conversation_id=convo.id, user_id=user.id, content="hi"
    )

    lines = [
        json.loads(line)
        for line in capsys.readouterr().err.strip().splitlines()
        if line.startswith("{")
    ]
    run_lines = [line for line in lines if line.get("event", "").startswith("run.")]
    assert run_lines, "the runtime emitted no correlated log lines"
    for line in run_lines:
        assert line["run_id"] == str(result.run_id)
        assert line["conversation_id"] == str(convo.id)


async def test_correlation_ids_do_not_leak_past_the_turn(
    sessions, clock, conversation, capsys
) -> None:
    """Unbinding matters: a run_id left bound would silently mislabel every
    later line in the process as belonging to a finished turn."""
    import json

    from app.observability.logging import configure_logging, get_logger

    user, convo = conversation
    configure_logging(level="INFO", json_output=True)
    await runtime(sessions, clock, FakeLLM()).handle_user_message(
        conversation_id=convo.id, user_id=user.id, content="hi"
    )
    capsys.readouterr()

    get_logger("after").info("unrelated.event")
    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert "run_id" not in payload
    assert "conversation_id" not in payload


async def test_an_illegal_transition_is_refused_at_the_write_path(
    sessions, clock, conversation
) -> None:
    """The state machine's guarantee, enforced where it matters: an illegal
    state must never reach the database, or the trace stops being trustworthy."""
    from app.core.agent_state import IllegalTransitionError

    user, convo = conversation
    async with sessions() as session, session.begin():
        runs = AgentRunRepository(session, clock)
        run = await runs.create_run(
            conversation_id=convo.id, user_id=user.id, trigger_message_id=None
        )
        with pytest.raises(IllegalTransitionError):
            await runs.transition(run, RunState.COMPLETE)  # skips all the work


async def test_a_completed_run_cannot_be_moved_again(sessions, clock, conversation) -> None:
    user, convo = conversation
    result = await runtime(sessions, clock, FakeLLM()).handle_user_message(
        conversation_id=convo.id, user_id=user.id, content="hi"
    )
    from app.core.agent_state import IllegalTransitionError

    async with sessions() as session, session.begin():
        runs = AgentRunRepository(session, clock)
        run = await runs.get_run(result.run_id)
        assert run is not None
        with pytest.raises(IllegalTransitionError):
            await runs.transition(run, RunState.MODEL_CALLING)


async def test_cost_is_recorded_and_rolled_up_onto_the_run(sessions, clock, conversation) -> None:
    """FakeLLM reports a model with no rate in the table, so this also pins the
    honest-unknown behaviour: usage is recorded, cost stays 0 rather than being
    guessed."""
    user, convo = conversation
    result = await runtime(sessions, clock, FakeLLM()).handle_user_message(
        conversation_id=convo.id, user_id=user.id, content="hi"
    )

    async with sessions() as session:
        runs = AgentRunRepository(session, clock)
        run = await runs.get_run(result.run_id)
        calls = await runs.calls_for_run(result.run_id)

    assert run is not None
    assert calls[0].tokens_in and calls[0].tokens_out  # usage is real
    assert calls[0].cost_micros == 0  # rate unknown, not invented
    assert run.total_cost_micros == 0
