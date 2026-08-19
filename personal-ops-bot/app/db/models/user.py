"""The owner, and the channel identities that map to them.

This system has exactly one user today. The table exists anyway, for one
concrete reason from ARCHITECTURE §11.2: `user_id` is on every user-scoped
table, and `users.timezone` (IANA) drives all rendering and all recurrence math
from M2 onward. Storing "Asia/Kolkata" once, next to the person, is what stops
`datetime` formatting decisions from being scattered and inconsistent later.

`identities` is the allowlist (§20.1): an inbound message is accepted only if
`(provider, external_id)` already exists here. There is no self-service signup.
At M1B the only provider is `cli`; at M1E Telegram reuses this table unchanged,
which is the whole point of introducing it before there is a second channel.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func, text
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(SAUuid(as_uuid=True), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # server_default, not just default=: a Python-side default only applies to
    # ORM inserts, so a migration or a raw-SQL writer would hit the NOT NULL.
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'Asia/Kolkata'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Identity(Base):
    __tablename__ = "identities"
    __table_args__ = (
        # The allowlist hot path (§11.4). UNIQUE rather than just indexed:
        # two users must never be able to claim the same channel identity.
        UniqueConstraint("provider", "external_id", name="provider_external_id"),
    )

    id: Mapped[UUID] = mapped_column(SAUuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        SAUuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
