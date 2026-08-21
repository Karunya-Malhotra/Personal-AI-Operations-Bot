"""The LLM provider boundary: what the rest of the application is allowed to know.

## Why the Agent Runtime may not call the Anthropic SDK directly (your §28)

Three reasons, in the order they will actually bite:

1. **Testability, immediately.** The runtime's interesting behaviour is retry,
   failure classification, budget and state transitions. If it constructs an
   `anthropic.AsyncAnthropic` itself, every one of those tests needs the network,
   a key, and money -- so in practice they do not get written. Against this
   interface `FakeLLM` makes them deterministic and free (see providers/llm/fake.py).

2. **Failure classification, soon.** The runtime needs one question answered:
   *is this failure worth retrying?* That is a provider-specific judgement about
   HTTP status codes and SDK exception types. Answering it inside the runtime
   means an `except anthropic.APIStatusError` in the middle of the state
   machine, and a second provider means a second such branch. Here it is the
   adapter's job, and the runtime branches on `LLMError.retryable`.

3. **Substitution, eventually.** ARCHITECTURE §12 keeps a second provider as a
   live option. That is only cheap if nothing above this line ever saw an
   Anthropic type.

## What crosses this boundary, and what must not

`ModelRequest` carries messages, a system instruction, a model name and a token
cap. It does **not** carry `Settings`, a database URL, or credentials of any
kind. That is your §19 requirement made structural: a provider is constructed
with the one secret it needs and receives per-call data that contains none.
There is deliberately no "pass the config through" convenience anywhere here,
because that is precisely the shortcut that later leaks a DSN into a prompt.

`ModelResponse.text` is **untrusted input** (§19). At M1B the model can only
produce text and nothing executes it, but the adapter still validates shape
rather than trusting the SDK to have done so.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from app.core.conversation import Role


class StopReason(StrEnum):
    """Why generation stopped, normalised across providers.

    `TOOL_USE` is absent on purpose: M1B sends no tools, so a provider cannot
    return it, and listing a reason the system can never produce would be a lie
    in the trace vocabulary. It arrives with tools at M1C.
    """

    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    REFUSAL = "refusal"
    #: The provider returned something this version does not know about. Mapped
    #: rather than raised: a new stop reason is not a reason to fail a turn that
    #: already produced text. The original string is kept in
    #: `ModelResponse.raw_stop_reason` so it is visible in the trace.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """One transcript turn as the provider will see it."""

    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """Everything a provider needs, and nothing else.

    `system` is separate from `messages` rather than prepended as a turn. That
    is not cosmetic: it is the boundary between our instructions and the user's
    content, and it is what v0.3.1 §E later builds `Origin` on. Flattening it
    into the transcript would erase the distinction at exactly the layer that
    must preserve it.
    """

    messages: tuple[ModelMessage, ...]
    model: str
    max_tokens: int
    system: str | None = None
    temperature: float | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts as reported by the provider.

    Every field is optional because "the provider did not tell us" and "it used
    zero tokens" are different facts, and a trace that conflates them will
    quietly under-report cost. `None` means unknown.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """A completed model call, in this application's own vocabulary."""

    text: str
    stop_reason: StopReason
    usage: Usage
    model: str
    provider: str
    #: The provider's own string, preserved when it did not map cleanly.
    raw_stop_reason: str | None = None
    latency_ms: int | None = None
    #: Small, non-secret provider details worth keeping in the trace (a request
    #: id, say). Never credentials, and never the raw response body.
    provider_metadata: Mapping[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Errors
#
# The taxonomy exists so the runtime can ask one question -- "retry?" -- without
# knowing anything about HTTP. `retryable` is a class attribute rather than an
# isinstance chain in the state machine, so adding a provider means adding a
# mapping in that adapter, not editing the runtime.
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base for every failure crossing the provider boundary."""

    #: Whether the Agent Runtime may transition to MODEL_RETRY_WAIT.
    retryable: bool = False

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider

    @property
    def kind(self) -> str:
        """Stable label for `llm_calls.error_kind`. Uses the class name so the
        trace does not depend on message wording."""
        return type(self).__name__


class LLMTimeout(LLMError):
    """The call exceeded its deadline. Retryable: the request may not have
    reached the provider at all."""

    retryable = True


class LLMUnavailable(LLMError):
    """Connection failure or a 5xx. The provider's problem, likely transient."""

    retryable = True


class LLMRateLimited(LLMError):
    """429. Retryable, and the provider may have told us how long to wait."""

    retryable = True

    def __init__(
        self, message: str, *, provider: str | None = None, retry_after_s: float | None = None
    ) -> None:
        super().__init__(message, provider=provider)
        self.retry_after_s = retry_after_s


class LLMInvalidRequest(LLMError):
    """A 4xx that is our fault -- malformed messages, an impossible token cap.

    Deliberately not retryable: replaying an identical bad request wastes a
    turn's budget and produces the identical failure.
    """


class LLMAuthError(LLMError):
    """Bad or missing credentials. Not retryable; a human must fix it."""


class LLMMalformedResponse(LLMError):
    """The provider returned a shape we cannot use.

    Separate from `LLMInvalidRequest` because it fails in the opposite
    direction: our request was fine and the *response* was not. Not retryable
    by default -- a provider returning nonsense will usually do it again -- and
    it is the error that stops the runtime from treating an empty or malformed
    result as a successful empty answer (§15A).
    """


class LLMProvider(Protocol):
    """The one method the runtime is allowed to call.

    A Protocol rather than an ABC so a provider needs no import from this
    module to satisfy it -- which keeps the dependency pointing inward and lets
    a test define a stand-in inline.
    """

    @property
    def name(self) -> str:
        """Stable identifier recorded in `llm_calls.provider`."""
        ...

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Run one model call, or raise an `LLMError` subclass."""
        ...
