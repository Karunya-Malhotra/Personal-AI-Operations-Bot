"""The Anthropic adapter: the only module in this codebase that imports the SDK.

Note the filename shadows the SDK package name. That is safe -- Python 3 uses
absolute imports, so `import anthropic` below resolves to the installed package
rather than to this module -- and it matches the layout in ARCHITECTURE
(`providers/llm/ -> anthropic.py, fake.py, pricing.py`). An import contract in
pyproject.toml enforces that no module outside `app.providers.llm` may import
the SDK, so the substitution promise in app/core/llm.py is checked rather than
merely intended.

## Why `max_retries=0`

The SDK retries connection errors, 408/409/429 and 5xx twice by default. The
Agent Runtime has an explicit `MODEL_RETRY_WAIT` state for exactly those cases
(ARCHITECTURE §5.2). Leaving both on would mean up to 2xN attempts, and -- worse
-- the SDK's retries are invisible: they never reach `llm_calls`, so the trace
would under-report what happened while `latency_ms` silently absorbed the hidden
waits. One retry mechanism, in the layer that can record it.

## Why the response is validated rather than trusted

§19 treats model output as untrusted input, and §15A requires that a failed call
never become a successful empty answer. A response with no text block is a
failure, and this adapter raises rather than returning `text=""` -- which would
otherwise reach the user as the assistant confidently saying nothing.
"""

from __future__ import annotations

from typing import Literal

import anthropic
from anthropic.types import MessageParam

from app.core.clock import Clock
from app.core.conversation import Role
from app.core.llm import (
    LLMAuthError,
    LLMError,
    LLMInvalidRequest,
    LLMMalformedResponse,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
    ModelRequest,
    ModelResponse,
    StopReason,
    Usage,
)

PROVIDER_NAME = "anthropic"

#: Anthropic's stop reasons -> ours. Anything absent maps to UNKNOWN with the
#: original preserved in `raw_stop_reason`, because a stop reason we have not
#: seen before is not a reason to fail a turn that already produced text.
_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": StopReason.END_TURN,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.STOP_SEQUENCE,
    "refusal": StopReason.REFUSAL,
}


class AnthropicProvider:
    """Translates `ModelRequest` <-> the Anthropic Messages API."""

    def __init__(
        self,
        *,
        api_key: str,
        clock: Clock,
        timeout_s: float = 30.0,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        # The key arrives as a plain str, unwrapped by bootstrap from a
        # SecretStr at the composition root. This class never sees Settings --
        # §19 -- and there is no code path here that could log it.
        self._clock = clock
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key,
            timeout=timeout_s,
            max_retries=0,  # see module docstring
        )

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    async def complete(self, request: ModelRequest) -> ModelResponse:
        started = self._clock.now()
        try:
            # Explicit keyword arguments rather than **kwargs: the SDK's
            # overloads are precise, and passing a dict would silently opt out
            # of the type checking that catches a bad parameter here instead of
            # at runtime against a live API.
            message = await self._client.messages.create(
                model=request.model,
                max_tokens=request.max_tokens,
                messages=_to_sdk_messages(request),
                system=request.system if request.system is not None else anthropic.omit,
            )
        except anthropic.APIError as exc:
            raise _map_error(exc) from exc

        elapsed_ms = int((self._clock.now() - started).total_seconds() * 1000)
        return _from_sdk_message(message, model=request.model, latency_ms=elapsed_ms)


def _to_sdk_messages(request: ModelRequest) -> list[MessageParam]:
    """Build the transcript. Raises `LLMInvalidRequest` for requests we malformed."""
    if not request.messages:
        raise LLMInvalidRequest(
            "a request must contain at least one message", provider=PROVIDER_NAME
        )

    if request.temperature is not None:
        # Current Anthropic models removed the sampling parameters entirely --
        # `temperature`, `top_p` and `top_k` are rejected, and the SDK no longer
        # accepts them. `ModelRequest.temperature` stays in the vocabulary
        # because Gemini still honours it, so this is a genuine per-provider
        # capability difference rather than a field nobody uses.
        #
        # Raising rather than dropping it silently: a caller who set a
        # temperature asked for different sampling behaviour, and quietly
        # ignoring that would make the request mean something other than what
        # was written.
        raise LLMInvalidRequest(
            "current Anthropic models do not accept a temperature; leave "
            "ModelRequest.temperature unset for this provider",
            provider=PROVIDER_NAME,
        )

    messages: list[MessageParam] = []
    for turn in request.messages:
        if turn.role is Role.SYSTEM:
            # The system instruction belongs in the `system` parameter, which is
            # what keeps our instructions distinguishable from the user's
            # content (app/core/llm.py). A SYSTEM turn inside the transcript
            # means the context builder flattened that boundary, so fail loudly
            # rather than quietly re-nesting it.
            raise LLMInvalidRequest(
                "system content must be passed as ModelRequest.system, not as a transcript turn",
                provider=PROVIDER_NAME,
            )
        # An explicit literal rather than `turn.role.value`: the SDK types the
        # role as a Literal, and spelling it out is what lets mypy prove the
        # SYSTEM case really was excluded above.
        role: Literal["user", "assistant"] = "user" if turn.role is Role.USER else "assistant"
        messages.append(MessageParam(role=role, content=turn.content))
    return messages


def _from_sdk_message(message: object, *, model: str, latency_ms: int) -> ModelResponse:
    """Normalise an SDK response, validating shape before trusting it."""
    text = "".join(
        block.text
        for block in getattr(message, "content", []) or []
        if getattr(block, "type", None) == "text"
    )
    if not text:
        # Either no text block at all, or an empty one. Both are failures: the
        # alternative is the assistant appearing to answer with silence.
        raise LLMMalformedResponse("response contained no text content", provider=PROVIDER_NAME)

    raw_stop = getattr(message, "stop_reason", None)
    usage = getattr(message, "usage", None)

    return ModelResponse(
        text=text,
        stop_reason=_STOP_REASONS.get(raw_stop or "", StopReason.UNKNOWN),
        usage=Usage(
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            cached_input_tokens=getattr(usage, "cache_read_input_tokens", None),
        ),
        model=getattr(message, "model", None) or model,
        provider=PROVIDER_NAME,
        raw_stop_reason=raw_stop,
        latency_ms=latency_ms,
        provider_metadata={"message_id": str(getattr(message, "id", "") or "")},
    )


def _map_error(exc: anthropic.APIError) -> LLMError:
    """SDK exception -> our taxonomy.

    This mapping is the whole reason the runtime never learns what an HTTP
    status code is. Ordered most-specific first; the `retryable` classification
    on each class is what `MODEL_RETRY_WAIT` branches on.
    """
    if isinstance(exc, anthropic.APITimeoutError | anthropic.DeadlineExceededError):
        return LLMTimeout(str(exc), provider=PROVIDER_NAME)
    if isinstance(exc, anthropic.RateLimitError):
        return LLMRateLimited(str(exc), provider=PROVIDER_NAME, retry_after_s=_retry_after(exc))
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return LLMAuthError(str(exc), provider=PROVIDER_NAME)
    if isinstance(
        exc,
        anthropic.InternalServerError
        | anthropic.OverloadedError
        | anthropic.ServiceUnavailableError
        | anthropic.APIConnectionError,
    ):
        return LLMUnavailable(str(exc), provider=PROVIDER_NAME)
    if isinstance(
        exc,
        anthropic.BadRequestError
        | anthropic.NotFoundError
        | anthropic.UnprocessableEntityError
        | anthropic.RequestTooLargeError,
    ):
        return LLMInvalidRequest(str(exc), provider=PROVIDER_NAME)
    # An unrecognised APIError is treated as unavailable rather than as our bug:
    # a new SDK error class is far more likely to be a transport or service
    # condition than a malformed request we suddenly started sending.
    return LLMUnavailable(str(exc), provider=PROVIDER_NAME)


def _retry_after(exc: anthropic.RateLimitError) -> float | None:
    """Read `retry-after` when the response carried one."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        # Servers may send an HTTP-date instead of seconds. We do not parse that
        # here: the runtime's backoff has its own floor, so an unparseable hint
        # costs nothing beyond falling back to it.
        return None
