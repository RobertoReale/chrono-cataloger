"""Data models shared across the pipeline.

``pydantic`` is used for the LLM output (strict validation) and plain
``dataclass`` objects for the records that only circulate internally and need no
runtime validation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, field_validator

# WebKit epoch: Chrome stores last_visit_time in microseconds since 1601-01-01 UTC.
_WEBKIT_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
_MICROS_PER_SECOND = 1_000_000


def webkit_micros_to_datetime(micros: int) -> datetime:
    """Convert a Chrome timestamp (microseconds since 1601-01-01) to a UTC datetime."""
    return _WEBKIT_EPOCH + timedelta(microseconds=micros)


def datetime_to_webkit_micros(dt: datetime) -> int:
    """Convert a datetime to a Chrome timestamp (microseconds since 1601-01-01)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt - _WEBKIT_EPOCH
    return int(delta.total_seconds() * _MICROS_PER_SECOND)


@dataclass
class HistoryEntry:
    """A raw (or cleaned) history entry."""

    url: str
    title: str
    visit_count: int
    last_visit_micros: int  # original WebKit timestamp, never lost along the pipeline

    # Filled in by the cleaner:
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
    """A classified entry returned by the LLM, after validation."""

    category: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    url: str = ""

    # Metadata added locally after classification (it does not come from the LLM):
    last_visit_micros: int | None = None
    normalized_url: str | None = None

    @field_validator("category", "summary", mode="before")
    @classmethod
    def _strip(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v
