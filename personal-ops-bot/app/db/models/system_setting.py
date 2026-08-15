"""Key/value settings owned by the database itself.

M1A creates exactly one row: `environment`. It is the database's answer to the
question "which deployment am I?", and it is what makes the dev/prod guard work
(see app/db/guard.py).

Why a table rather than, say, a filename convention or a database name: the
stamp has to travel *with the data*. A dump restored into a differently-named
database keeps its stamp. A connection string that was copy-pasted from the
wrong place cannot lie about it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
