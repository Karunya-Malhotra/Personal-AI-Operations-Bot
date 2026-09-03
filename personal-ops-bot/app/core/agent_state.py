"""The Agent Runtime's states and the transitions between them.

This is the M1B subset of the state machine in ARCHITECTURE_v0.2 §5.2. Every
state below appears in that diagram; the states that only exist once tools do
(`TOOLS_PROPOSED`, `POLICY_EVALUATING`, `TOOLS_EXECUTING`, `TOOL_RESULTS_READY`,
`AWAITING_CONFIRMATION`, `CONFIRMATION_EXPIRED`, `BACKGROUND_HANDOFF`) are
deliberately absent -- adding a state is a line; having the runtime *pretend* to
support one it cannot reach is a lie in the trace.

Why this is a table rather than control flow (§5.1):

A `while not done:` loop keeps "where is this turn" on the Python call stack.
That works until M1D, when a confirmation suspends a turn across a process
restart and the call stack is gone. At that point a persisted state column has
to be the source of truth, and every retry, timeout and budget rule has to hang
off it. Building the table now costs a file; retrofitting it later is a rewrite
of the runtime, which is precisely why the architecture split M1B out as its own
milestone.

Making the transitions *data* rather than `if` statements buys one specific
thing: an illegal transition is caught by a lookup that no caller can forget,
and the set of legal edges can be asserted in a test (see
tests/unit/test_agent_state.py) instead of being re-derived by reading the
runtime.
"""

from __future__ import annotations

from enum import StrEnum


class RunState(StrEnum):
    RECEIVED = "received"
    CONTEXT_BUILDING = "context_building"
    MODEL_CALLING = "model_calling"
    MODEL_RETRY_WAIT = "model_retry_wait"
    RESPONDING = "responding"
    COMPLETE = "complete"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


#: A run in one of these states will never change again. The reaper must not
#: touch them, and `completed_at` is set exactly when one is entered.
TERMINAL_STATES = frozenset({RunState.COMPLETE, RunState.FAILED, RunState.TIMED_OUT})

#: ARCHITECTURE §5.3 splits states by how strongly they must be persisted.
#: Terminal states are durable facts: they are committed before the process may
#: reply, because "did this turn finish" must survive a crash. The rest are
#: observability -- written on transition, and cheap to lose because the reaper
#: sweeps anything left behind.
DURABLE_STATES = TERMINAL_STATES
OBSERVABILITY_STATES = frozenset(RunState) - DURABLE_STATES

#: The legal edges. Read this as the M1B slice of the §5.2 diagram.
TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    # FAILED is reachable from RECEIVED only because of the reaper: a process
    # can die in any state, including before it does anything, and the sweep
    # must be able to finalise whatever it finds. Without this edge a run
    # orphaned in RECEIVED could never be closed, and would sit in the reaper's
    # index forever. Found by writing the reaper, not by reading the diagram.
    RunState.RECEIVED: frozenset({RunState.CONTEXT_BUILDING, RunState.FAILED}),
    RunState.CONTEXT_BUILDING: frozenset({RunState.MODEL_CALLING, RunState.FAILED}),
    RunState.MODEL_CALLING: frozenset(
        {
            RunState.RESPONDING,
            RunState.MODEL_RETRY_WAIT,
            RunState.FAILED,
            RunState.TIMED_OUT,
        }
    ),
    RunState.MODEL_RETRY_WAIT: frozenset({RunState.MODEL_CALLING, RunState.FAILED}),
    RunState.RESPONDING: frozenset({RunState.COMPLETE, RunState.FAILED}),
    RunState.COMPLETE: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.TIMED_OUT: frozenset(),
}


class IllegalTransitionError(Exception):
    """Raised when the runtime attempts an edge that does not exist.

    This is a programming error, not a runtime condition: it means the state
    machine and the code that drives it have diverged. It is deliberately loud
    rather than a log line, because a runtime that silently accepts an unknown
    transition is one whose trace can no longer be trusted -- and the trace is
    the thing the architecture promises can answer "why did you answer that?".
    """

    def __init__(self, current: RunState, proposed: RunState) -> None:
        super().__init__(
            f"illegal agent run transition {current.value!r} -> {proposed.value!r}; "
            f"legal targets are {sorted(s.value for s in TRANSITIONS[current])}"
        )
        self.current = current
        self.proposed = proposed


def assert_transition(current: RunState, proposed: RunState) -> None:
    """Raise `IllegalTransitionError` unless `current -> proposed` is legal."""
    if proposed not in TRANSITIONS[current]:
        raise IllegalTransitionError(current, proposed)


def is_terminal(state: RunState) -> bool:
    return state in TERMINAL_STATES
