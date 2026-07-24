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

    Also returns a helper to build WebKit timestamps from datetimes.
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

    def wk(y, m, d):
        return datetime_to_webkit_micros(datetime(y, m, d, 12, 0, tzinfo=timezone.utc))

    rows = [
        ("https://en.wikipedia.org/wiki/Hegel", "Hegel - Wikipedia", 5, wk(2026, 7, 3)),
        ("https://mail.google.com/mail/u/0", "Inbox", 40, wk(2026, 7, 4)),
        ("https://www.youtube.com/watch?v=abc123&utm_source=x", "Documentary about Marx", 2, wk(2026, 7, 5)),
        ("https://site.com/login?next=/home", "Sign in", 3, wk(2026, 7, 6)),
        ("https://books.com/spinoza", "Spinoza's Ethics", 1, wk(2026, 8, 2)),
    ]
    conn.executemany(
        "INSERT INTO urls (url, title, visit_count, last_visit_time) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db_path
