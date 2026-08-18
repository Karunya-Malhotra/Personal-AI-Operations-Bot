"""Declarative base and the constraint naming convention.

The naming convention is the important part of this file, and it has to be here
from the first migration or it is worthless.

Postgres auto-generates names for unnamed constraints and indexes. Alembic's
`--autogenerate` then cannot reliably match an existing constraint to a model
change, so it produces migrations that drop-and-recreate, or silently miss the
change. Worse, the auto-generated name differs between a database built by
migrations and one built by `create_all()` in a test, so tests and production
diverge in a way that only shows up months later.

Fixing this retroactively means renaming every constraint in a live database.
Setting it now costs six lines.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
