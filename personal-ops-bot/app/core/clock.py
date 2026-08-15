"""Time as an injected dependency.

This looks like over-abstraction for a one-line `datetime.now()`. It is not,
and the reason is entirely concrete: at M2 the scheduler computes "every Monday
at 09:00 in Asia/Kolkata" from an RRULE, and the only honest way to test that
across a DST boundary, a leap day, or a 23-hour day is to *move the clock*.
You cannot do that with `datetime.now()` without monkeypatching, and
monkeypatching time is the kind of test infrastructure that breaks in ways that
take an afternoon to diagnose.

Two rules that come with this file:
  1. Nothing outside this module calls `datetime.now()`. A ruff rule enforces
     it (see pyproject: flake8-datetimez / DTZ).
  2. Every timestamp is timezone-aware and in UTC. Naive datetimes are a
     correctness bug waiting for the first DST transition.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """Source of the current time. Injected, never imported as a global."""

    def now(self) -> datetime:
        """Current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    """The real clock. Used everywhere except tests."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """A controllable clock for tests.

    Lives in `app`, not in `tests`, on purpose: it is part of the contract of
    the `Clock` interface, and later milestones (scheduler tests, confirmation
    TTL tests) all need the same one. A fake that only exists in the test tree
    tends to get forked three times.
    """

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self._at = at.astimezone(UTC)

    def now(self) -> datetime:
        return self._at

    def advance(self, delta: timedelta) -> None:
        self._at += delta

    def set(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self._at = at.astimezone(UTC)
