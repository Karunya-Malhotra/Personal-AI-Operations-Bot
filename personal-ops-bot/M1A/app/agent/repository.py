"""Persistence for the trace tables: `agent_runs` and `llm_calls`.

Two things about the shape of this module.

**Transition validation lives here, not in the runtime.** `assert_transition`
could sit in the state machine's driving code, but the property that actually
matters is that an illegal state never reaches the *database* -- the trace is
only worth reading if it cannot contain a turn that went from RECEIVED straight
to COMPLETE. Putting the check on the write path means no caller can skip it,
including callers that do not exist yet.

**Nothing here opens a transaction**, for the same reason as the conversation
repository: the runtime decides where the commit boundaries fall, and in this
case those boundaries are load-bearing. See app/agent/runtime.py for why the
model call happens with no transaction held.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_state import RunState, assert_transition, is_terminal
from app.core.clock import Clock
from app.core.ids import uuid7
from app.core.llm import Usage
from app.db.models.agent_run import AgentRun
from app.db.models.llm_call import LlmCall


class AgentRunRepository:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def create_run(
        self, *, conversation_id: UUID, user_id: UUID, trigger_message_id: UUID | None
    ) -> AgentRun:
        """Open a run in RECEIVED. The row *is* the turn from here on."""
        now = self._clock.now()
        run = AgentRun(
            id=uuid7(now),
            conversation_id=conversation_id,
            user_id=user_id,
            trigger_message_id=trigger_message_id,
            state=RunState.RECEIVED.value,
            started_at=now,
            total_cost_micros=0,
        )
        self._session.add(run)
        await self._session.flush()
        return run

    async def get_run(self, run_id: UUID) -> AgentRun | None:
        return await self._session.get(AgentRun, run_id)

    async def transition(
        self,
        run: AgentRun,
        to: RunState,
        *,
        stop_reason: str | None = None,
        failure_kind: str | None = None,
        failure_detail: str | None = None,
    ) -> AgentRun:
        """Move a run to `to`, refusing edges the state machine does not have.

        Terminal states get `completed_at` in the same statement. The database
        has a CHECK asserting terminal-iff-completed, so forgetting it here
        would be an integrity error rather than a silently half-finished run --
        but setting it here means that check never has to fire.
        """
        assert_transition(RunState(run.state), to)
        run.state = to.value
        if stop_reason is not None:
            run.stop_reason = stop_reason
        if failure_kind is not None:
            run.failure_kind = failure_kind
        if failure_detail is not None:
            # Bounded: a provider traceback can be enormous, and this column is
            # read by a human trying to see what happened, not by a parser.
            run.failure_detail = failure_detail[:2000]
        if is_terminal(to):
            run.completed_at = self._clock.now()
        await self._session.flush()
        return run

    async def record_llm_call(
        self,
        *,
        run: AgentRun,
        step_no: int,
        provider: str,
        model: str,
        prompt_digest: str,
        context_summary: dict[str, object],
        ok: bool,
        usage: Usage | None = None,
        cost_micros: int | None = None,
        latency_ms: int | None = None,
        stop_reason: str | None = None,
        error_kind: str | None = None,
        error_detail: str | None = None,
    ) -> LlmCall:
        """One row per model invocation, including the ones that failed.

        Failed attempts are recorded deliberately: a turn that succeeded on its
        third try and one that succeeded immediately are different events, and
        only the trace can tell them apart. This is also why the SDK's own
        retries are disabled -- they would never appear here.
        """
        now = self._clock.now()
        call = LlmCall(
            id=uuid7(now),
            run_id=run.id,
            step_no=step_no,
            provider=provider,
            model=model,
            prompt_digest=prompt_digest,
            context_summary=context_summary,
            tokens_in=usage.input_tokens if usage else None,
            tokens_out=usage.output_tokens if usage else None,
            tokens_cached=usage.cached_input_tokens if usage else None,
            # `None` from the pricing table means "no rate known", which is not
            # the same as free. It is stored as 0 so the column stays NOT NULL,
            # and the token columns above keep the truth for a later backfill.
            cost_micros=cost_micros or 0,
            latency_ms=latency_ms,
            ok=ok,
            stop_reason=stop_reason,
            error_kind=error_kind,
            error_detail=error_detail[:2000] if error_detail else None,
            called_at=now,
        )
        self._session.add(call)
        run.total_cost_micros = (run.total_cost_micros or 0) + (cost_micros or 0)
        await self._session.flush()
        return call

    async def calls_for_run(self, run_id: UUID) -> list[LlmCall]:
        """Every attempt, in order. The join behind "what happened in this turn"."""
        stmt = select(LlmCall).where(LlmCall.run_id == run_id).order_by(LlmCall.step_no)
        return list((await self._session.execute(stmt)).scalars().all())

    async def find_orphaned_runs(self, *, older_than_s: float) -> list[AgentRun]:
        """Non-terminal runs too old to still be running.

        This is the query the partial index on `agent_runs (state, started_at)`
        exists for (§5.3). It must stay cheap forever, which is why the index
        covers only non-terminal rows -- in a healthy system, a handful.
        """
        cutoff = self._clock.now() - timedelta(seconds=older_than_s)
        terminal = [s.value for s in RunState if is_terminal(s)]
        stmt = select(AgentRun).where(AgentRun.state.notin_(terminal), AgentRun.started_at < cutoff)
        return list((await self._session.execute(stmt)).scalars().all())
