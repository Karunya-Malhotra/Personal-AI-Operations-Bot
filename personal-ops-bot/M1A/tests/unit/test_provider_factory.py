"""Provider selection: the part that makes swapping vendors a config change."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config.settings import Settings
from app.core.clock import FrozenClock
from app.core.errors import ConfigurationError
from app.core.llm import LLMProvider
from app.providers.llm import KNOWN_PROVIDERS, build_llm_provider

AT = datetime(2026, 8, 19, 9, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(AT)


@pytest.mark.parametrize(
    ("provider", "api_key", "expected_name"),
    [
        ("fake", None, "fake"),
        ("anthropic", "sk-test", "anthropic"),
        ("gemini", "g-test", "gemini"),
    ],
)
def test_each_provider_can_be_built(
    provider: str, api_key: str | None, expected_name: str, clock: FrozenClock
) -> None:
    built: LLMProvider = build_llm_provider(provider=provider, clock=clock, api_key=api_key)
    assert built.name == expected_name


@pytest.mark.parametrize("provider", ["anthropic", "gemini"])
def test_a_missing_key_fails_at_construction_not_at_the_first_turn(
    provider: str, clock: FrozenClock
) -> None:
    """A missing key is a misconfiguration. It belongs next to the database
    guard at boot, not surfacing as a failed turn once someone is talking."""
    with pytest.raises(ConfigurationError) as exc:
        build_llm_provider(provider=provider, clock=clock, api_key=None)
    assert provider.upper() in str(exc.value)
    assert "LLM_PROVIDER=fake" in str(exc.value)


def test_fake_needs_no_key(clock: FrozenClock) -> None:
    """Running the whole application with no key and no network is a
    first-class mode, not a test-only hack."""
    assert build_llm_provider(provider="fake", clock=clock).name == "fake"


def test_an_unknown_provider_is_refused(clock: FrozenClock) -> None:
    with pytest.raises(ConfigurationError, match="unknown llm_provider"):
        build_llm_provider(provider="chatgpt", clock=clock, api_key="x")


def test_settings_and_factory_agree_on_the_provider_set(clean_env: None) -> None:
    """`config` cannot import `providers` (an import-linter contract), so the
    allowed set is written twice. This is the test that keeps the copies
    honest -- the same enum-vs-CHECK-constraint pattern as v0.3.1 §E.2.
    """
    for provider in KNOWN_PROVIDERS:
        assert Settings(llm_provider=provider).llm_provider == provider

    with pytest.raises(ValueError, match="llm_provider must be one of"):
        Settings(llm_provider="not-a-provider")


def test_the_error_message_names_the_env_var_to_set(clock: FrozenClock) -> None:
    """A configuration error that does not say what to do is a puzzle."""
    with pytest.raises(ConfigurationError) as exc:
        build_llm_provider(provider="gemini", clock=clock)
    assert "GEMINI_API_KEY" in str(exc.value)
