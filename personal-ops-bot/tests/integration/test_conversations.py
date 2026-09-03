"""Conversation persistence and lifecycle, against a real PostgreSQL.

These are integration tests rather than unit tests with a mocked session
because every property that matters here -- ordering, the idempotency
constraint, soft-delete exclusion -- is decided by SQL. A mocked session would
be asserting that the code calls the methods the test expects, which is a
restatement of the implementation rather than a check on its behaviour.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.clock import FrozenClock
from app.core.conversation import Role
from app.domains.conversations import ConversationRepository, ConversationService
from app.domains.conversations.service import DuplicateMessage

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    return FrozenClock(datetime(2026, 8, 19, 9, 0, 0, tzinfo=UTC))


async def _make_user(session, clock: FrozenClock):
    """A distinct owner per test.

    Not `ensure_local_owner`, which is idempotent by design and so would hand
    every test the same user and the same conversation list. Tests that shared
    an owner would pass or fail depending on execution order.
    """
    repo = ConversationRepository(session, clock)
    return await repo.create_user_with_identity(
        display_name="test-owner",
        provider="cli",
        external_id=f"test-{uuid4()}",
    )


@pytest.fixture
async def session_factory(migrated_dsn: str):
    engine = create_async_engine(migrated_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def owner(session_factory, clock):
    """A fresh owner + conversation per test, so tests cannot interfere."""
    async with session_factory() as session, session.begin():
        service = ConversationService(session, clock)
        user = await _make_user(session, clock)
        conversation = await service.start_conversation(user_id=user.id)
        return user, conversation


async def test_create_and_retrieve_a_conversation(session_factory, clock, owner) -> None:
    user, conversation = owner
    async with session_factory() as session:
        repo = ConversationRepository(session, clock)
        loaded = await repo.get_conversation(conversation.id)
    assert loaded is not None
    assert loaded.user_id == user.id
    assert loaded.closed_at is None


async def test_messages_are_persisted_and_returned_in_order(session_factory, clock, owner) -> None:
    """Ordering is the whole point: a transcript replayed out of order is a
    different conversation."""
    user, conversation = owner
    async with session_factory() as session, session.begin():
        service = ConversationService(session, clock)
        for text in ["first", "second", "third"]:
            await service.record_inbound_message(
                conversation_id=conversation.id, user_id=user.id, content=text
            )

    async with session_factory() as session:
        service = ConversationService(session, clock)
        history = await service.history(conversation.id, limit=50)

    assert [m.content for m in history] == ["first", "second", "third"]
    assert all(m.role is Role.USER for m in history)


async def test_ordering_survives_identical_timestamps(session_factory, clock, owner) -> None:
    """The FrozenClock gives every row the same `sent_at`.

    This is the case that fails without the monotonic UUIDv7 counter: ordering
    would fall back to random bits and the transcript would shuffle.
    """
    user, conversation = owner
    async with session_factory() as session, session.begin():
        service = ConversationService(session, clock)
        for i in range(25):
            await service.record_inbound_message(
                conversation_id=conversation.id, user_id=user.id, content=f"msg-{i:02d}"
            )

    async with session_factory() as session:
        service = ConversationService(session, clock)
        history = await service.history(conversation.id, limit=50)

    assert [m.content for m in history] == [f"msg-{i:02d}" for i in range(25)]
    assert len({m.sent_at for m in history}) == 1  # all identical, as intended


async def test_history_returns_the_newest_window_oldest_first(
    session_factory, clock, owner
) -> None:
    user, conversation = owner
    async with session_factory() as session, session.begin():
        service = ConversationService(session, clock)
        for i in range(10):
            await service.record_inbound_message(
                conversation_id=conversation.id, user_id=user.id, content=f"m{i}"
            )

    async with session_factory() as session:
        service = ConversationService(session, clock)
        history = await service.history(conversation.id, limit=3)

    assert [m.content for m in history] == ["m7", "m8", "m9"]


async def test_assistant_messages_carry_their_run(session_factory, clock, owner) -> None:
    """`created_by_run_id` is what makes answer -> trace a single join."""
    user, conversation = owner
    run_id = uuid4()
    async with session_factory() as session, session.begin():
        service = ConversationService(session, clock)
        message = await service.record_assistant_message(
            conversation_id=conversation.id,
            user_id=user.id,
            content="hello there",
            run_id=run_id,
        )
        assert message.created_by_run_id == run_id
        assert message.role == Role.ASSISTANT.value


async def test_duplicate_inbound_message_is_rejected_not_duplicated(
    session_factory, clock, owner
) -> None:
    """The M1E redelivery case, protected today by the database (§16)."""
    user, conversation = owner
    async with session_factory() as session, session.begin():
        service = ConversationService(session, clock)
        await service.record_inbound_message(
            conversation_id=conversation.id,
            user_id=user.id,
            content="hello",
            provider_message_id="tg-1",
        )

    async with session_factory() as session, session.begin():
        service = ConversationService(session, clock)
        with pytest.raises(DuplicateMessage):
            await service.record_inbound_message(
                conversation_id=conversation.id,
                user_id=user.id,
                content="hello",
                provider_message_id="tg-1",
            )

    async with session_factory() as session:
        repo = ConversationRepository(session, clock)
        assert await repo.count_messages(conversation.id) == 1


async def test_resume_returns_the_conversation_with_the_latest_message(
    session_factory, clock
) -> None:
    """What `aiops chat` does on start: continue where you left off."""
    async with session_factory() as session, session.begin():
        service = ConversationService(session, clock)
        user = await _make_user(session, clock)
        older = await service.start_conversation(user_id=user.id)
        newer = await service.start_conversation(user_id=user.id)
        await service.record_inbound_message(
            conversation_id=older.id, user_id=user.id, content="in the older one"
        )
        clock.advance(timedelta(minutes=5))
        await service.record_inbound_message(
            conversation_id=newer.id, user_id=user.id, content="in the newer one"
        )

    async with session_factory() as session:
        service = ConversationService(session, clock)
        resumed, was_resumed = await service.resume_or_start(user_id=user.id)

    assert was_resumed is True
    assert resumed.id == newer.id


async def test_closed_conversations_are_not_resumed(session_factory, clock) -> None:
    async with session_factory() as session, session.begin():
        service = ConversationService(session, clock)
        user = await _make_user(session, clock)
        conversation = await service.start_conversation(user_id=user.id)
        await service.record_inbound_message(
            conversation_id=conversation.id, user_id=user.id, content="hi"
        )
        await service.close_conversation(conversation.id)

    async with session_factory() as session, session.begin():
        service = ConversationService(session, clock)
        resumed, was_resumed = await service.resume_or_start(user_id=user.id)

    assert was_resumed is False
    assert resumed.id != conversation.id


async def test_soft_deleted_messages_leave_the_transcript(session_factory, clock, owner) -> None:
    """A message the owner deleted must not reappear in the next prompt."""
    from sqlalchemy import text as sql_text

    user, conversation = owner
    async with session_factory() as session, session.begin():
        service = ConversationService(session, clock)
        await service.record_inbound_message(
            conversation_id=conversation.id, user_id=user.id, content="keep me"
        )
        await service.record_inbound_message(
            conversation_id=conversation.id, user_id=user.id, content="delete me"
        )

    async with session_factory() as session, session.begin():
        await session.execute(
            sql_text("UPDATE messages SET deleted_at = now() WHERE content = 'delete me'")
        )

    async with session_factory() as session:
        service = ConversationService(session, clock)
        history = await service.history(conversation.id, limit=50)

    assert [m.content for m in history] == ["keep me"]


async def test_the_owner_identity_is_created_once(session_factory, clock) -> None:
    """`ensure_local_owner` runs on every CLI start, so it must be idempotent."""
    async with session_factory() as session, session.begin():
        first = await ConversationService(session, clock).ensure_local_owner()
    async with session_factory() as session, session.begin():
        second = await ConversationService(session, clock).ensure_local_owner()
    assert first.id == second.id
