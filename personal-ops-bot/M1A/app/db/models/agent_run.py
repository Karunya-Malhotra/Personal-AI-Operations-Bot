"""The trace tables: one row per turn, one row per model call.

Why a run is a database row rather than a function call (your §28):

An in-memory call is enough right up until you ask a question about it after it
finished. "Why did it answer that?", "what did last week cost?", "is anything
stuck right now?" are all questions about turns that are no longer running, and
none of them can be answered from a stack frame. ARCHITECTURE §61 puts it more
bluntly: these tables are how you debug everything else, and adding them after
the fact means you cannot debug the thing that made you want them.

The second reason is structural rather than diagnostic. At M1D a confirmation
suspends a turn across a process restart; the row *is* the turn at that point.
Introducing it now means M1D adds a state, not a persistence layer.

`agent_runs.state` is the state machine's column (app/core/agent_state.py). The
partial index below is the reaper's query (§5.3) and must stay small -- it only
covers non-terminal runs, which in a healthy system is a handful of rows.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.agent_state import TERMINAL_STATES, RunState
from app.db.base import Base

_STATE_VALUES = ", ".join(f"'{s.value}'" for s in RunState)
_TERMINAL_VALUES = ", ".join(f"'{s.value}'" for s in sorted(TERMINAL_STATES))


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(f"state IN ({_STATE_VALUES})", name="state_valid"),
        # A terminal run has an end time; a live one does not. This is the
        # database refusing to represent "completed but still running", which
        # is exactly the corruption a crash mid-turn would otherwise produce.
        CheckConstraint(
            f"(state IN ({_TERMINAL_VALUES})) = (completed_at IS NOT NULL)",
            name="terminal_iff_completed",
        ),
        Index("ix_agent_runs_conversation_id_started_at", "conversation_id", "started_at"),
        # The reaper's index (§5.3): only in-flight runs. Partial so it stays
        # tiny no matter how much history accumulates.
        Index(
            "ix_agent_runs_state_started_at",
            "state",
            "started_at",
            postgresql_where=text(f"state NOT IN ({_TERMINAL_VALUES})"),
        ),
    )

    id: Mapped[UUID] = mapped_column(SAUuid(as_uuid=True), primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        SAUuid(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        SAUuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: The inbound message that caused this turn. Makes "user message -> run"
    #: navigable in both directions (§26 Scenario 5).
    trigger_message_id: Mapped[UUID | None] = mapped_column(
        SAUuid(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Why the run ended: 'end_turn', 'provider_error', 'timeout', 'orphaned'.
    #: Free-ish text rather than an enum because this is a diagnostic label, not
    #: a value any rule branches on.
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Rolled up from llm_calls so "what did this turn cost" is one read.
    total_cost_micros: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
