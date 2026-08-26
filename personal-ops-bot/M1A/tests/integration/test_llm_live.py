"""The one test that talks to a real provider.

Everything else about the adapters is covered without a network call, because
translation is fully observable from a mocked client. What cannot be checked
that way is whether our understanding of the API is *correct* -- whether the
request shape is accepted, whether usage arrives where we read it, whether the
stop reason is the string we mapped. A mock built from our own assumptions
cannot falsify those assumptions.

So this exists, and it skips without a key. Skipping is not the same as passing,
and it is reported as skipped.

    ANTHROPIC_API_KEY=sk-... pytest tests/integration/test_llm_live.py
    LLM_LIVE_GEMINI_KEY=...  pytest tests/integration/test_llm_live.py
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from app.core.clock import SystemClock
from app.core.conversation import Role
from app.core.llm import ModelMessage, ModelRequest, StopReason

pytestmark = pytest.mark.integration

PROMPT = ModelRequest(
    messages=(ModelMessage(Role.USER, "Reply with exactly the word: pong"),),
    model="",  # filled per provider below
    max_tokens=64,
    system="You answer with a single word and nothing else.",
)


async def test_anthropic_round_trip() -> None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("no ANTHROPIC_API_KEY; live provider test skipped")

    from app.providers.llm.anthropic import AnthropicProvider

    provider = AnthropicProvider(api_key=key, clock=SystemClock())
    response = await provider.complete(
        ModelRequest(**{**PROMPT.__dict__, "model": os.environ.get("LLM_MODEL", "claude-opus-5")})
    )

    assert response.text.strip()
    assert response.provider == "anthropic"
    assert response.stop_reason in set(StopReason)
    assert response.usage.input_tokens and response.usage.input_tokens > 0
    assert response.latency_ms is not None


async def test_gemini_round_trip() -> None:
    key = os.environ.get("LLM_LIVE_GEMINI_KEY") or os.environ.get("GEMINI_API_KEY")
    if not key:
        pytest.skip("no GEMINI_API_KEY; live provider test skipped")

    from app.providers.llm.gemini import GeminiProvider

    provider = GeminiProvider(api_key=key, clock=SystemClock())
    response = await provider.complete(
        ModelRequest(
            **{**PROMPT.__dict__, "model": os.environ.get("LLM_MODEL_GEMINI", "gemini-3-flash")}
        )
    )

    assert response.text.strip()
    assert response.provider == "gemini"
    assert response.usage.input_tokens and response.usage.input_tokens > 0


def test_the_clock_used_here_is_the_real_one() -> None:
    """Guards against this file quietly becoming another mocked test: a live
    round trip that froze time would not be measuring anything real."""
    assert SystemClock().now() > datetime(2026, 1, 1, tzinfo=UTC)
