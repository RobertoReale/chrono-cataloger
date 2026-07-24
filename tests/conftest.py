"""Shared test fixtures."""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Make the src package importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import datetime_to_webkit_micros  # noqa: E402


class FakeLLMClient:
    """Fake LLM client: answers through a callback supplied by the test.

    ``responder(prompt, model, max_tokens) -> str``. Records the calls.
    """

    def __init__(self, responder):
        self._responder = responder
        self.calls: list[tuple[str, str]] = []

    def complete(self, prompt: str, model: str, max_tokens: int = 4096) -> str:
        self.calls.append((prompt, model))
        return self._responder(prompt, model, max_tokens)


@pytest.fixture
def fake_client_factory():
    return FakeLLMClient


@pytest.fixture
def chrome_history_db(tmp_path):
    """Create a Chrome-style History SQLite DB and return its path.

    Mirrors the real schema: ``urls`` holds all-time aggregates, ``visits`` holds
    one row per individual visit. The extractor reads ``visits``, so the two must
    be consistent — including the Wikipedia page, which is visited both in July
    and in August specifically to cover the windowing case.
    """
    db_path = tmp_path / "History"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE urls (
            id INTEGER PRIMARY KEY,
            url TEXT,
            title TEXT,
            visit_count INTEGER DEFAULT 0,
            typed_count INTEGER DEFAULT 0,
            last_visit_time INTEGER,
            hidden INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE visits (
            id INTEGER PRIMARY KEY,
            url INTEGER,
            visit_time INTEGER,
            from_visit INTEGER,
            transition INTEGER DEFAULT 0
        )
        """
    )

    def wk(y, m, d):
        return datetime_to_webkit_micros(datetime(y, m, d, 12, 0, tzinfo=timezone.utc))

    # (url, title, [visit dates]) — the visit list is the source of truth.
    pages = [
        ("https://en.wikipedia.org/wiki/Hegel", "Hegel - Wikipedia",
         [wk(2026, 7, 3), wk(2026, 7, 10), wk(2026, 8, 20)]),
        ("https://mail.google.com/mail/u/0", "Inbox", [wk(2026, 7, 4)]),
        ("https://www.youtube.com/watch?v=abc123&utm_source=x", "Documentary about Marx",
         [wk(2026, 7, 5), wk(2026, 7, 6)]),
        ("https://site.com/login?next=/home", "Sign in", [wk(2026, 7, 6)]),
        ("https://books.com/spinoza", "Spinoza's Ethics", [wk(2026, 8, 2)]),
    ]
    for url_id, (url, title, times) in enumerate(pages, start=1):
        conn.execute(
            "INSERT INTO urls (id, url, title, visit_count, last_visit_time) "
            "VALUES (?, ?, ?, ?, ?)",
            (url_id, url, title, len(times), max(times)),
        )
        conn.executemany(
            "INSERT INTO visits (url, visit_time) VALUES (?, ?)",
            [(url_id, t) for t in times],
        )
    conn.commit()
    conn.close()
    return db_path
