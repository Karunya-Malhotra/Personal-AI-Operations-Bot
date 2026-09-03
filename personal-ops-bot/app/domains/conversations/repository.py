"""Data access for conversations and messages. SQL lives here and nowhere else.

The repository does not open transactions. It is handed an `AsyncSession` and
the *caller* decides where the commit boundary falls. That is not indecision --
it is the point. ARCHITECTURE §15 asks what happens when the database write
fails after the model has already responded, and the only component that can
answer is the one that knows the whole turn: the Agent Runtime. If each method
committed on its own, the runtime could not group "persist the assistant reply"
and "mark the run complete" into one atomic step, and a crash between them
would leave a reply the trace does not explain.

Ordering is `(sent_at, id)` everywhere history is read. The tiebreak is not
decoration: under a FrozenClock every row in a test shares `sent_at`, and even
in production two rows can land in one millisecond. `id` is a UUIDv7 whose
counter is monotonic within a millisecond (app/core/ids.py), so the pair is a
total order. Ordering by `sent_at` alone would let a user turn and the
assistant's reply come back inverted.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.conversation import ConversationMessage, Role
from app.core.ids import uuid7
from app.db.models.conversation import Conversation, Message
from app.db.models.user import Identity, User


class ConversationRepository:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    # -- identity ---------------------------------------------------------

    async def find_user_by_identity(self, *, provider: str, external_id: str) -> User | None:
        """The allowlist lookup (§20.1). No row means the sender is unknown."""
        stmt = (
            select(User)
            .join(Identity, Identity.user_id == User.id)
            .where(Identity.provider == provider, Identity.external_id == external_id)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def create_user_with_identity(
        self, *, display_name: str, provider: str, external_id: str, timezone: str | None = None
    ) -> User:
        now = self._clock.now()
        user = User(id=uuid7(now), display_name=display_name)
        if timezone is not None:
            user.timezone = timezone
        self._session.add(user)
        # Flush the user before adding the identity. There is no relationship()
        # between these models -- deliberately, since nothing needs to navigate
        # between them -- so SQLAlchemy's unit of work has no dependency to sort
        # by and can emit the identity insert first, violating the FK.
        await self._session.flush()

        self._session.add(
            Identity(id=uuid7(now), user_id=user.id, provider=provider, external_id=external_id)
        )
        await self._session.flush()
        return user

    # -- conversations ----------------------------------------------------

    async def create_conversation(self, *, user_id: UUID, title: str | None = None) -> Conversation:
        conversation = Conversation(id=uuid7(self._clock.now()), user_id=user_id, title=title)
        self._session.add(conversation)
        await self._session.flush()
        return conversation

    async def get_conversation(self, conversation_id: UUID) -> Conversation | None:
        return await self._session.get(Conversation, conversation_id)

    async def latest_open_conversation(self, user_id: UUID) -> Conversation | None:
        """The one `aiops chat` resumes by default.

        Ordered by `last_message_at` rather than `created_at` so a conversation
        you actually spoke in wins over one you opened and abandoned. NULLs (a
        conversation with no messages yet) sort last for the same reason.
        """
        stmt = (
            select(Conversation)
            .where(Conversation.user_id == user_id, Conversation.closed_at.is_(None))
            .order_by(
                Conversation.last_message_at.desc().nullslast(),
                Conversation.id.desc(),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def close_conversation(self, conversation_id: UUID) -> None:
        conversation = await self._session.get(Conversation, conversation_id)
        if conversation is not None:
            conversation.closed_at = self._clock.now()

    # -- messages ---------------------------------------------------------

    async def append_message(
        self,
        *,
        conversation_id: UUID,
        user_id: UUID,
        role: Role,
        content: str,
        provider: str,
        provider_message_id: str | None = None,
        created_by_run_id: UUID | None = None,
        sent_at: datetime | None = None,
    ) -> Message:
        """Insert one turn and move the conversation's `last_message_at`.

        Both writes happen in the caller's transaction, so a conversation whose
        `last_message_at` disagrees with its newest message is not a state this
        code can produce.
        """
        when = sent_at or self._clock.now()
        message = Message(
            id=uuid7(when),
            conversation_id=conversation_id,
            user_id=user_id,
            role=role.value,
            content=content,
            provider=provider,
            provider_message_id=provider_message_id,
            created_by_run_id=created_by_run_id,
            sent_at=when,
        )
        self._session.add(message)

        conversation = await self._session.get(Conversation, conversation_id)
        if conversation is not None and (
            conversation.last_message_at is None or conversation.last_message_at < when
        ):
            conversation.last_message_at = when

        await self._session.flush()
        return message

    async def find_by_provider_message_id(
        self, *, conversation_id: UUID, provider_message_id: str
    ) -> Message | None:
        """Look up an already-recorded inbound message. Used only on the
        duplicate path, after the database has rejected the insert."""
        stmt = select(Message).where(
            Message.conversation_id == conversation_id,
            Message.provider_message_id == provider_message_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def load_recent_messages(
        self, conversation_id: UUID, *, limit: int
    ) -> list[ConversationMessage]:
        """The newest `limit` turns, returned oldest-first.

        Read newest-first with a LIMIT so the index does the work and the query
        cost does not grow with conversation length, then reversed in Python so
        callers always receive chronological order. A caller that had to
        remember to reverse would eventually forget.

        Soft-deleted rows are excluded (§11.2): a message the owner deleted must
        not reappear in the next prompt.
        """
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id, Message.deleted_at.is_(None))
            .order_by(Message.sent_at.desc(), Message.id.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            ConversationMessage(
                id=row.id, role=Role(row.role), content=row.content, sent_at=row.sent_at
            )
            for row in reversed(rows)
        ]

    async def count_messages(self, conversation_id: UUID) -> int:
        stmt = select(Message).where(
            Message.conversation_id == conversation_id, Message.deleted_at.is_(None)
        )
        return len((await self._session.execute(stmt)).scalars().all())
