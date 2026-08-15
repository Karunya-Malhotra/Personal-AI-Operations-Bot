"""The CLI seam that M1B replaces."""

from __future__ import annotations

from app.cli import handle_message


def test_m1a_echoes() -> None:
    assert handle_message("hello") == "echo: hello"


def test_handle_message_is_a_pure_function() -> None:
    """Kept as a plain function, not inlined into the input() loop, so M1B's
    first agent test can assert on a turn without driving a terminal."""
    assert handle_message("a") != handle_message("b")
