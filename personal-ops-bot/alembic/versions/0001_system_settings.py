"""system_settings table and the environment stamp

Revision ID: 0001
Revises:
Create Date: M1A

This is the only migration in M1A. It creates no domain tables -- notes,
messages and conversations arrive in M1B/M1C -- and exists solely to stamp the
database with its environment.

Note that the stamp value comes from `Settings` at migration time. That is
unusual for a migration (data that depends on where it runs) and it is
deliberate: migrations are applied *on the host that owns the database*, so the
host's own APP_ENV is exactly the right source. The stamp is written once and
never updated, so a production database can never quietly become a dev one.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.config.settings import Settings

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    system_settings = op.create_table(
        "system_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=256), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_system_settings")),
    )

    op.bulk_insert(
        system_settings,
        [{"key": "environment", "value": Settings().app_env.value}],
    )


def downgrade() -> None:
    op.drop_table("system_settings")
