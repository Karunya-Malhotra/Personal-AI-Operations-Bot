"""Every model must be imported here so Alembic's autogenerate can see it.

This is an explicit list rather than a filesystem scan, for the same reason the
tool registry is explicit: a model that exists but was never imported produces
a migration that silently drops its table.
"""

from app.db.base import Base
from app.db.models.system_setting import SystemSetting

__all__ = ["Base", "SystemSetting"]
