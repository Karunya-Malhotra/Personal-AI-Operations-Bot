"""Cost estimation: integer-only, and honest about what it does not know."""

from __future__ import annotations

from app.core.llm import Usage
from app.providers.llm.pricing import RATES, estimate_cost_micros


def test_cost_is_computed_from_the_rate_table() -> None:
    # 1000 input at $5/Mtok = $0.005; 500 output at $25/Mtok = $0.0125.
    assert estimate_cost_micros("claude-opus-5", Usage(1000, 500)) == 17_500


def test_an_unknown_model_reports_unknown_not_free() -> None:
    """The distinction the caller must preserve: None means "we cannot say",
    0 would mean "this call was free". Guessing a rate would silently corrupt
    every aggregate built on top of it."""
    assert estimate_cost_micros("gemini-3-flash", Usage(1000, 500)) is None


def test_unknown_usage_reports_unknown() -> None:
    assert estimate_cost_micros("claude-opus-5", Usage()) is None


def test_zero_usage_is_zero_cost_not_unknown() -> None:
    assert estimate_cost_micros("claude-opus-5", Usage(0, 0)) == 0


def test_cached_input_is_counted_rather_than_treated_as_free() -> None:
    """The table does not model cache pricing. Counting cached tokens at the
    input rate over-reports slightly; ignoring them would pretend they were
    free, which is the larger error."""
    plain = estimate_cost_micros("claude-opus-5", Usage(1000, 0))
    cached = estimate_cost_micros("claude-opus-5", Usage(1000, 0, cached_input_tokens=1000))
    assert plain is not None and cached is not None
    assert cached == 2 * plain


def test_every_rate_is_an_integer() -> None:
    """§11.3: no float ever touches a stored amount."""
    for model, rate in RATES.items():
        assert isinstance(rate.input_micros_per_mtok, int), model
        assert isinstance(rate.output_micros_per_mtok, int), model


def test_result_is_always_an_int() -> None:
    cost = estimate_cost_micros("claude-opus-5", Usage(7, 3))
    assert isinstance(cost, int)
