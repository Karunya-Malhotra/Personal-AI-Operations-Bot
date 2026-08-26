"""The Gemini adapter.

These tests carry a second job beyond checking Gemini: they are the evidence
that `app/core/llm.py` is a real abstraction rather than a thin renaming of one
vendor's API. Gemini names the assistant `"model"`, puts the system instruction
on a config object, and reports five distinct safety outcomes where Anthropic
reports one. If the boundary were drawn wrongly, at least one of those would
have forced a change to core -- none did.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from google.genai import errors as genai_errors

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
from app.providers.llm.gemini import GeminiProvider

AT = datetime(2026, 8, 19, 9, 0, 0, tzinfo=UTC)


def sdk_response(
    *, text: str = "hello", finish: str | None = "STOP", candidates: list | None = None
) -> SimpleNamespace:
    if candidates is None:
        candidates = [
            SimpleNamespace(
                content=SimpleNamespace(parts=[SimpleNamespace(text=text)]),
                finish_reason=SimpleNamespace(name=finish) if finish else None,
            )
        ]
    return SimpleNamespace(
        candidates=candidates,
        usage_metadata=SimpleNamespace(
            prompt_token_count=13,
            candidates_token_count=5,
            cached_content_token_count=2,
        ),
        model_version="gemini-3-flash",
        response_id="resp_abc",
    )


class FakeModels:
    def __init__(self, result: object | Exception) -> None:
        self._result = result
        self.calls: list[dict] = []

    async def generate_content(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeClient:
    def __init__(self, result: object | Exception) -> None:
        self.aio = SimpleNamespace(models=FakeModels(result))


def provider(result: object | Exception) -> tuple[GeminiProvider, FakeClient]:
    client = FakeClient(result)
    return (
        GeminiProvider(api_key="g-test", clock=FrozenClock(AT), client=client),  # type: ignore[arg-type]
        client,
    )


def request(*turns: tuple[Role, str], system: str | None = None, **kw) -> ModelRequest:
    return ModelRequest(
        messages=tuple(ModelMessage(r, t) for r, t in turns),
        model="gemini-3-flash",
        max_tokens=1024,
        system=system,
        **kw,
    )


# -- request translation ----------------------------------------------------


async def test_assistant_turns_become_gemini_model_turns() -> None:
    """Gemini's role vocabulary differs from ours. The mapping lives here, and
    `app.core.conversation.Role` is unchanged by it."""
    p, client = provider(sdk_response())
    await p.complete(request((Role.USER, "hi"), (Role.ASSISTANT, "hello")))

    contents = client.aio.models.calls[0]["contents"]
    assert [c.role for c in contents] == ["user", "model"]
    assert [c.parts[0].text for c in contents] == ["hi", "hello"]


async def test_system_instruction_goes_on_the_config_not_the_transcript() -> None:
    """Because ModelRequest keeps `system` as its own field, this vendor
    difference is one line here instead of a change to the interface."""
    p, client = provider(sdk_response())
    await p.complete(request((Role.USER, "hi"), system="be terse"))

    call = client.aio.models.calls[0]
    assert call["config"].system_instruction == "be terse"
    assert all("be terse" not in c.parts[0].text for c in call["contents"])


async def test_generation_parameters_are_passed_through() -> None:
    """Unlike current Anthropic models, Gemini still accepts a temperature."""
    p, client = provider(sdk_response())
    await p.complete(request((Role.USER, "hi"), temperature=0.2))

    config = client.aio.models.calls[0]["config"]
    assert config.max_output_tokens == 1024
    assert config.temperature == 0.2


async def test_timeout_is_converted_to_milliseconds() -> None:
    """Ours is seconds; Gemini's http_options is milliseconds."""
    client = FakeClient(sdk_response())
    p = GeminiProvider(api_key="g", clock=FrozenClock(AT), timeout_s=12.5, client=client)  # type: ignore[arg-type]
    await p.complete(request((Role.USER, "hi")))
    assert client.aio.models.calls[0]["config"].http_options.timeout == 12500


async def test_a_system_turn_in_the_transcript_is_rejected() -> None:
    p, _ = provider(sdk_response())
    with pytest.raises(LLMInvalidRequest, match=r"ModelRequest\.system"):
        await p.complete(request((Role.SYSTEM, "you are helpful"), (Role.USER, "hi")))


async def test_an_empty_transcript_is_rejected() -> None:
    p, _ = provider(sdk_response())
    with pytest.raises(LLMInvalidRequest, match="at least one message"):
        await p.complete(request())


# -- response translation ---------------------------------------------------


async def test_text_usage_and_stop_reason_are_normalised() -> None:
    p, _ = provider(sdk_response(text="the answer"))
    response = await p.complete(request((Role.USER, "hi")))

    assert response.text == "the answer"
    assert response.stop_reason is StopReason.END_TURN
    assert response.raw_stop_reason == "STOP"
    assert response.provider == "gemini"
    assert (response.usage.input_tokens, response.usage.output_tokens) == (13, 5)
    assert response.usage.cached_input_tokens == 2


@pytest.mark.parametrize(
    "finish", ["SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"]
)
async def test_every_safety_outcome_collapses_to_refusal(finish: str) -> None:
    """Gemini distinguishes five refusal flavours where Anthropic reports one.
    The runtime does not act on the difference, so they normalise to REFUSAL --
    and the specific reason survives in `raw_stop_reason` for the trace."""
    p, _ = provider(sdk_response(finish=finish))
    response = await p.complete(request((Role.USER, "hi")))
    assert response.stop_reason is StopReason.REFUSAL
    assert response.raw_stop_reason == finish


async def test_max_tokens_is_mapped() -> None:
    p, _ = provider(sdk_response(finish="MAX_TOKENS"))
    assert (await p.complete(request((Role.USER, "hi")))).stop_reason is StopReason.MAX_TOKENS


async def test_an_unknown_finish_reason_does_not_fail_the_turn() -> None:
    p, _ = provider(sdk_response(finish="SOME_NEW_REASON"))
    response = await p.complete(request((Role.USER, "hi")))
    assert response.stop_reason is StopReason.UNKNOWN
    assert response.raw_stop_reason == "SOME_NEW_REASON"


async def test_a_blocked_prompt_returns_no_candidates_and_is_a_failure() -> None:
    """Gemini answers a blocked prompt with zero candidates. That is a refusal,
    not an empty answer, and §15A forbids letting it read as success."""
    p, _ = provider(sdk_response(candidates=[]))
    with pytest.raises(LLMMalformedResponse, match="no candidates"):
        await p.complete(request((Role.USER, "hi")))


async def test_a_candidate_without_text_is_a_failure() -> None:
    p, _ = provider(
        sdk_response(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(parts=[]),
                    finish_reason=SimpleNamespace(name="SAFETY"),
                )
            ]
        )
    )
    with pytest.raises(LLMMalformedResponse, match="SAFETY"):
        await p.complete(request((Role.USER, "hi")))


# -- error mapping ----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected", "retryable"),
    [
        (429, LLMRateLimited, True),
        (401, LLMAuthError, False),
        (403, LLMAuthError, False),
        (408, LLMTimeout, True),
        (400, LLMInvalidRequest, False),
        (404, LLMInvalidRequest, False),
        (500, LLMUnavailable, True),
        (503, LLMUnavailable, True),
    ],
)
async def test_status_codes_map_to_our_taxonomy(
    status: int, expected: type, retryable: bool
) -> None:
    """Gemini has no per-status exception class -- only ClientError/ServerError
    -- so `.code` is the only thing precise enough to tell a 429 from a 400."""
    error_cls = genai_errors.ServerError if status >= 500 else genai_errors.ClientError
    p, _ = provider(error_cls(status, {"error": {"message": "boom"}}))
    with pytest.raises(expected) as exc:
        await p.complete(request((Role.USER, "hi")))
    assert exc.value.retryable is retryable
    assert exc.value.provider == "gemini"


async def test_a_transport_timeout_is_mapped() -> None:
    p, _ = provider(TimeoutError("read timed out"))
    with pytest.raises(LLMTimeout):
        await p.complete(request((Role.USER, "hi")))
