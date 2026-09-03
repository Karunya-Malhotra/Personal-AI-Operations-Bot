"""The state machine's shape.

These are structural tests. They exist because the runtime's correctness rests
on the transition table being complete and closed -- a missing entry would be a
KeyError in the middle of a turn, and an extra edge would be a path the trace
cannot explain.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from app.core.agent_state import (
    DURABLE_STATES,
    OBSERVABILITY_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    IllegalTransitionError,
    RunState,
    assert_transition,
    is_terminal,
)


def test_every_state_has_a_transition_entry() -> None:
    """A missing key is a KeyError mid-turn, so assert totality rather than
    discovering it in production."""
    assert set(TRANSITIONS) == set(RunState)


def test_transition_targets_are_all_real_states() -> None:
    for source, targets in TRANSITIONS.items():
        assert targets <= set(RunState), source


def test_terminal_states_have_no_outgoing_edges() -> None:
    for state in TERMINAL_STATES:
        assert TRANSITIONS[state] == frozenset()
        assert is_terminal(state)


def test_durable_and_observability_partition_the_state_set() -> None:
    """ARCHITECTURE §5.3 classifies every state as one or the other."""
    assert DURABLE_STATES | OBSERVABILITY_STATES == set(RunState)
    assert DURABLE_STATES & OBSERVABILITY_STATES == frozenset()


def test_every_state_is_reachable_from_received() -> None:
    """An unreachable state is dead code that will mislead whoever reads the
    trace vocabulary later."""
    seen = {RunState.RECEIVED}
    frontier = [RunState.RECEIVED]
    while frontier:
        for target in TRANSITIONS[frontier.pop()]:
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    assert seen == set(RunState)


def test_happy_path_is_legal() -> None:
    path = [
        RunState.RECEIVED,
        RunState.CONTEXT_BUILDING,
        RunState.MODEL_CALLING,
        RunState.RESPONDING,
        RunState.COMPLETE,
    ]
    for current, proposed in pairwise(path):
        assert_transition(current, proposed)


def test_retry_loop_is_legal() -> None:
    assert_transition(RunState.MODEL_CALLING, RunState.MODEL_RETRY_WAIT)
    assert_transition(RunState.MODEL_RETRY_WAIT, RunState.MODEL_CALLING)
    assert_transition(RunState.MODEL_RETRY_WAIT, RunState.FAILED)


@pytest.mark.parametrize(
    ("current", "proposed"),
    [
        (RunState.RECEIVED, RunState.COMPLETE),  # cannot skip the work
        (RunState.RECEIVED, RunState.MODEL_CALLING),  # cannot skip context building
        (RunState.COMPLETE, RunState.MODEL_CALLING),  # terminal is terminal
        (RunState.FAILED, RunState.RESPONDING),
        (RunState.CONTEXT_BUILDING, RunState.RESPONDING),  # no answer without a model call
    ],
)
def test_illegal_transitions_raise(current: RunState, proposed: RunState) -> None:
    with pytest.raises(IllegalTransitionError) as exc:
        assert_transition(current, proposed)
    assert exc.value.current is current
    assert exc.value.proposed is proposed
