"""Turning stored conversation into a model request, deterministically.

Determinism is the requirement, not an aspiration: the same conversation and the
same settings must produce byte-identical input, or `prompt_digest` means
nothing and "why did it answer that?" becomes unanswerable. So this module is a
pure function of its arguments -- it performs no I/O, reads no clock, and holds
no state.

## The context window rule (your §22)

The window is not infinite and this module does not pretend otherwise. The rule
is deliberately the simplest one that is *observable*:

    include the most recent `window_messages` turns; drop the rest.

That is it. No summarisation (M3), no relevance ranking (M4), no dropping from
the middle. What makes it acceptable rather than lossy-and-silent is that every
prompt records what it left out -- `messages_total`, `messages_included` and
`messages_dropped` go into `llm_calls.context_summary`, so a conversation whose
early turns stopped being visible says so in its own trace.

The failure behaviour is equally plain: nothing raises. A conversation longer
than the window still answers, using its tail. The alternative -- refusing to
answer once a conversation gets long -- would be a worse product and would still
need this bookkeeping to explain itself.

**What this does not do yet:** it counts *messages*, not tokens. A window of 40
short turns and 40 very long ones are treated alike, so a pathological
conversation could still exceed a model's context and be rejected by the
provider (surfacing as `LLMInvalidRequest`, not as silent truncation). Token-
aware budgeting needs a real tokeniser per provider and belongs with the
retrieval work in M4. Stated here rather than discovered later.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.agent.prompts import PROMPT_VERSION, SYSTEM_PROMPT
from app.core.conversation import ConversationMessage
from app.core.llm import ModelMessage, ModelRequest


@dataclass(frozen=True, slots=True)
class BuiltContext:
    """A model request plus the record of how it was assembled."""

    request: ModelRequest
    #: Goes verbatim into `llm_calls.context_summary`. Counts and identifiers
    #: only -- never message content, which already lives in `messages` under
    #: the owner's control (see app/db/models/llm_call.py).
    summary: dict[str, object]
    #: SHA-256 over the exact rendered request. Answers "was this input
    #: identical to the previous attempt?" without retaining the input.
    prompt_digest: str


def build_context(
    history: list[ConversationMessage],
    *,
    model: str,
    max_tokens: int,
    window_messages: int,
    total_messages: int | None = None,
    system_prompt: str = SYSTEM_PROMPT,
) -> BuiltContext:
    """Assemble the request for one turn.

    `history` must already be in chronological order -- the repository
    guarantees that with `ORDER BY (sent_at, id)`, which is why the UUIDv7
    counter in app/core/ids.py matters here rather than being a detail.
    """
    included = history[-window_messages:] if window_messages > 0 else []
    total = total_messages if total_messages is not None else len(history)

    messages = tuple(
        ModelMessage(role=message.role, content=message.content) for message in included
    )
    request = ModelRequest(
        messages=messages,
        model=model,
        max_tokens=max_tokens,
        # Separate from the transcript, always. This is the boundary between our
        # instructions and the owner's words, and the thing v0.3.1 §E builds
        # `Origin` on at M1C.
        system=system_prompt,
    )

    summary: dict[str, object] = {
        "prompt_version": PROMPT_VERSION,
        "window_messages": window_messages,
        "messages_total": total,
        "messages_included": len(included),
        "messages_dropped": max(0, total - len(included)),
        "chars_included": sum(len(m.content) for m in included),
    }
    return BuiltContext(request=request, summary=summary, prompt_digest=digest_request(request))


def digest_request(request: ModelRequest) -> str:
    """A stable fingerprint of exactly what will be sent.

    Field-separated with a delimiter that cannot appear in the parts, so that a
    message ending in "x" followed by one starting with "y" cannot hash the same
    as a single message "xy". A digest that collided on adjacent turns would
    quietly report two different prompts as identical.
    """
    parts = [
        f"model={request.model}",
        f"max_tokens={request.max_tokens}",
        f"system={request.system or ''}",
    ]
    parts.extend(f"{m.role.value}={m.content}" for m in request.messages)
    payload = "\x1e".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
