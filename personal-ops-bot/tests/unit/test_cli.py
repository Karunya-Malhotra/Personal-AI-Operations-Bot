"""The CLI's own logic, which is deliberately only rendering.

M1A's echo seam (`handle_message`) is gone: the Agent Runtime replaced it, which
is exactly what that seam existed to make cheap. What remains testable here is
how a `TurnResult` is presented, because that is the CLI's entire job.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.agent.runtime import TurnResult
from app.cli import render
from app.core.agent_state import RunState


def test_a_successful_turn_renders_just_the_reply() -> None:
    """No prefix, no decoration: the assistant's words are the output."""
    result = TurnResult(run_id=uuid4(), state=RunState.COMPLETE, reply="Hello there.")
    assert render(result) == "Hello there."


@pytest.mark.parametrize(
    ("failure_kind", "expected_phrase"),
    [
        ("LLMTimeout", "did not respond in time"),
        ("LLMUnavailable", "unreachable"),
        ("LLMRateLimited", "rate limiting"),
        ("LLMAuthError", "rejected our credentials"),
        ("LLMMalformedResponse", "could not read"),
        ("context_error", "history could not be assembled"),
    ],
)
def test_each_failure_is_reported_in_the_owners_words(
    failure_kind: str, expected_phrase: str
) -> None:
    """§26 Scenario 4: a useful user-facing error. "Something went wrong" tells
    the owner nothing about whether to retry, wait, or fix a key."""
    rendered = render(TurnResult(run_id=uuid4(), state=RunState.FAILED, failure_kind=failure_kind))
    assert expected_phrase in rendered
    assert "no answer" in rendered


def test_a_failure_never_renders_as_an_empty_reply() -> None:
    """§15A at the last mile. Printing nothing would read as the assistant
    having chosen to say nothing, which is a different and false claim."""
    rendered = render(
        TurnResult(run_id=uuid4(), state=RunState.FAILED, failure_kind="LLMUnavailable")
    )
    assert rendered.strip()
    assert "no answer" in rendered


def test_a_failure_names_the_run_so_the_trace_can_be_found() -> None:
    run_id = uuid4()
    rendered = render(
        TurnResult(run_id=run_id, state=RunState.TIMED_OUT, failure_kind="LLMTimeout")
    )
    assert str(run_id) in rendered
    assert "timed_out" in rendered


def test_an_unrecognised_failure_still_renders_something_useful() -> None:
    """A new error kind must not produce a blank or a KeyError."""
    rendered = render(
        TurnResult(run_id=uuid4(), state=RunState.FAILED, failure_kind="SomethingNew")
    )
    assert "unexpected error" in rendered


def test_the_owners_message_is_reported_as_kept() -> None:
    """Commit boundary A means their message survived even though the turn
    failed. Saying so is the difference between "retry" and "retype"."""
    rendered = render(
        TurnResult(run_id=uuid4(), state=RunState.FAILED, failure_kind="LLMUnavailable")
    )
    assert "beyond your message" in rendered
