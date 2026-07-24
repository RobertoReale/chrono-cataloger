from datetime import datetime, timezone

from src.extractor import extract


def test_extract_reads_range(chrome_history_db):
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 31, 23, 59, tzinfo=timezone.utc)
    entries = extract(chrome_history_db, start, end)
    urls = {e.url for e in entries}
    # Solo le voci di luglio (non spinoza di agosto)
    assert any("Hegel" in e.title for e in entries)
    assert not any("spinoza" in u for u in urls)


def test_extract_excludes_out_of_range(chrome_history_db):
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc)
    entries = extract(chrome_history_db, start, end)
    assert len(entries) == 1
    assert "spinoza" in entries[0].url


def test_extract_preserves_timestamps(chrome_history_db):
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 31, 23, 59, tzinfo=timezone.utc)
    entries = extract(chrome_history_db, start, end)
    for e in entries:
        assert e.last_visit.year == 2026
        assert e.last_visit.month == 7
