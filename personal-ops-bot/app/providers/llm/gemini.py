"""The Google Gemini adapter.

Its real job in this codebase is to be the *second* implementation. An interface
with one adapter is an interface shaped like that adapter, and nobody finds out
until the second one arrives. Gemini's API differs from Anthropic's in three
ways that would each have leaked upward through a weaker boundary:

  - **Role vocabulary.** Gemini calls the assistant `"model"`, not
    `"assistant"`. Mapped below; `app.core.conversation.Role` is unchanged.
  - **System instruction placement.** It is a field on the *config* object, not
    a request parameter. Because `ModelRequest.system` is a separate field
    rather than a prepended transcript turn, that difference is a one-line
    translation instead of a rework.
  - **Stop reasons.** Gemini reports a much larger enum, including several
    distinct safety outcomes (`SAFETY`, `PROHIBITED_CONTENT`, `BLOCKLIST`,
    `SPII`, `RECITATION`) where Anthropic reports one `refusal`. All of them
    collapse to `StopReason.REFUSAL`, and the original string is kept in
    `raw_stop_reason` so nothing is lost from the trace.

## A note on the free tier, deliberately left in the code

Google's unpaid tier trains on submitted content and permits human review of
inputs and outputs, and its terms say not to submit personal information. This
application's entire payload is personal notes, expenses and attendance. Nothing
here prevents a free-tier key being used -- that is the operator's decision --
but it is not a decision anyone should make without knowing, so it is recorded
where the code lives rather than only in a design document.
"""

from __future__ import annotations

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

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

PROVIDER_NAME = "gemini"

#: Gemini's role names. Ours -> theirs.
_ROLES: dict[Role, str] = {Role.USER: "user", Role.ASSISTANT: "model"}

#: Gemini's FinishReason -> ours. Every safety-flavoured outcome becomes
#: REFUSAL: they differ in *why* the model declined, which is diagnostic detail
#: preserved in `raw_stop_reason`, not a difference the runtime acts on.
_STOP_REASONS: dict[str, StopReason] = {
    "STOP": StopReason.END_TURN,
    "MAX_TOKENS": StopReason.MAX_TOKENS,
    "SAFETY": StopReason.REFUSAL,
    "RECITATION": StopReason.REFUSAL,
    "BLOCKLIST": StopReason.REFUSAL,
    "PROHIBITED_CONTENT": StopReason.REFUSAL,
    "SPII": StopReason.REFUSAL,
    "IMAGE_SAFETY": StopReason.REFUSAL,
}


class GeminiProvider:
    """Translates `ModelRequest` <-> the Gemini `generate_content` API."""

    def __init__(
        self,
        *,
        api_key: str,
        clock: Clock,
        timeout_s: float = 30.0,
        client: genai.Client | None = None,
    ) -> None:
        self._clock = clock
        self._timeout_s = timeout_s
        # Like the Anthropic adapter, this receives one unwrapped secret and
        # never sees Settings (§19).
        self._client = client or genai.Client(api_key=api_key)

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    async def complete(self, request: ModelRequest) -> ModelResponse:
        started = self._clock.now()
        try:
            response = await self._client.aio.models.generate_content(
                model=request.model,
                contents=_to_contents(request),
                config=_to_config(request, self._timeout_s),
            )
        except genai_errors.APIError as exc:
            raise _map_error(exc) from exc
        except TimeoutError as exc:
            raise LLMTimeout(str(exc), provider=PROVIDER_NAME) from exc

        elapsed_ms = int((self._clock.now() - started).total_seconds() * 1000)
        return _from_response(response, model=request.model, latency_ms=elapsed_ms)


def _to_contents(request: ModelRequest) -> list[genai_types.Content]:
    if not request.messages:
        raise LLMInvalidRequest(
            "a request must contain at least one message", provider=PROVIDER_NAME
        )

    contents: list[genai_types.Content] = []
    for turn in request.messages:
        role = _ROLES.get(turn.role)
        if role is None:
            # SYSTEM reaching the transcript means the context builder flattened
            # the instruction boundary; see the Anthropic adapter for why that
            # is an error rather than something to paper over.
            raise LLMInvalidRequest(
                "system content must be passed as ModelRequest.system, not as a transcript turn",
                provider=PROVIDER_NAME,
            )
        contents.append(genai_types.Content(role=role, parts=[genai_types.Part(text=turn.content)]))
    return contents


def _to_config(request: ModelRequest, timeout_s: float) -> genai_types.GenerateContentConfig:
    return genai_types.GenerateContentConfig(
        system_instruction=request.system,
        max_output_tokens=request.max_tokens,
        temperature=request.temperature,
        # Gemini's HTTP timeout is milliseconds, ours is seconds.
        http_options=genai_types.HttpOptions(timeout=int(timeout_s * 1000)),
    )


def _from_response(response: object, *, model: str, latency_ms: int) -> ModelResponse:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        # A prompt blocked before generation returns zero candidates. That is a
        # refusal, not an answer, and must not surface as empty text (§15A).
        raise LLMMalformedResponse("response contained no candidates", provider=PROVIDER_NAME)

    candidate = candidates[0]
    content = getattr(candidate, "content", None)
    parts = getattr(content, "parts", None) or []
    text = "".join(getattr(part, "text", None) or "" for part in parts)

    raw_stop = getattr(candidate, "finish_reason", None)
    raw_stop_name = getattr(raw_stop, "name", None) or (str(raw_stop) if raw_stop else None)

    if not text:
        raise LLMMalformedResponse(
            f"response contained no text content (finish_reason={raw_stop_name})",
            provider=PROVIDER_NAME,
        )

    usage = getattr(response, "usage_metadata", None)
    return ModelResponse(
        text=text,
        stop_reason=_STOP_REASONS.get(raw_stop_name or "", StopReason.UNKNOWN),
        usage=Usage(
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
            cached_input_tokens=getattr(usage, "cached_content_token_count", None),
        ),
        model=getattr(response, "model_version", None) or model,
        provider=PROVIDER_NAME,
        raw_stop_reason=raw_stop_name,
        latency_ms=latency_ms,
        provider_metadata={"response_id": str(getattr(response, "response_id", "") or "")},
    )


def _map_error(exc: genai_errors.APIError) -> LLMError:
    """Gemini errors carry an HTTP status in `.code`, so map on that.

    Unlike the Anthropic SDK there is no per-status exception class -- only
    `ClientError` (4xx) and `ServerError` (5xx) -- so the status is the only
    thing precise enough to classify a 429 apart from a 400.
    """
    status = getattr(exc, "code", None)
    message = str(getattr(exc, "message", None) or exc)

    if status == 429:
        return LLMRateLimited(message, provider=PROVIDER_NAME)
    if status in (401, 403):
        return LLMAuthError(message, provider=PROVIDER_NAME)
    if status == 408 or status == 504:
        return LLMTimeout(message, provider=PROVIDER_NAME)
    if isinstance(exc, genai_errors.ServerError) or (status is not None and status >= 500):
        return LLMUnavailable(message, provider=PROVIDER_NAME)
    if status is not None and 400 <= status < 500:
        return LLMInvalidRequest(message, provider=PROVIDER_NAME)
    return LLMUnavailable(message, provider=PROVIDER_NAME)
