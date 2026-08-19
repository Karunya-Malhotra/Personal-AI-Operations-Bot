"""UUIDv7 generation: the bit layout, and time-ordering under a frozen clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

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
    """Ordering comes from the counter; uniqueness must come from the entropy."""
    values = {uuid7(AT) for _ in range(2000)}
    assert len(values) == 2000


def test_ids_in_the_same_millisecond_are_still_strictly_ordered() -> None:
    """The regression this guards is subtle and was measured, not theorised.

    With `rand_a` filled randomly, ~52% of consecutive same-millisecond pairs
    sorted in the wrong order. Message ordering is `(sent_at, id)`, so under a
    FrozenClock -- where every row shares a timestamp -- a user turn and the
    assistant's reply could be replayed to the model inverted.
    """
    # Its own millisecond: the counter is process-global, so sharing `AT` with
    # another test would start this burst mid-range and saturate it early.
    at = AT + timedelta(seconds=11)
    values = [uuid7(at) for _ in range(3000)]
    assert all(a < b for a, b in pairwise(values))


def test_ordering_holds_across_a_millisecond_boundary() -> None:
    """The counter reseeds when the millisecond changes, so this checks the
    timestamp bits still dominate the ordering."""
    clock = FrozenClock(AT + timedelta(seconds=22))
    values = []
    for _ in range(50):
        values.append(uuid7(clock.now()))
        clock.advance(timedelta(milliseconds=1))
    assert all(a < b for a, b in pairwise(values))
