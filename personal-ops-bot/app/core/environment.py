"""Which deployment this process is.

This lives in `core` rather than in `config` for a specific dependency reason:
the database guard (app/db/guard.py) compares the value the *database* claims
to be against the value the *process* claims to be. The guard therefore needs
this type, but `db` must not import `config` -- the engine is built in
bootstrap and handed down, so the database layer never reads settings.

Putting the shared vocabulary in `core` is what lets both sides speak it
without either depending on the other.
"""

from __future__ import annotations

from enum import StrEnum


class Environment(StrEnum):
    DEV = "dev"
    PROD = "prod"
