"""Message roles.

Why role is application semantics rather than presentation:

1. **It is a wire-format requirement, not a label.** The provider API accepts an
   alternating user/assistant transcript and a *separately* supplied system
   instruction. Rendering "User: ..." into one big string and sending it as a
   single user turn would still "look" right in a terminal while destroying the
   structure the model was trained on.

2. **It is the trust boundary.** System instructions are ours; user content is
   the owner's; assistant content is model-authored. ARCHITECTURE v0.3.1 §E
   turns exactly this distinction into the `Origin` enum at M1C, where
   `MODEL_GENERATED` content is excluded from facts and `USER_MESSAGE` content
   is the only thing that can create an egress grant. If role were a display
   string, that later rule would have nothing reliable to stand on.

3. **It decides what may be replayed.** Rebuilding context reads stored rows
   back; a row whose role is unknown cannot be placed in the transcript at all.

Hence a closed enum plus a database CHECK constraint, not a free string. The
same reasoning the architecture applies to `Origin` at M1C -- a value that rules
branch on gets a closed set and a constraint that keeps the database honest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


#: The roles that may be persisted as conversation turns. SYSTEM is included
#: because a future milestone may pin an instruction into a conversation, but
#: M1B builds the system instruction at request time rather than storing it --
#: see app/agent/context_builder.py for why.
PERSISTABLE_ROLES = frozenset(Role)


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    """One stored turn, as the rest of the application sees it.

    Deliberately not the SQLAlchemy model. Two reasons, both practical rather
    than purist:

    1. The context builder and the Agent Runtime run *after* the database
       session that loaded the history has closed. Handing them ORM instances
       invites `DetachedInstanceError` and accidental lazy loads at exactly the
       moment we are trying to be deterministic about what went into a prompt.
    2. `app.core` may not import SQLAlchemy (an import-linter contract enforces
       it), and this is the type the core-level context builder speaks. Mapping
       at the repository boundary is a few lines and keeps that boundary real.
    """

    id: UUID
    role: Role
    content: str
    sent_at: datetime
