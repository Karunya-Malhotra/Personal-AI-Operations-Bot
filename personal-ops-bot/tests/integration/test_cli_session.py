"""The CLI driving real turns against a real database.

This is where §20 is actually verified: a conversation survives the process that
created it. The test runs two *separate* container instances against the same
database, which is as close to "exit the CLI and start it again" as a test can
get without spawning a subprocess -- and unlike a subprocess it can assert on
what the second session's prompt contained, which is the part that matters.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.runtime import AgentRuntime
from app.cli import _session
from app.core.clock import FrozenClock
from app.core.conversation import Role
from app.domains.conversations import ConversationService
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


def container_for(sessions, llm: FakeLLM, clock: FrozenClock):
    """A container wired like `build_container` does, minus the real engine.

    `shutdown()` disposes the engine, and these tests share one across both
    "processes", so the container gets a stand-in whose dispose is a no-op.
    """

    class _NoDisposeEngine:
        async def dispose(self) -> None:
            return None

    return make_container(
        session_factory=sessions,
        llm=llm,
        clock=clock,
        engine=_NoDisposeEngine(),
        runtime=AgentRuntime(
            session_factory=sessions,
            llm=llm,
            clock=clock,
            model="fake-model-1",
            max_tokens=256,
        ),
    )


def feed(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    """Stand in for a person typing, ending with EOF like Ctrl-D."""
    queue = list(lines)

    def fake_input(_prompt: str = "") -> str:
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


async def test_a_conversation_survives_the_process_that_created_it(
    sessions, monkeypatch, capsys
) -> None:
    """§20, the scenario that makes the persistence visible rather than claimed."""
    clock = FrozenClock(AT)
    first_llm = FakeLLM(default="Noted.")

    feed(monkeypatch, ["My favourite programming language is Python."])
    assert (
        await _session(container_for(sessions, first_llm, clock), check_db=False, start_new=True)
        == 0
    )
    capsys.readouterr()

    # A second, independent session -- new container, new runtime, new provider
    # instance. Only the database is shared, which is the point.
    clock.advance(timedelta(minutes=5))
    second_llm = FakeLLM(default="Python.")
    feed(monkeypatch, ["What is my favourite programming language?"])
    assert (
        await _session(container_for(sessions, second_llm, clock), check_db=False, start_new=False)
        == 0
    )

    out = capsys.readouterr().out
    assert "(resumed)" in out
    assert "Python." in out

    # The claim that matters: the earlier turn reached the second prompt. The
    # scripted reply proves nothing on its own -- what the provider *received*
    # does.
    sent = second_llm.last_request
    assert sent is not None
    contents = [m.content for m in sent.messages]
    assert "My favourite programming language is Python." in contents
    assert contents[-1] == "What is my favourite programming language?"
    assert [m.role for m in sent.messages] == [Role.USER, Role.ASSISTANT, Role.USER]


async def test_new_starts_a_separate_conversation(sessions, monkeypatch, capsys) -> None:
    clock = FrozenClock(AT)
    llm = FakeLLM(default="ok")

    feed(monkeypatch, ["remember this", "/new", "fresh start"])
    await _session(container_for(sessions, llm, clock), check_db=False, start_new=True)

    out = capsys.readouterr().out
    assert "started conversation" in out

    # The turn after /new must not carry the previous conversation's history.
    sent = llm.last_request
    assert sent is not None
    assert [m.content for m in sent.messages] == ["fresh start"]


async def test_a_provider_outage_is_reported_not_hidden(sessions, monkeypatch, capsys) -> None:
    """§26 Scenario 4, end to end: the owner is told, and nothing is invented."""
    from app.core.llm import LLMUnavailable

    clock = FrozenClock(AT)
    llm = FakeLLM(outcomes=[LLMUnavailable("503")] * 10)

    feed(monkeypatch, ["are you there?"])
    assert await _session(container_for(sessions, llm, clock), check_db=False, start_new=True) == 0

    out = capsys.readouterr().out
    assert "no answer" in out
    assert "unreachable" in out
    assert "beyond your message" in out


async def test_the_owners_message_survives_a_failed_turn(sessions, monkeypatch, capsys) -> None:
    from app.core.llm import LLMUnavailable

    clock = FrozenClock(AT)
    llm = FakeLLM(outcomes=[LLMUnavailable("503")] * 10)
    container = container_for(sessions, llm, clock)

    feed(monkeypatch, ["this must not be lost"])
    await _session(container, check_db=False, start_new=True)
    capsys.readouterr()

    # Locate the conversation by the message itself rather than by resuming
    # "the latest": every test in this module shares one owner (the CLI's
    # ensure_local_owner is idempotent by design), so "latest" depends on which
    # other tests ran and how far each advanced its clock.
    async with sessions() as session:
        conversation_id = (
            await session.execute(
                text("SELECT conversation_id FROM messages WHERE content = :c"),
                {"c": "this must not be lost"},
            )
        ).scalar_one()
        history = await ConversationService(session, clock).history(conversation_id, limit=10)

    # The message is kept; no assistant reply was invented for a failed turn.
    assert [(m.role, m.content) for m in history] == [(Role.USER, "this must not be lost")]


async def test_slash_commands_do_not_reach_the_model(sessions, monkeypatch, capsys) -> None:
    """/help is interface, not conversation. Sending it to the model would put
    it in the transcript and cost a turn."""
    clock = FrozenClock(AT)
    llm = FakeLLM(default="ok")

    feed(monkeypatch, ["/help", "/quit"])
    await _session(container_for(sessions, llm, clock), check_db=False, start_new=True)

    assert llm.call_count == 0
    assert "/new" in capsys.readouterr().out


async def test_blank_lines_are_ignored(sessions, monkeypatch, capsys) -> None:
    clock = FrozenClock(AT)
    llm = FakeLLM(default="ok")

    feed(monkeypatch, ["", "   ", "actually asking"])
    await _session(container_for(sessions, llm, clock), check_db=False, start_new=True)

    assert llm.call_count == 1
