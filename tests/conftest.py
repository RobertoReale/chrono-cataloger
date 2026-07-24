"""Fixture condivise per i test."""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Rendi importabile il pacchetto src.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import datetime_to_webkit_micros  # noqa: E402


class FakeLLMClient:
    """Client LLM finto: risponde in base a callback fornite dal test.

    ``responder(prompt, model, max_tokens) -> str``. Registra le chiamate.
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
    """Crea un DB SQLite in stile Chrome History e ritorna il percorso.

    Ritorna anche un helper per costruire timestamp WebKit da datetime.
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
        ("https://mail.google.com/mail/u/0", "Posta in arrivo", 40, wk(2026, 7, 4)),
        ("https://www.youtube.com/watch?v=abc123&utm_source=x", "Documentario su Marx", 2, wk(2026, 7, 5)),
        ("https://site.com/login?next=/home", "Accedi", 3, wk(2026, 7, 6)),
        ("https://books.com/spinoza", "Etica di Spinoza", 1, wk(2026, 8, 2)),
    ]
    conn.executemany(
        "INSERT INTO urls (url, title, visit_count, last_visit_time) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db_path
