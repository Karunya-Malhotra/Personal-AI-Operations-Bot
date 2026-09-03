"""UUIDv7 identifiers.

Why UUIDv7 rather than a bigserial or a UUIDv4 (ARCHITECTURE §11.2):

  - **Not bigserial.** IDs appear in logs, in trace joins, and (from M1E) in
    messages relayed between processes. A sequential integer leaks volume
    ("run 41") and collides across tables, so `41` is ambiguous between a run
    and a message. A UUID is unambiguous everywhere.

  - **Not UUIDv4.** Random UUIDs scatter B-tree inserts across the whole index,
    so every insert dirties a different page. UUIDv7 puts a 48-bit millisecond
    timestamp in the high bits, so consecutively-created rows sort together and
    land on the same pages. It also means `ORDER BY id` is chronological, which
    is exactly what the trace tables are read by.

The layout is RFC 9562 §5.7:

    48 bits  unix_ts_ms
     4 bits  version (0111)
    12 bits  rand_a   -- used here as a monotonic counter, see below
     2 bits  variant (10)
    62 bits  rand_b

**`rand_a` is a counter, not random** (RFC 9562 §6.2, "monotonic random").
Filling it randomly makes ids generated within the same millisecond sort in
random order -- measured at ~52% inversions on consecutive pairs. That is not
academic here: message ordering is `(sent_at, id)`, so two messages written in
the same millisecond could be replayed to the model in the wrong order, turning
a user turn and the assistant's reply around. Under a FrozenClock *every*
message shares a timestamp, so without the counter test ordering is pure chance.

The counter is per-process. Across processes ordering falls back to the
timestamp, which is the correct resolution because separate processes writing
inside one millisecond have no real order to preserve anyway.

`at` is a parameter rather than a call to `time.time()` for the same reason
`Clock` exists (see app/core/clock.py): a repository generating an id passes
`clock.now()`, so under a FrozenClock the generated ids are deterministic and
time-ordered. Tests that assert "these rows came back in creation order" then
mean something. A module that silently read the wall clock would quietly
undermine every frozen-clock test that touches a primary key.
"""

from __future__ import annotations

import secrets
import threading
from datetime import datetime
from uuid import UUID

_VERSION = 0x7
_VARIANT = 0b10
_COUNTER_BITS = 12
_COUNTER_MAX = (1 << _COUNTER_BITS) - 1

#: Guards the counter. Ids are generated from request-handling code that may run
#: on more than one thread, and a torn read here would reintroduce exactly the
#: inversions the counter exists to prevent.
_lock = threading.Lock()
_last_ms = -1
_last_counter = 0


def _next_counter(unix_ts_ms: int) -> int:
    """Monotonic within a millisecond; reseeded when the millisecond changes.

    A fresh millisecond starts from a small random offset rather than 0, so an
    id does not advertise that it was the first of its millisecond, while still
    leaving ~3840 of the 4096 slots as headroom.

    **The guarantee, stated precisely:** ids generated in the same millisecond
    by this process are strictly increasing until the counter saturates. Past
    that the counter stops advancing rather than rolling over -- rolling over
    would emit an id sorting *before* its predecessor, the one thing this
    function exists to prevent -- and ordering within that millisecond degrades
    to the random `rand_b`. Uniqueness is never affected; that comes from 62
    random bits. Saturation needs thousands of ids inside one millisecond, which
    is far outside anything this application does: a conversation turn writes a
    handful of rows.
    """
    global _last_ms, _last_counter
    if unix_ts_ms == _last_ms:
        if _last_counter < _COUNTER_MAX:
            _last_counter += 1
    else:
        _last_ms = unix_ts_ms
        _last_counter = secrets.randbits(8)
    return _last_counter


def uuid7(at: datetime) -> UUID:
    """A time-ordered UUIDv7 whose timestamp is `at`.

    `at` must be timezone-aware; a naive datetime is a bug rather than a
    default, for the reasons in app/core/clock.py.
    """
    if at.tzinfo is None:
        raise ValueError("uuid7 requires a timezone-aware datetime")

    unix_ts_ms = int(at.timestamp() * 1000) & 0xFFFF_FFFF_FFFF

    with _lock:
        counter = _next_counter(unix_ts_ms)
    rand_b = secrets.randbits(62)

    value = unix_ts_ms << 80
    value |= _VERSION << 76
    value |= counter << 64
    value |= _VARIANT << 62
    value |= rand_b
    return UUID(int=value)


def timestamp_ms(value: UUID) -> int:
    """Extract the embedded millisecond timestamp. Used by tests and debugging."""
    return value.int >> 80
