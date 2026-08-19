"""One row per model invocation.

ARCHITECTURE §11.1 merged `agent_steps` and `llm_usage` into this table: every
step in this runtime is either a model call or a batch of tool calls, and tool
calls have their own table from M1C. So a step *is* a model call, carrying its
own context summary, usage and cost. One fewer join on the hot debugging path.

## The durable-tracing privacy tradeoff (your §13)

The architecture promises the trace can answer "why did you answer that?"
(§983). Taken naively that argues for storing the full prompt and response.
This table deliberately does not, and the reasoning is worth stating because it
is a real tension rather than an obvious call:

*The case for storing raw payloads.* Reproducing a bad answer is easiest when
you can see the exact bytes the model saw. Digests cannot be un-hashed.

*The case against, which wins here.* The prompt is a copy of the conversation,
and the conversation is already stored in `messages` -- durably, soft-deletable,
and under the user's control. Copying it into a trace table would create a
**second, undeletable copy of personal content** with a different retention
policy (§972: traces are hard-retained for 90 days then purged, while messages
are user-visible content the owner may delete). Deleting a message would then
not actually delete it. That is a privacy bug wearing an observability costume.

*What is stored instead:* `prompt_digest`, a SHA-256 over the exact rendered
request, which answers "was the input identical to last time?" and "did the
context change between these two calls?" without retaining the content; and
`context_summary`, a JSONB record of *what was assembled and why* -- how many
messages, which window, how many tokens -- which is the part you actually reach
for when a context bug is suspected. The content itself is recovered by joining
to `messages`, which is where it already lives.

`cost_micros` is millionths of a currency unit, integer-only, following the
money convention in §11.3: no floats anywhere near an amount.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LlmCall(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (
        # Step numbers are dense and ordered within a run. UNIQUE makes a
        # double-recorded step a database error rather than a silently
        # duplicated row in the cost rollup.
        UniqueConstraint("run_id", "step_no", name="run_id_step_no"),
    )

    id: Mapped[UUID] = mapped_column(SAUuid(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        SAUuid(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)

    #: What was assembled and why -- counts and windows, never content.
    context_summary: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    #: SHA-256 of the exact rendered request. Comparable, not reversible.
    prompt_digest: Mapped[str] = mapped_column(String(64), nullable=False)

    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_cached: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: The provider's own stop reason, normalised by the adapter.
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    called_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
