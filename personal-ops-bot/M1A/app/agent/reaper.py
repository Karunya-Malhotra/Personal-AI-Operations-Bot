"""Closing out runs that a crash left in flight.

ARCHITECTURE §5.3 calls this "30 lines and it is not optional", and the reason
is concrete: the runtime deliberately holds no transaction across the model call
(see app/agent/runtime.py), so a process that dies mid-turn leaves a row in
MODEL_CALLING that nothing will ever advance. Without a sweep those rows look
in-flight forever -- they pollute every latency and success-rate figure computed
afterwards, and they accumulate in the partial index that exists to stay small.

This is the **startup sweep**. §5.3 puts the periodic version in M2, alongside
the job queue that will run it; until then it runs once, at boot, which covers
the case that actually happens on a laptop and a single always-on host: the
process restarted and something was mid-turn when it went down.

I have been living inside the failure mode this handles -- the sandbox running
this project reaps background processes between sessions, which is exactly a
process vanishing between one moment and the next with no chance to clean up.

**What it cannot do:** it marks a turn as failed; it does not resume it. The
owner's message stays committed (commit boundary A), so nothing they said is
lost, but they will not get a late answer to it. Resuming an interrupted turn
needs the confirmation machinery at M1D, where a run becomes something that can
be picked up rather than only closed.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.repository import AgentRunRepository
from app.core.agent_state import RunState
from app.core.clock import Clock
from app.observability.logging import get_logger

log = get_logger(__name__)

ORPHANED = "orphaned"


async def sweep_orphaned_runs(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    older_than_s: float,
) -> int:
    """Mark stale non-terminal runs FAILED. Returns how many were swept.

    Every sweep is logged at warning, with the run ids: an orphaned run means a
    process died mid-turn, which is worth noticing rather than silently
    tidying away.
    """
    async with session_factory() as session, session.begin():
        runs = AgentRunRepository(session, clock)
        orphaned = await runs.find_orphaned_runs(older_than_s=older_than_s)
        for run in orphaned:
            await runs.transition(
                run,
                RunState.FAILED,
                stop_reason=ORPHANED,
                failure_kind=ORPHANED,
                failure_detail=(
                    f"run was still in state {run.state!r} more than {older_than_s:.0f}s "
                    f"after it started; presumed interrupted by a process exit"
                ),
            )

    if orphaned:
        log.warning(
            "reaper.swept_orphaned_runs",
            count=len(orphaned),
            run_ids=[str(r.id) for r in orphaned],
        )
    return len(orphaned)
