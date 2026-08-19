"""Conversation lifecycle: who is talking, in which conversation, and what was said.

This sits above the repository and holds the rules that are not SQL: which
conversation a bare `aiops chat` continues, what "new" means, and what happens
when the same inbound message arrives twice.

## Duplicate processing (your §16)

M1B introduces durable execution, so it is worth being explicit about where a
duplicate can appear rather than discovering it at M1E.

**Where duplicates come from.** Not the CLI -- it is synchronous, and a crash
loses the turn rather than repeating it. They come from *channels that retry*:
Telegram and WhatsApp both redeliver a webhook when they do not see a timely
200, so at M1E the same user message can legitimately arrive two or three
times. Without protection each delivery would create its own run and its own
assistant reply, and the user would see the answer twice.

**What protects it today.** `messages (conversation_id, provider_message_id)`
UNIQUE (§11.4). `record_inbound_message` below inserts and treats a unique
violation as "already seen", returning the stored row and reporting
`duplicate=True`. The check is the database's, not a prior SELECT, so two
concurrent deliveries cannot both pass a look-before-you-leap test.

**What is deliberately not solved yet.** The window between "user message
committed" and "assistant reply committed" is not idempotent. If the process
dies mid-turn, the user message is stored with no reply; a retry of the same
provider message is recognised as a duplicate and will *not* re-answer it. That
is the safe failure -- silence rather than a double reply -- but it is a real
limitation: recovery of the orphaned turn needs the reaper (a startup sweep in
M1B.5) plus a resume path that does not exist until the runtime can restart a
run. Stated here so it is a known gap rather than a surprise.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.conversation import ConversationMessage, Role
from app.db.models.conversation import Conversation, Message
from app.db.models.user import User
from app.domains.conversations.repository import ConversationRepository

#: The identity the local CLI speaks as. The CLI runs on the owner's own
#: machine, so the process *is* the owner; there is no authentication to do and
#: none is pretended. Telegram at M1E supplies a real external id instead, and
#: the allowlist starts mattering there.
CLI_PROVIDER = "cli"
CLI_EXTERNAL_ID = "local"


class DuplicateMessage(Exception):
    """Raised when an inbound message has already been recorded."""

    def __init__(self, existing: Message) -> None:
        super().__init__(
            f"message {existing.provider_message_id!r} already recorded in "
            f"conversation {existing.conversation_id}"
        )
        self.existing = existing


class ConversationService:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock
        self._repo = ConversationRepository(session, clock)

    # -- identity ---------------------------------------------------------

    async def ensure_local_owner(self, *, display_name: str = "owner") -> User:
        """Return the CLI owner, creating the user and identity on first run.

        Idempotent, so `aiops chat` can call it unconditionally at startup
        rather than the user having to run a provisioning command first.
        """
        existing = await self._repo.find_user_by_identity(
            provider=CLI_PROVIDER, external_id=CLI_EXTERNAL_ID
        )
        if existing is not None:
            return existing
        return await self._repo.create_user_with_identity(
            display_name=display_name, provider=CLI_PROVIDER, external_id=CLI_EXTERNAL_ID
        )

    # -- lifecycle --------------------------------------------------------

    async def start_conversation(self, *, user_id: UUID, title: str | None = None) -> Conversation:
        return await self._repo.create_conversation(user_id=user_id, title=title)

    async def resume_or_start(self, *, user_id: UUID) -> tuple[Conversation, bool]:
        """The default `aiops chat` behaviour. Returns (conversation, resumed).

        Resuming rather than always starting fresh is what makes the persistence
        visible: restart the CLI, ask "what is my name?", and the answer comes
        from history. Always starting a new conversation would make the database
        an implementation detail nobody could observe.
        """
        existing = await self._repo.latest_open_conversation(user_id)
        if existing is not None:
            return existing, True
        return await self._repo.create_conversation(user_id=user_id), False

    async def close_conversation(self, conversation_id: UUID) -> None:
        await self._repo.close_conversation(conversation_id)

    # -- messages ---------------------------------------------------------

    async def record_inbound_message(
        self,
        *,
        conversation_id: UUID,
        user_id: UUID,
        content: str,
        provider: str = CLI_PROVIDER,
        provider_message_id: str | None = None,
    ) -> Message:
        """Persist a user turn. Raises `DuplicateMessage` if already recorded.

        A SAVEPOINT wraps the insert so that a unique violation does not poison
        the caller's transaction. Without it the whole turn's transaction would
        be aborted by a duplicate that we intend to handle gracefully.
        """
        if provider_message_id is None:
            return await self._repo.append_message(
                conversation_id=conversation_id,
                user_id=user_id,
                role=Role.USER,
                content=content,
                provider=provider,
            )

        try:
            async with self._session.begin_nested():
                return await self._repo.append_message(
                    conversation_id=conversation_id,
                    user_id=user_id,
                    role=Role.USER,
                    content=content,
                    provider=provider,
                    provider_message_id=provider_message_id,
                )
        except IntegrityError as exc:
            existing = await self._repo.find_by_provider_message_id(
                conversation_id=conversation_id, provider_message_id=provider_message_id
            )
            if existing is None:
                raise
            raise DuplicateMessage(existing) from exc

    async def record_assistant_message(
        self,
        *,
        conversation_id: UUID,
        user_id: UUID,
        content: str,
        run_id: UUID,
        provider: str = CLI_PROVIDER,
    ) -> Message:
        """Persist the assistant's reply, tagged with the run that produced it.

        `created_by_run_id` is what makes "this answer -> its whole trace" a
        single join, which is the §26 Scenario 5 requirement.
        """
        return await self._repo.append_message(
            conversation_id=conversation_id,
            user_id=user_id,
            role=Role.ASSISTANT,
            content=content,
            provider=provider,
            created_by_run_id=run_id,
        )

    async def history(self, conversation_id: UUID, *, limit: int) -> list[ConversationMessage]:
        return await self._repo.load_recent_messages(conversation_id, limit=limit)
