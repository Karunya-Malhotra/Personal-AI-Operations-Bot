"""Every model must be imported here so Alembic's autogenerate can see it.

This is an explicit list rather than a filesystem scan, for the same reason the
tool registry is explicit: a model that exists but was never imported produces
a migration that silently drops its table.
"""

from app.db.base import Base
from app.db.models.agent_run import AgentRun
from app.db.models.conversation import Conversation, Message
from app.db.models.llm_call import LlmCall
from app.db.models.system_setting import SystemSetting
from app.db.models.user import Identity, User

__all__ = [
    "AgentRun",
    "Base",
    "Conversation",
    "Identity",
    "LlmCall",
    "Message",
    "SystemSetting",
    "User",
]
