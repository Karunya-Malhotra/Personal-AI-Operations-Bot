"""A deterministic in-process LLM provider.

## Why integration tests cannot simply call Anthropic every time (your §28)

Four reasons, and the first is the one that actually decides it:

1. **Non-determinism.** The model is sampled. Asserting "the assistant replied
   X" against a real provider is asserting something that is not guaranteed to
   be true twice, so the test either becomes vague enough to pass anything or
   flaky enough to be ignored. The Agent Runtime's tests are about *its*
   behaviour -- did it retry, did it record the call, did it reach COMPLETE --
   and a sampled response adds noise to every one of them.

2. **Failure paths are otherwise untestable.** §15 requires that a provider
   outage produce a `FAILED` run and an honest error rather than a fake empty
   success. You cannot make Anthropic time out on demand. Here it is
   `FakeLLM(outcomes=[LLMTimeout("...")])`.

3. **Speed and cost.** The suite runs on every commit. Network latency and
   per-token billing both scale with how often you run it, which is a direct
   tax on running it often.

4. **It needs no key.** A test suite that cannot run without a secret is a
   suite that new machines and CI cannot run at all.

This lives in `app/`, not `tests/`, for the same reason `FrozenClock` does: it
is part of the contract of the `LLMProvider` interface, and later milestones
(the runtime, the CLI, eventually the scheduler) all need the same one. A fake
that lives only in the test tree gets forked three times and drifts.

## What it deliberately does not do

It does not attempt to be a language model. Responses are scripted or derived
from the input by a fixed rule. Anything cleverer would tempt tests to assert
on generated wording, which is exactly the coupling this exists to avoid.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping

from app.core.conversation import Role
from app.core.llm import (
    LLMError,
    ModelRequest,
    ModelResponse,
    StopReason,
    Usage,
)


#: Deterministic stand-in for real tokenisation: whitespace-separated words.
#: Not accurate, and not trying to be -- what tests need is that usage is
#: *stable* and non-zero so cost accounting has something real to carry.
def _estimate_tokens(text: str) -> int:
    return max(1, len(text.split()))


class FakeLLM:
    """Scripted `LLMProvider`. Every response is decided before the call.

    Resolution order for each call:

    1. the next queued outcome, if any (`outcomes`),
    2. a `script` entry matching the last user message exactly,
    3. `default`.

    A queued outcome that is an exception is raised, which is how failure paths
    are exercised.
    """

    def __init__(
        self,
        *,
        outcomes: Iterable[str | Exception | ModelResponse] | None = None,
        script: Mapping[str, str] | None = None,
        default: str = "This is a test response.",
        model: str = "fake-model-1",
        name: str = "fake",
        stop_reason: StopReason = StopReason.END_TURN,
        latency_ms: int = 0,
    ) -> None:
        self._outcomes: deque[str | Exception | ModelResponse] = deque(outcomes or ())
        self._script = dict(script or {})
        self._default = default
        self._model = model
        self._name = name
        self._stop_reason = stop_reason
        self._latency_ms = latency_ms
        #: Every request received, in order. Tests assert on what the context
        #: builder actually sent -- which is the only way to check that history
        #: was replayed, that the system instruction stayed separate, and that
        #: no secret leaked into the prompt.
        self.calls: list[ModelRequest] = []

    @property
    def name(self) -> str:
        return self._name

    def queue(self, *outcomes: str | Exception | ModelResponse) -> None:
        """Append outcomes after construction, for multi-turn tests."""
        self._outcomes.extend(outcomes)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_request(self) -> ModelRequest | None:
        return self.calls[-1] if self.calls else None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        # Recorded before dispatch so a call that raises still shows up. A
        # retry test needs to see that the runtime called twice, and it would
        # not if failures were invisible here.
        self.calls.append(request)

        if self._outcomes:
            outcome = self._outcomes.popleft()
            if isinstance(outcome, BaseException):
                raise outcome
            if isinstance(outcome, ModelResponse):
                return outcome
            return self._respond(request, outcome)

        last_user = next(
            (m.content for m in reversed(request.messages) if m.role is Role.USER), None
        )
        if last_user is not None and last_user in self._script:
            return self._respond(request, self._script[last_user])
        return self._respond(request, self._default)

    def _respond(self, request: ModelRequest, text: str) -> ModelResponse:
        prompt_text = " ".join(m.content for m in request.messages)
        if request.system:
            prompt_text = f"{request.system} {prompt_text}"
        return ModelResponse(
            text=text,
            stop_reason=self._stop_reason,
            usage=Usage(
                input_tokens=_estimate_tokens(prompt_text),
                output_tokens=_estimate_tokens(text),
                cached_input_tokens=0,
            ),
            model=self._model,
            provider=self._name,
            latency_ms=self._latency_ms,
            provider_metadata={"fake": "true"},
        )


def always_failing(error: LLMError, *, times: int = 1000) -> FakeLLM:
    """A provider that fails every call. For outage tests (§26 Scenario 4)."""
    return FakeLLM(outcomes=[error] * times)
