"""Construct the configured provider.

This is the small version of the Model Router deferred from ARCHITECTURE line
1150. It exists so that changing which model backs the assistant is an `.env`
edit rather than a code change: the Agent Runtime, the CLI, the domain layer and
the schema all speak `LLMProvider` and never learn which vendor answered.

Note the signature: **primitives, not `Settings`**. `app.providers` may not
import `app.config` (an import-linter contract enforces it), for the same reason
`configure_logging` takes a level and a boolean -- a provider should be
constructible by a worker, a script, or a test without building the whole
configuration object. `bootstrap` unwraps `Settings` and calls this.

`fake` is a first-class choice rather than a test-only hack. Running the whole
application against a scripted model -- no key, no network, no cost -- is how
the CLI and the runtime get exercised end to end, and how you demonstrate the
system to someone without spending anything.
"""

from __future__ import annotations

from app.core.clock import Clock
from app.core.errors import ConfigurationError
from app.core.llm import LLMProvider

#: Must agree with the validator in app/config/settings.py, which cannot import
#: this module. A unit test asserts the two sets match.
KNOWN_PROVIDERS = frozenset({"anthropic", "gemini", "fake"})


def build_llm_provider(
    *,
    provider: str,
    clock: Clock,
    api_key: str | None = None,
    timeout_s: float = 30.0,
) -> LLMProvider:
    """Return the adapter named by `provider`.

    Raises `ConfigurationError` -- not an LLM error -- when the selected
    provider has no key. That distinction matters: a missing key is a
    misconfiguration that should stop the process at boot, next to the database
    guard, rather than surface as a failed turn once someone is talking to it.

    The SDK imports are deliberately inside the branches. Importing both eagerly
    would make every process pay for two vendor SDKs to use one, and would make
    a broken install of the unused vendor break startup for the used one.
    """
    if provider not in KNOWN_PROVIDERS:
        raise ConfigurationError(
            f"unknown llm_provider {provider!r}; expected one of {sorted(KNOWN_PROVIDERS)}"
        )

    if provider == "fake":
        from app.providers.llm.fake import FakeLLM

        return FakeLLM()

    if not api_key:
        raise ConfigurationError(
            f"llm_provider is {provider!r} but no API key is configured. "
            f"Set {provider.upper()}_API_KEY in .env, or set LLM_PROVIDER=fake to run "
            f"against a scripted model with no key."
        )

    if provider == "anthropic":
        from app.providers.llm.anthropic import AnthropicProvider

        return AnthropicProvider(api_key=api_key, clock=clock, timeout_s=timeout_s)

    from app.providers.llm.gemini import GeminiProvider

    return GeminiProvider(api_key=api_key, clock=clock, timeout_s=timeout_s)
