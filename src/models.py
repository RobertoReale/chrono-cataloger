"""Modelli dati condivisi lungo la pipeline.

Uso di ``pydantic`` per l'output dell'LLM (validazione stretta) e di semplici
``dataclass`` per i record che circolano internamente e non hanno bisogno di
validazione a runtime.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, field_validator

# WebKit epoch: Chrome memorizza last_visit_time in microsecondi dal 1601-01-01 UTC.
_WEBKIT_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
_MICROS_PER_SECOND = 1_000_000


def webkit_micros_to_datetime(micros: int) -> datetime:
    """Converte un timestamp Chrome (microsecondi dal 1601-01-01) in datetime UTC."""
    return _WEBKIT_EPOCH + timedelta(microseconds=micros)


def datetime_to_webkit_micros(dt: datetime) -> int:
    """Converte un datetime in timestamp Chrome (microsecondi dal 1601-01-01)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt - _WEBKIT_EPOCH
    return int(delta.total_seconds() * _MICROS_PER_SECOND)


@dataclass
class HistoryEntry:
    """Una voce grezza (o pulita) di cronologia."""

    url: str
    title: str
    visit_count: int
    last_visit_micros: int  # timestamp WebKit originale, mai perso lungo la pipeline

    # Popolato dal cleaner:
    normalized_url: str = ""

    @property
    def last_visit(self) -> datetime:
        return webkit_micros_to_datetime(self.last_visit_micros)

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "visit_count": self.visit_count,
            "last_visit_micros": self.last_visit_micros,
            "normalized_url": self.normalized_url,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HistoryEntry":
        return cls(
            url=d["url"],
            title=d.get("title", ""),
            visit_count=int(d.get("visit_count", 1)),
            last_visit_micros=int(d["last_visit_micros"]),
            normalized_url=d.get("normalized_url", ""),
        )


class ClassifiedEntry(BaseModel):
    """Voce classificata restituita dall'LLM e validata."""

    categoria: str = Field(..., min_length=1)
    sintesi: str = Field(..., min_length=1)
    url: str = ""

    # Metadati aggiunti localmente dopo la classificazione (non provengono dall'LLM):
    last_visit_micros: int | None = None
    normalized_url: str | None = None

    @field_validator("categoria", "sintesi", mode="before")
    @classmethod
    def _strip(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v
