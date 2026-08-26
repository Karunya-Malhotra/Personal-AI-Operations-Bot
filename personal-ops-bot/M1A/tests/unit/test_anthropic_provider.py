"""The Anthropic adapter's translation, in both directions.

Every test here mocks the SDK client. That is not a compromise -- the adapter's
entire job *is* translation, and translation is fully observable without a
network call. A live call would add sampling non-determinism, latency and cost
to tests whose subject is "does a 429 become LLMRateLimited", which the network
has no opinion about.

The one thing that genuinely needs a real API is covered separately: see
tests/integration/test_anthropic_live.py, which skips without a key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app.core.clock import FrozenClock
from app.core.conversation import Role
from app.core.llm import (
    LLMAuthError,
    LLMInvalidRequest,
    LLMMalformedResponse,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
    ModelMessage,
    ModelRequest,
    StopReason,
)
from app.providers.llm.anthropic import AnthropicProvider

AT = datetime(2026, 8, 19, 9, 0, 0, tzinfo=UTC)
REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def sdk_message(
    *, text: str = "hello", stop_reason: str | None = "end_turn", blocks: list | None = None
) -> SimpleNamespace:
    content = blocks if blocks is not None else [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(
        id="msg_123",
        content=content,
        stop_reason=stop_reason,
        model="claude-opus-5",
        usage=SimpleNamespace(input_tokens=11, output_tokens=7, cache_read_input_tokens=3),
    )


class FakeMessages:
    def __init__(self, result: object | Exception) -> None:
        self._result = result
        self.calls: list[dict] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeClient:
    def __init__(self, result: object | Exception) -> None:
        self.messages = FakeMessages(result)


def provider(result: object | Exception) -> tuple[AnthropicProvider, FakeClient]:
    client = FakeClient(result)
    return (
        AnthropicProvider(api_key="sk-test", clock=FrozenClock(AT), client=client),  # type: ignore[arg-type]
        client,
    )


def request(*turns: tuple[Role, str], system: str | None = None, **kw) -> ModelRequest:
    return ModelRequest(
        messages=tuple(ModelMessage(r, t) for r, t in turns),
        model="claude-opus-5",
        max_tokens=1024,
        system=system,
        **kw,
    )


# -- request translation ----------------------------------------------------


async def test_transcript_and_system_are_sent_separately() -> None:
    """The boundary between our instructions and the user's content survives
    the trip to the SDK -- see app/core/llm.py for why that matters at M1C."""
    p, client = provider(sdk_message())
    await p.complete(request((Role.USER, "hi"), (Role.ASSISTANT, "hello"), system="be terse"))

    sent = client.messages.calls[0]
    assert sent["messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert sent["system"] == "be terse"
    assert sent["model"] == "claude-opus-5"
    assert sent["max_tokens"] == 1024


async def test_absent_system_is_omitted_not_sent_as_none() -> None:
    p, client = provider(sdk_message())
    await p.complete(request((Role.USER, "hi")))
    assert client.messages.calls[0]["system"] is anthropic.omit


async def test_a_system_turn_in_the_transcript_is_rejected() -> None:
    """A SYSTEM role inside `messages` means the context builder flattened the
    instruction boundary. Fail loudly rather than silently re-nesting it."""
    p, _ = provider(sdk_message())
    with pytest.raises(LLMInvalidRequest, match=r"ModelRequest\.system"):
        await p.complete(request((Role.SYSTEM, "you are helpful"), (Role.USER, "hi")))


async def test_an_empty_transcript_is_rejected() -> None:
    p, _ = provider(sdk_message())
    with pytest.raises(LLMInvalidRequest, match="at least one message"):
        await p.complete(request())


async def test_temperature_is_rejected_rather_than_dropped() -> None:
    """Current Anthropic models removed sampling parameters entirely. Gemini
    still honours temperature, so the field stays in the vocabulary -- but
    silently ignoring it here would make the request mean something other than
    what the caller wrote."""
    p, _ = provider(sdk_message())
    with pytest.raises(LLMInvalidRequest, match="temperature"):
        await p.complete(request((Role.USER, "hi"), temperature=0.7))


# -- response translation ---------------------------------------------------


async def test_text_usage_and_stop_reason_are_normalised() -> None:
    p, _ = provider(sdk_message(text="the answer"))
    response = await p.complete(request((Role.USER, "hi")))

    assert response.text == "the answer"
    assert response.stop_reason is StopReason.END_TURN
    assert response.raw_stop_reason == "end_turn"
    assert response.provider == "anthropic"
    assert response.model == "claude-opus-5"
    assert (response.usage.input_tokens, response.usage.output_tokens) == (11, 7)
    assert response.usage.cached_input_tokens == 3


async def test_multiple_text_blocks_are_concatenated() -> None:
    p, _ = provider(
        sdk_message(
            blocks=[
                SimpleNamespace(type="text", text="part one "),
                SimpleNamespace(type="text", text="part two"),
            ]
        )
    )
    assert (await p.complete(request((Role.USER, "hi")))).text == "part one part two"


async def test_non_text_blocks_are_ignored() -> None:
    p, _ = provider(
        sdk_message(
            blocks=[
                SimpleNamespace(type="thinking", thinking="hmm"),
                SimpleNamespace(type="text", text="visible"),
            ]
        )
    )
    assert (await p.complete(request((Role.USER, "hi")))).text == "visible"


async def test_an_unknown_stop_reason_does_not_fail_the_turn() -> None:
    """A stop reason we have not seen is not a reason to discard text the model
    already produced. It is recorded verbatim instead."""
    p, _ = provider(sdk_message(stop_reason="some_future_reason"))
    response = await p.complete(request((Role.USER, "hi")))
    assert response.stop_reason is StopReason.UNKNOWN
    assert response.raw_stop_reason == "some_future_reason"


async def test_refusal_is_mapped() -> None:
    p, _ = provider(sdk_message(stop_reason="refusal"))
    assert (await p.complete(request((Role.USER, "hi")))).stop_reason is StopReason.REFUSAL


@pytest.mark.parametrize(
    "blocks",
    [
        pytest.param([], id="no blocks at all"),
        pytest.param([SimpleNamespace(type="text", text="")], id="empty text block"),
        pytest.param([SimpleNamespace(type="thinking", thinking="x")], id="no text block"),
    ],
)
async def test_a_response_without_text_is_a_failure_not_an_empty_answer(blocks: list) -> None:
    """§15A: a failed call must never become a successful empty answer. Without
    this the assistant would appear to reply with silence."""
    p, _ = provider(sdk_message(blocks=blocks))
    with pytest.raises(LLMMalformedResponse):
        await p.complete(request((Role.USER, "hi")))


async def test_latency_is_measured_with_the_injected_clock() -> None:
    """§17: nothing reads the wall clock directly, so latency is deterministic
    under a FrozenClock instead of varying per run."""
    p, _ = provider(sdk_message())
    assert (await p.complete(request((Role.USER, "hi")))).latency_ms == 0


# -- error mapping ----------------------------------------------------------


@pytest.mark.parametrize(
    ("sdk_error", "expected", "retryable"),
    [
        (anthropic.APITimeoutError(request=REQUEST), LLMTimeout, True),
        (
            anthropic.APIConnectionError(message="connection reset", request=REQUEST),
            LLMUnavailable,
            True,
        ),
        (
            anthropic.InternalServerError(
                "boom", response=httpx.Response(500, request=REQUEST), body=None
            ),
            LLMUnavailable,
            True,
        ),
        (
            anthropic.AuthenticationError(
                "bad key", response=httpx.Response(401, request=REQUEST), body=None
            ),
            LLMAuthError,
            False,
        ),
        (
            anthropic.PermissionDeniedError(
                "nope", response=httpx.Response(403, request=REQUEST), body=None
            ),
            LLMAuthError,
            False,
        ),
        (
            anthropic.BadRequestError(
                "malformed", response=httpx.Response(400, request=REQUEST), body=None
            ),
            LLMInvalidRequest,
            False,
        ),
        (
            anthropic.NotFoundError(
                "no such model", response=httpx.Response(404, request=REQUEST), body=None
            ),
            LLMInvalidRequest,
            False,
        ),
    ],
)
async def test_sdk_errors_map_to_our_taxonomy(
    sdk_error: Exception, expected: type, retryable: bool
) -> None:
    """This mapping is why the Agent Runtime never learns what an HTTP status
    code is; it branches only on `retryable`."""
    p, _ = provider(sdk_error)
    with pytest.raises(expected) as exc:
        await p.complete(request((Role.USER, "hi")))
    assert exc.value.retryable is retryable
    assert exc.value.provider == "anthropic"


async def test_rate_limit_carries_retry_after() -> None:
    error = anthropic.RateLimitError(
        "slow down",
        response=httpx.Response(429, request=REQUEST, headers={"retry-after": "2.5"}),
        body=None,
    )
    p, _ = provider(error)
    with pytest.raises(LLMRateLimited) as exc:
        await p.complete(request((Role.USER, "hi")))
    assert exc.value.retry_after_s == 2.5
    assert exc.value.retryable is True


async def test_an_unparseable_retry_after_is_tolerated() -> None:
    """Servers may send an HTTP-date. The runtime has its own backoff floor, so
    an unreadable hint must not turn into a crash."""
    error = anthropic.RateLimitError(
        "slow down",
        response=httpx.Response(
            429, request=REQUEST, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}
        ),
        body=None,
    )
    p, _ = provider(error)
    with pytest.raises(LLMRateLimited) as exc:
        await p.complete(request((Role.USER, "hi")))
    assert exc.value.retry_after_s is None


async def test_sdk_retries_are_disabled() -> None:
    """The state machine owns retries (§5.2). If the SDK also retried, the
    attempts would never reach `llm_calls` and recorded latency would silently
    absorb the hidden waits."""
    real = AnthropicProvider(api_key="sk-test", clock=FrozenClock(AT))
    assert real._client.max_retries == 0
