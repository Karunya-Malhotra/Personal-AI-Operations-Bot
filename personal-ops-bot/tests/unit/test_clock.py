"""Time is injected, and it is always timezone-aware."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock, SystemClock


def test_system_clock_is_timezone_aware() -> None:
    assert SystemClock().now().tzinfo is not None


def test_frozen_clock_does_not_move_on_its_own() -> None:
    clock = FrozenClock(datetime(2026, 8, 14, 9, 0, tzinfo=UTC))
    assert clock.now() == clock.now()


def test_frozen_clock_advances_explicitly() -> None:
    """The capability M2's RRULE tests and M1D's confirmation-TTL tests need."""
    clock = FrozenClock(datetime(2026, 8, 14, 9, 0, tzinfo=UTC))
    clock.advance(timedelta(minutes=6))
    assert clock.now() == datetime(2026, 8, 14, 9, 6, tzinfo=UTC)


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        # DTZ001 is suppressed because constructing a naive datetime is
        # precisely what this test asserts is rejected. The lint rule and
        # the test are enforcing the same invariant from opposite sides.
        FrozenClock(datetime(2026, 8, 14, 9, 0))  # noqa: DTZ001
