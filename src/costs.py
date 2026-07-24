"""(Rough) token and cost estimation for logging.

The estimates exist to give the user an order of magnitude, not to bill anyone:
tokens are approximated with the ~4 chars/token rule and the prices are
indicative (USD per million tokens, input/output).

Cost is reported ONLY when it is meaningful — that is, when the provider bills
per token *and* the model matches the price table. Ollama runs locally and
``claude_code`` draws on a flat subscription, so for those a dollar figure would
be a fabrication; the tracker returns ``None`` instead of a misleading ``0.0``.
"""
from __future__ import annotations

# Indicative prices in USD per 1M tokens (input, output). Matched as a substring
# of the model name; used only for the estimate in the log.
_PRICE_TABLE = {
    "haiku": (1.00, 5.00),
    "sonnet": (3.00, 15.00),
    "opus": (5.00, 25.00),
    "fable": (10.00, 50.00),
}

# Providers billed per token. The others (local models, subscription-backed CLI)
# have no per-token price to quote.
_METERED_PROVIDERS = {"anthropic", "openai"}

_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Estimate the number of tokens in a string (~4 chars/token)."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _price_for(model: str) -> tuple[float, float] | None:
    """Indicative (input, output) price, or None if the model is not in the table."""
    m = (model or "").lower()
    for key, price in _PRICE_TABLE.items():
        if key in m:
            return price
    return None


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float | None:
    """Estimate the cost in USD, or None if the model has no known price."""
    price = _price_for(model)
    if price is None:
        return None
    in_price, out_price = price
    return (input_tokens / 1_000_000) * in_price + (output_tokens / 1_000_000) * out_price


class CostTracker:
    """Accumulates estimated tokens over a run, and cost when it is meaningful.

    ``provider`` is the configured ``llm.provider``: it decides whether a dollar
    figure is quotable at all.
    """

    def __init__(self, provider: str = "anthropic"):
        self.provider = provider
        self.metered = provider in _METERED_PROVIDERS
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd: float | None = 0.0 if self.metered else None
        # True as soon as one call used a model with no price in the table:
        # the total is then a lower bound, not an estimate of the whole run.
        self.partial_cost = False

    def add(self, model: str, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        if not self.metered:
            return
        cost = estimate_cost_usd(model, input_tokens, output_tokens)
        if cost is None:
            self.partial_cost = True
        else:
            self.cost_usd = (self.cost_usd or 0.0) + cost

    def as_dict(self) -> dict:
        return {
            "estimated_input_tokens": self.input_tokens,
            "estimated_output_tokens": self.output_tokens,
            "estimated_cost_usd": (
                round(self.cost_usd, 4) if self.cost_usd is not None else None
            ),
            "cost_note": self._note(),
        }

    def _note(self) -> str:
        if not self.metered:
            return f"provider {self.provider!r} is not billed per token"
        if self.partial_cost:
            return "lower bound: some models have no price in the table"
        return "indicative estimate (~4 chars/token, list prices)"
