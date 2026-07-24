"""(Rough) token and cost estimation for logging.

The estimates exist to give the user an order of magnitude, not to bill anyone:
tokens are approximated with the ~4 chars/token rule and the prices are
indicative (USD per million tokens, input/output).
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

_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Estimate the number of tokens in a string (~4 chars/token)."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _price_for(model: str) -> tuple[float, float]:
    m = (model or "").lower()
    for key, price in _PRICE_TABLE.items():
        if key in m:
            return price
    return (0.0, 0.0)  # local provider (ollama) or unknown model


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Estimate the cost in USD given the input/output tokens and the model."""
    in_price, out_price = _price_for(model)
    return (input_tokens / 1_000_000) * in_price + (output_tokens / 1_000_000) * out_price


class CostTracker:
    """Accumulates estimated tokens and cost over a run."""

    def __init__(self):
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0

    def add(self, model: str, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost_usd += estimate_cost_usd(model, input_tokens, output_tokens)

    def as_dict(self) -> dict:
        return {
            "estimated_input_tokens": self.input_tokens,
            "estimated_output_tokens": self.output_tokens,
            "estimated_cost_usd": round(self.cost_usd, 4),
        }
