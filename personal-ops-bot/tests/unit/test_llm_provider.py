"""The provider boundary: its vocabulary, its error taxonomy, and FakeLLM."""

from __future__ import annotations

import dataclasses

import pytest

from app.core.conversation import Role
from app.core.llm import (
    LLMAuthError,
    LLMError,
    LLMInvalidRequest,
    LLMMalformedResponse,
    LLMProvider,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    StopReason,
    Usage,
)
from app.providers.llm.fake import FakeLLM, always_failing


def request(*turns: str, system: str | None = None) -> ModelRequest:
    return ModelRequest(
        messages=tuple(ModelMessage(Role.USER, t) for t in turns),
        model="fake-model-1",
        max_tokens=256,
        system=system,
    )


# -- the vocabulary ---------------------------------------------------------


def test_fake_llm_satisfies_the_provider_protocol() -> None:
    """Structural typing means this is the only check that the fake and the
    real adapter are actually interchangeable."""
    provider: LLMProvider = FakeLLM()
    assert provider.name == "fake"


def test_retryable_classification_matches_the_state_machine() -> None:
    """ARCHITECTURE §5.2 sends 429 / 5xx / timeout to MODEL_RETRY_WAIT and
    nothing else. The runtime reads `retryable`, so this is the list."""
    assert LLMTimeout("x").retryable is True
    assert LLMUnavailable("x").retryable is True
    assert LLMRateLimited("x").retryable is True

    assert LLMInvalidRequest("x").retryable is False
    assert LLMAuthError("x").retryable is False
    assert LLMMalformedResponse("x").retryable is False


def test_error_kind_is_the_class_name_not_the_message() -> None:
    """`llm_calls.error_kind` must stay stable when someone improves wording."""
    assert LLMTimeout("deadline exceeded after 30s").kind == "LLMTimeout"
    assert LLMRateLimited("slow down", retry_after_s=2.5).retry_after_s == 2.5


def test_every_error_is_an_llm_error() -> None:
    """The runtime catches one base class; a sibling that escaped it would
    crash a turn instead of failing it cleanly."""
    for cls in (
        LLMTimeout,
        LLMUnavailable,
        LLMRateLimited,
        LLMInvalidRequest,
        LLMAuthError,
        LLMMalformedResponse,
    ):
        assert issubclass(cls, LLMError)


def test_unknown_usage_is_distinct_from_zero_usage() -> None:
    """ "The provider did not say" and "it used none" are different facts; a
    trace that conflates them under-reports cost."""
    assert Usage().total_tokens is None
    assert Usage(input_tokens=0, output_tokens=0).total_tokens == 0
    assert Usage(input_tokens=10, output_tokens=5).total_tokens == 15


def test_tool_use_is_not_in_the_stop_reason_vocabulary() -> None:
    """M1B sends no tools, so no provider can return it. Listing a reason the
    system cannot produce would be a lie in the trace."""
    assert "tool_use" not in {s.value for s in StopReason}


def test_model_request_carries_no_configuration_or_secrets() -> None:
    """§19, made structural.

    The provider gets messages and generation parameters. If a `settings`,
    `database_url` or `api_key` field ever appears here, the object that
    crosses the provider boundary starts carrying things the provider has no
    business seeing -- which is exactly how a DSN ends up in a prompt.
    """
    fields = {f.name for f in dataclasses.fields(ModelRequest)}
    assert fields == {"messages", "model", "max_tokens", "system", "temperature"}


# -- FakeLLM behaviour ------------------------------------------------------


async def test_scripted_response_is_returned_for_a_matching_turn() -> None:
    fake = FakeLLM(script={"Hello": "Hello! This is a test response."})
    response = await fake.complete(request("Hello"))
    assert response.text == "Hello! This is a test response."
    assert response.stop_reason is StopReason.END_TURN
    assert response.provider == "fake"


async def test_default_is_used_when_nothing_matches() -> None:
    fake = FakeLLM(default="fallback")
    assert (await fake.complete(request("anything"))).text == "fallback"


async def test_outcomes_are_consumed_in_order() -> None:
    """Multi-turn tests depend on this being a queue, not a set."""
    fake = FakeLLM(outcomes=["first", "second"])
    assert (await fake.complete(request("a"))).text == "first"
    assert (await fake.complete(request("b"))).text == "second"
    # Queue exhausted: falls through to the default.
    assert (await fake.complete(request("c"))).text == "This is a test response."


async def test_queued_exceptions_are_raised() -> None:
    """The failure paths §15 requires, which a real provider cannot be asked
    to produce on demand."""
    fake = FakeLLM(outcomes=[LLMTimeout("deadline exceeded")])
    with pytest.raises(LLMTimeout):
        await fake.complete(request("hello"))


async def test_failed_calls_are_still_recorded() -> None:
    """A retry test asserts the runtime called twice. It could not if a
    failure left no trace on the fake."""
    fake = FakeLLM(outcomes=[LLMUnavailable("503"), "recovered"])
    with pytest.raises(LLMUnavailable):
        await fake.complete(request("hi"))
    assert (await fake.complete(request("hi"))).text == "recovered"
    assert fake.call_count == 2


async def test_empty_response_is_representable() -> None:
    """§10 asks for an empty response as a controlled case. It must be
    expressible so the runtime can be tested for refusing to treat it as a
    successful answer."""
    fake = FakeLLM(outcomes=[""])
    assert (await fake.complete(request("hi"))).text == ""


async def test_malformed_response_is_representable() -> None:
    fake = FakeLLM(outcomes=[LLMMalformedResponse("no content blocks")])
    with pytest.raises(LLMMalformedResponse):
        await fake.complete(request("hi"))


async def test_always_failing_keeps_failing() -> None:
    """§26 Scenario 4: a sustained outage, not a single blip."""
    fake = always_failing(LLMUnavailable("provider down"))
    for _ in range(5):
        with pytest.raises(LLMUnavailable):
            await fake.complete(request("hi"))


async def test_the_request_is_recorded_for_inspection() -> None:
    """Tests assert on what was actually sent: that history was replayed, and
    that the system instruction stayed out of the transcript."""
    fake = FakeLLM()
    await fake.complete(request("first", "second", system="you are helpful"))

    sent = fake.last_request
    assert sent is not None
    assert [m.content for m in sent.messages] == ["first", "second"]
    assert sent.system == "you are helpful"
    assert all(m.role is Role.USER for m in sent.messages)


async def test_usage_is_deterministic_and_non_zero() -> None:
    """Cost accounting needs something real to carry, and needs it to be the
    same on every run."""
    fake = FakeLLM(script={"hello there": "a b c"})
    first = await fake.complete(request("hello there"))
    second = await fake.complete(request("hello there"))
    assert first.usage == second.usage
    assert first.usage.output_tokens == 3
    assert first.usage.input_tokens is not None and first.usage.input_tokens > 0


async def test_a_prepared_response_object_passes_through_untouched() -> None:
    """Lets a test pin an exact stop reason or usage the fake would not invent,
    e.g. a MAX_TOKENS truncation."""
    prepared = ModelResponse(
        text="truncated",
        stop_reason=StopReason.MAX_TOKENS,
        usage=Usage(input_tokens=1000, output_tokens=256),
        model="fake-model-1",
        provider="fake",
    )
    fake = FakeLLM(outcomes=[prepared])
    assert await fake.complete(request("hi")) is prepared
