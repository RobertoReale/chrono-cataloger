"""Stima (approssimativa) di token e costo per il logging.

Le stime servono a dare all'utente un ordine di grandezza, non a fatturare:
i token sono approssimati con la regola ~4 caratteri/token e i prezzi sono
indicativi (USD per milione di token, input/output).
"""
from __future__ import annotations

# Prezzi indicativi USD per 1M token (input, output). Match per sottostringa
# sul nome modello; usati solo per la stima nel log.
_PRICE_TABLE = {
    "haiku": (1.00, 5.00),
    "sonnet": (3.00, 15.00),
    "opus": (5.00, 25.00),
    "fable": (10.00, 50.00),
}

_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Stima il numero di token da una stringa (~4 caratteri/token)."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _price_for(model: str) -> tuple[float, float]:
    m = (model or "").lower()
    for key, price in _PRICE_TABLE.items():
        if key in m:
            return price
    return (0.0, 0.0)  # provider locale (ollama) o modello sconosciuto


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Stima il costo in USD dati i token di input/output e il modello."""
    in_price, out_price = _price_for(model)
    return (input_tokens / 1_000_000) * in_price + (output_tokens / 1_000_000) * out_price


class CostTracker:
    """Accumula token e costo stimati lungo un'esecuzione."""

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
            "input_tokens_stimati": self.input_tokens,
            "output_tokens_stimati": self.output_tokens,
            "costo_usd_stimato": round(self.cost_usd, 4),
        }
