"""Token prices, and the conversion from usage to `llm_calls.cost_micros`.

Scope, per your §23: this establishes a *durable basis* for cost monitoring. It
is not billing. It does not model batch discounts, cache-write surcharges,
long-context tiers, or partner (Bedrock/Vertex) rates, and it should not grow to
-- the authority on what you were charged is the provider's invoice, and a table
in this repo that pretends otherwise would be confidently wrong.

What it is for: answering "which conversations are expensive?" and "did that
change make turns cost more?" from SQL, which needs a number that is consistent
and roughly right, not exact.

Two conventions inherited from the architecture:

  - **Integer arithmetic only** (§11.3). `cost_micros` is millionths of a
    currency unit. Floats never touch a stored amount; the division happens
    once, at the end, with `round()`.
  - **Unknown model means unknown cost, not zero cost.** A missing entry
    returns `None`, and the caller stores 0 while the usage columns still carry
    the truth. Guessing a rate would silently corrupt every aggregate built on
    top of it, and would do so invisibly.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.llm import Usage


@dataclass(frozen=True, slots=True)
class ModelRate:
    """USD per one million tokens, as integer micro-dollars.

    Stored as micros rather than floats so the multiplication below is exact:
    $5.00 / 1M tokens is 5_000_000 micros / 1M tokens.
    """

    input_micros_per_mtok: int
    output_micros_per_mtok: int


#: Anthropic list prices (first-party API), verified against the current model
#: table rather than recalled. Partner platforms (Bedrock, Vertex) bill
#: separately and are deliberately absent.
#:
#: Gemini models are **not listed**, and that is a deliberate gap rather than an
#: oversight: authoritative Google pricing could not be verified from this
#: environment, and inventing a rate is worse than admitting we do not have one.
#: Gemini calls therefore record full token usage with `cost_micros = 0` until a
#: rate is added here. See `estimate_cost_micros` for how that surfaces.
RATES: dict[str, ModelRate] = {
    "claude-opus-5": ModelRate(5_000_000, 25_000_000),
    "claude-opus-4-8": ModelRate(5_000_000, 25_000_000),
    "claude-opus-4-7": ModelRate(5_000_000, 25_000_000),
    "claude-opus-4-6": ModelRate(5_000_000, 25_000_000),
    "claude-sonnet-5": ModelRate(3_000_000, 15_000_000),
    "claude-sonnet-4-6": ModelRate(3_000_000, 15_000_000),
    "claude-haiku-4-5": ModelRate(1_000_000, 5_000_000),
    "claude-fable-5": ModelRate(10_000_000, 50_000_000),
}

_TOKENS_PER_MTOK = 1_000_000


def estimate_cost_micros(model: str, usage: Usage) -> int | None:
    """Cost in micro-dollars, or None when the rate or the usage is unknown.

    `None` is not the same as 0 and callers must keep the distinction: 0 means
    "this call was free", None means "we cannot say". The runtime stores 0 in
    `llm_calls.cost_micros` for the None case, and the token columns remain
    populated, so a later migration can backfill once a rate exists.
    """
    rate = RATES.get(model)
    if rate is None:
        return None
    if usage.input_tokens is None and usage.output_tokens is None:
        return None

    # Cached input is billed differently by every provider that offers it, and
    # this table does not model that. Counting it at the full input rate would
    # over-report; ignoring it under-reports. We fold it into input because the
    # alternative is pretending cached tokens were free.
    input_tokens = (usage.input_tokens or 0) + (usage.cached_input_tokens or 0)
    output_tokens = usage.output_tokens or 0

    total = input_tokens * rate.input_micros_per_mtok + output_tokens * rate.output_micros_per_mtok
    return round(total / _TOKENS_PER_MTOK)
