"""Conversations and the messages in them -- the system of record for chat.

The architectural claim worth being explicit about (your §28): **conversation
state is an application concern, not model memory.** The provider is stateless;
it is handed a transcript on every call and remembers nothing between them. So
"what is my name?" works across a restart only if we stored the earlier turn and
replay it. Treating this as "the model remembers" would be a category error that
breaks the moment the process restarts, the model is swapped, or the same
conversation is resumed from a different channel at M1E.

Two indexes here are load-bearing rather than optimisations (§11.4):

  - `(conversation_id, provider_message_id)` UNIQUE is the **idempotency** key.
    It is what stops a redelivered inbound message from being processed twice.
    See app/domains/conversations/service.py for how the runtime uses it.
  - `(conversation_id, sent_at DESC)` is the context-assembly read path: every
    turn loads the tail of the conversation in that exact order.

Soft delete (`deleted_at`) applies to messages because they are user-visible
content (§11.2). Trace tables get hard retention instead -- deleting a message
must not silently rewrite the history of why the assistant said something.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.conversation import Role
from app.db.base import Base

_ROLE_VALUES = ", ".join(f"'{r.value}'" for r in Role)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_user_id_last_message_at", "user_id", "last_message_at"),
    )

    id: Mapped[UUID] = mapped_column(SAUuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        SAUuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Denormalised so "resume my latest conversation" is one indexed read
    #: rather than an aggregate over messages on every CLI start.
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Lifecycle: a closed conversation is never resumed by `/new` or by
    #: "latest". Nothing in M1B closes one automatically; the column exists so
    #: the lifecycle is representable rather than implied by absence.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(f"role IN ({_ROLE_VALUES})", name="role_valid"),
        # The idempotency key. Postgres treats NULLs as distinct in a UNIQUE
        # constraint, so the many locally-authored messages (which have no
        # provider id) never collide with each other -- while a redelivered
        # inbound message, which does carry one, is rejected on insert.
        UniqueConstraint(
            "conversation_id", "provider_message_id", name="conversation_id_provider_message_id"
        ),
        Index("ix_messages_conversation_id_sent_at", "conversation_id", "sent_at"),
    )

    id: Mapped[UUID] = mapped_column(SAUuid(as_uuid=True), primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        SAUuid(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        SAUuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: Which channel delivered (or will deliver) this message. 'cli' at M1B.
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The channel's own id for this message, when it has one. NULL for
    #: messages we authored. Half of the idempotency key above.
    provider_message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Set on assistant messages: which run produced this. This is the
    #: `created_by_run_id` convention from §11.2 and it is what makes
    #: "assistant response -> its trace" a single join.
    created_by_run_id: Mapped[UUID | None] = mapped_column(
        SAUuid(as_uuid=True),
        # Circular with agent_runs.trigger_message_id, so the constraint is
        # added after both tables exist (see the migration). use_alter lets
        # SQLAlchemy order it correctly for create_all() in tests too.
        ForeignKey("agent_runs.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
