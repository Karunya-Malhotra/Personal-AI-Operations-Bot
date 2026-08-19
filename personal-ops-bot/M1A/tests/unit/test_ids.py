"""UUIDv7 generation: the bit layout, and time-ordering under a frozen clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock
from app.core.ids import timestamp_ms, uuid7

AT = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)


def test_version_and_variant_bits_are_rfc9562() -> None:
    value = uuid7(AT)
    assert value.version == 7
    # RFC 9562 variant is the two high bits of clock_seq_hi_and_reserved == 0b10.
    assert (value.int >> 62) & 0b11 == 0b10


def test_embedded_timestamp_roundtrips() -> None:
    assert timestamp_ms(uuid7(AT)) == int(AT.timestamp() * 1000)


def test_naive_datetime_is_rejected() -> None:
    """Same rule as Clock: a naive datetime is a bug, not a default."""
    with pytest.raises(ValueError, match="timezone-aware"):
        uuid7(datetime(2026, 8, 19, 12, 0, 0))  # noqa: DTZ001


def test_ids_sort_chronologically() -> None:
    """The property that makes UUIDv7 worth using: index locality and
    `ORDER BY id` being chronological on the trace tables."""
    clock = FrozenClock(AT)
    first = uuid7(clock.now())
    clock.advance(timedelta(milliseconds=5))
    second = uuid7(clock.now())
    clock.advance(timedelta(seconds=1))
    third = uuid7(clock.now())

    assert first < second < third
    assert sorted([third, first, second]) == [first, second, third]


def test_ids_are_unique_within_the_same_millisecond() -> None:
    """Ordering comes from the clock; uniqueness must come from the entropy."""
    values = {uuid7(AT) for _ in range(2000)}
    assert len(values) == 2000
