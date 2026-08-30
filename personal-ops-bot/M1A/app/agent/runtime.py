"""The Agent Runtime: one user message in, one persisted answer out.

This is the state machine from ARCHITECTURE §5.2, driving the M1B subset of
states declared in app/core/agent_state.py. There are no tools yet, so the shape
is short -- but it is a state machine rather than a function because at M1D a
confirmation suspends a turn across a process restart, and at that point the
persisted `agent_runs.state` has to already be the source of truth about where
the turn is.

## Where the transaction boundaries fall, and why (your §15)

The ordering you asked about, with the commit points marked:

    persist user message + create run        -- COMMIT
    build context                            (reads only)
    call the model                           -- NO TRANSACTION HELD
    record llm_call + assistant message
        + complete the run                   -- COMMIT (one transaction)

Three deliberate choices in that:

**A. The inbound message is committed before the model is called.** If the
process dies mid-turn, the thing the owner actually said is already durable.
Losing their message because our reply failed would be the worst available
outcome.

**B. No transaction is held across the model call.** A model call takes seconds
and can take thirty. Holding a Postgres transaction open across it would pin a
connection and an MVCC snapshot for the whole duration, and under any
concurrency that is how a connection pool dies. The cost of not holding one is
that a crash here leaves a run stranded in MODEL_CALLING -- which is precisely
what the reaper exists to clean up (app/agent/reaper.py), and why §5.3 classes
these mid-flight states as best-effort rather than durable.

**C. The results commit as one transaction.** `llm_calls`, the assistant
message, and the run reaching COMPLETE either all land or none do. That is what
makes the trace trustworthy: there is no interleaving that produces an answer
the trace cannot explain, or a completed run with no reply attached.

**What happens if that final commit fails** (your §15C): the transaction rolls
back, the run stays non-terminal, no assistant message exists, and the caller
gets an error rather than a reply. The owner sees a failure, which is true --
the turn did not complete. The reaper later marks the run FAILED. What we do
*not* do is return the model's text to the user while failing to record it;
that would be a reply with no trace, and the next turn would have no idea it
had happened.

## Retries

`MODEL_RETRY_WAIT` is a real state, not a `for` loop with a sleep, so every
attempt is a row in `llm_calls` -- including the ones that failed. A turn that
succeeded on its third try and one that succeeded immediately are different
events, and the trace is the only place that difference survives. This is also
why the provider SDKs have their own retries disabled: they would be invisible
here.

Whether an error is retryable is `LLMError.retryable`, decided by the adapter.
The runtime never learns what an HTTP status code is.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.context_builder import BuiltContext, build_context
from app.agent.repository import AgentRunRepository
from app.core.agent_state import RunState
from app.core.clock import Clock
from app.core.llm import LLMError, LLMProvider, LLMRateLimited, LLMTimeout, ModelResponse
from app.domains.conversations import ConversationRepository, ConversationService
from app.observability.logging import bind_contextvars, get_logger, unbind_contextvars
from app.providers.llm.pricing import estimate_cost_micros

log = get_logger(__name__)

Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TurnResult:
    """What the caller (the CLI, later a channel adapter) needs to render."""

    run_id: UUID
    state: RunState
    reply: str | None = None
    failure_kind: str | None = None
    failure_detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.state is RunState.COMPLETE and self.reply is not None


class AgentRuntime:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        llm: LLMProvider,
        clock: Clock,
        model: str,
        max_tokens: int,
        window_messages: int = 40,
        max_attempts: int = 3,
        retry_base_delay_s: float = 1.0,
        sleep: Sleeper | None = None,
    ) -> None:
        self._sessions = session_factory
        self._llm = llm
        self._clock = clock
        self._model = model
        self._max_tokens = max_tokens
        self._window = window_messages
        self._max_attempts = max_attempts
        self._retry_base_delay_s = retry_base_delay_s
        # Injected so tests exercise the retry path without actually waiting.
        # A test that slept for real would be a test nobody runs.
        self._sleep: Sleeper = sleep or asyncio.sleep

    async def handle_user_message(
        self,
        *,
        conversation_id: UUID,
        user_id: UUID,
        content: str,
        provider: str = "cli",
        provider_message_id: str | None = None,
    ) -> TurnResult:
        run_id, trigger_id = await self._begin_turn(
            conversation_id=conversation_id,
            user_id=user_id,
            content=content,
            provider=provider,
            provider_message_id=provider_message_id,
        )

        # Correlation ids for every log line in this turn, including ones five
        # layers down that know nothing about runs (§14).
        bind_contextvars(run_id=str(run_id), conversation_id=str(conversation_id))
        try:
            return await self._drive(
                run_id=run_id,
                conversation_id=conversation_id,
                user_id=user_id,
                trigger_id=trigger_id,
                provider=provider,
            )
        finally:
            unbind_contextvars("run_id", "conversation_id")

    # -- phases -----------------------------------------------------------

    async def _begin_turn(
        self,
        *,
        conversation_id: UUID,
        user_id: UUID,
        content: str,
        provider: str,
        provider_message_id: str | None,
    ) -> tuple[UUID, UUID]:
        """Commit boundary A: the owner's message and the run, durably."""
        async with self._sessions() as session, session.begin():
            service = ConversationService(session, self._clock)
            message = await service.record_inbound_message(
                conversation_id=conversation_id,
                user_id=user_id,
                content=content,
                provider=provider,
                provider_message_id=provider_message_id,
            )
            run = await AgentRunRepository(session, self._clock).create_run(
                conversation_id=conversation_id,
                user_id=user_id,
                trigger_message_id=message.id,
            )
            return run.id, message.id

    async def _drive(
        self,
        *,
        run_id: UUID,
        conversation_id: UUID,
        user_id: UUID,
        trigger_id: UUID,
        provider: str,
    ) -> TurnResult:
        log.info("run.started", state=RunState.RECEIVED.value)

        try:
            context = await self._build(run_id=run_id, conversation_id=conversation_id)
        except Exception as exc:  # context assembly failed -> CONTEXT_BUILDING -> FAILED
            await self._terminate(
                run_id, RunState.FAILED, "context_error", type(exc).__name__, str(exc)
            )
            log.error("run.context_failed", error=type(exc).__name__)
            return TurnResult(
                run_id=run_id,
                state=RunState.FAILED,
                failure_kind="context_error",
                failure_detail=str(exc),
            )

        await self._set_state(run_id, RunState.MODEL_CALLING)
        response, failure = await self._call_with_retries(run_id=run_id, context=context)

        if response is None:
            assert failure is not None
            terminal = RunState.TIMED_OUT if isinstance(failure, LLMTimeout) else RunState.FAILED
            await self._terminate(
                run_id, terminal, failure.kind.lower(), failure.kind, str(failure)
            )
            log.error("run.failed", state=terminal.value, error=failure.kind)
            return TurnResult(
                run_id=run_id,
                state=terminal,
                failure_kind=failure.kind,
                failure_detail=str(failure),
            )

        # Commit boundary C: the reply and the run's completion, atomically.
        async with self._sessions() as session, session.begin():
            runs = AgentRunRepository(session, self._clock)
            run = await runs.get_run(run_id)
            assert run is not None
            await runs.transition(run, RunState.RESPONDING)
            await ConversationService(session, self._clock).record_assistant_message(
                conversation_id=conversation_id,
                user_id=user_id,
                content=response.text,
                run_id=run_id,
                provider=provider,
            )
            await runs.transition(run, RunState.COMPLETE, stop_reason=response.stop_reason.value)

        log.info(
            "run.completed",
            state=RunState.COMPLETE.value,
            stop_reason=response.stop_reason.value,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
        )
        return TurnResult(run_id=run_id, state=RunState.COMPLETE, reply=response.text)

    async def _build(self, *, run_id: UUID, conversation_id: UUID) -> BuiltContext:
        await self._set_state(run_id, RunState.CONTEXT_BUILDING)
        async with self._sessions() as session:
            repo = ConversationRepository(session, self._clock)
            history = await repo.load_recent_messages(conversation_id, limit=self._window)
            total = await repo.count_messages(conversation_id)
        return build_context(
            history,
            model=self._model,
            max_tokens=self._max_tokens,
            window_messages=self._window,
            total_messages=total,
        )

    async def _call_with_retries(
        self, *, run_id: UUID, context: BuiltContext
    ) -> tuple[ModelResponse | None, LLMError | None]:
        last: LLMError | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._llm.complete(context.request)
            except LLMError as exc:
                last = exc
                await self._record_call(run_id=run_id, step_no=attempt, context=context, error=exc)
                attempts_left = attempt < self._max_attempts
                log.warning(
                    "run.model_call_failed",
                    attempt=attempt,
                    error=exc.kind,
                    retryable=exc.retryable,
                    will_retry=exc.retryable and attempts_left,
                )
                if not (exc.retryable and attempts_left):
                    return None, exc

                await self._set_state(run_id, RunState.MODEL_RETRY_WAIT)
                await self._sleep(self._backoff_for(attempt, exc))
                await self._set_state(run_id, RunState.MODEL_CALLING)
                continue

            await self._record_call(
                run_id=run_id, step_no=attempt, context=context, response=response
            )
            return response, None

        return None, last  # unreachable; the loop returns on every path

    def _backoff_for(self, attempt: int, error: LLMError) -> float:
        """Exponential, unless the provider told us how long to wait.

        Honouring `retry_after_s` matters for 429s specifically: retrying sooner
        than asked is how a rate limit becomes a longer rate limit.
        """
        # isinstance rather than getattr: only rate limiting carries a hint,
        # and spelling that out keeps the type checker able to see it.
        if isinstance(error, LLMRateLimited) and error.retry_after_s is not None:
            return error.retry_after_s
        # 2.0 rather than 2: mypy types `int ** int` as Any, since a negative
        # exponent would produce a float.
        return self._retry_base_delay_s * (2.0 ** (attempt - 1))

    # -- persistence helpers ----------------------------------------------

    async def _set_state(self, run_id: UUID, state: RunState) -> None:
        """A best-effort observability transition (§5.3)."""
        async with self._sessions() as session, session.begin():
            runs = AgentRunRepository(session, self._clock)
            run = await runs.get_run(run_id)
            if run is not None:
                await runs.transition(run, state)

    async def _terminate(
        self,
        run_id: UUID,
        state: RunState,
        stop_reason: str,
        failure_kind: str,
        failure_detail: str,
    ) -> None:
        """A durable terminal transition (§5.3): committed before we reply."""
        async with self._sessions() as session, session.begin():
            runs = AgentRunRepository(session, self._clock)
            run = await runs.get_run(run_id)
            if run is not None:
                await runs.transition(
                    run,
                    state,
                    stop_reason=stop_reason,
                    failure_kind=failure_kind,
                    failure_detail=failure_detail,
                )

    async def _record_call(
        self,
        *,
        run_id: UUID,
        step_no: int,
        context: BuiltContext,
        response: ModelResponse | None = None,
        error: LLMError | None = None,
    ) -> None:
        async with self._sessions() as session, session.begin():
            runs = AgentRunRepository(session, self._clock)
            run = await runs.get_run(run_id)
            if run is None:
                return
            cost = estimate_cost_micros(response.model, response.usage) if response else None
            await runs.record_llm_call(
                run=run,
                step_no=step_no,
                provider=response.provider if response else self._llm.name,
                model=response.model if response else self._model,
                prompt_digest=context.prompt_digest,
                context_summary=context.summary,
                ok=response is not None,
                usage=response.usage if response else None,
                cost_micros=cost,
                latency_ms=response.latency_ms if response else None,
                stop_reason=response.raw_stop_reason if response else None,
                error_kind=error.kind if error else None,
                error_detail=str(error) if error else None,
            )
