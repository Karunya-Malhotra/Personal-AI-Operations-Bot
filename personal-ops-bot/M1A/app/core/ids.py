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
    12 bits  rand_a
     2 bits  variant (10)
    62 bits  rand_b

`at` is a parameter rather than a call to `time.time()` for the same reason
`Clock` exists (see app/core/clock.py): a repository generating an id passes
`clock.now()`, so under a FrozenClock the generated ids are deterministic and
time-ordered. Tests that assert "these rows came back in creation order" then
mean something. A module that silently read the wall clock would quietly
undermine every frozen-clock test that touches a primary key.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from uuid import UUID

_VERSION = 0x7
_VARIANT = 0b10


def uuid7(at: datetime) -> UUID:
    """A time-ordered UUIDv7 whose timestamp is `at`.

    `at` must be timezone-aware; a naive datetime is a bug rather than a
    default, for the reasons in app/core/clock.py.
    """
    if at.tzinfo is None:
        raise ValueError("uuid7 requires a timezone-aware datetime")

    unix_ts_ms = int(at.timestamp() * 1000) & 0xFFFF_FFFF_FFFF

    # One draw, split: 12 bits of rand_a and 62 bits of rand_b.
    entropy = secrets.randbits(74)
    rand_a = entropy >> 62
    rand_b = entropy & ((1 << 62) - 1)

    value = unix_ts_ms << 80
    value |= _VERSION << 76
    value |= rand_a << 64
    value |= _VARIANT << 62
    value |= rand_b
    return UUID(int=value)


def timestamp_ms(value: UUID) -> int:
    """Extract the embedded millisecond timestamp. Used by tests and debugging."""
    return value.int >> 80
