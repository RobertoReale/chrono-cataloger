from datetime import datetime, timezone

from src.extractor import extract


def test_extract_reads_range(chrome_history_db):
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 31, 23, 59, tzinfo=timezone.utc)
    entries = extract(chrome_history_db, start, end)
    urls = {e.url for e in entries}
    # Only the July entries (not the Spinoza one from August)
    assert any("Hegel" in e.title for e in entries)
    assert not any("spinoza" in u for u in urls)


def test_extract_excludes_out_of_range(chrome_history_db):
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc)
    entries = extract(chrome_history_db, start, end)
    urls = {e.url for e in entries}
    assert any("spinoza" in u for u in urls)
    assert not any("youtube" in u for u in urls)


def test_extract_finds_pages_whose_last_visit_is_outside_the_window(chrome_history_db):
    """The Hegel page was last visited in August but also visited in July.

    Reading urls.last_visit_time would have hidden it from the July window.
    """
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 31, 23, 59, tzinfo=timezone.utc)
    entries = extract(chrome_history_db, start, end)
    assert any("Hegel" in e.title for e in entries)


def test_visit_count_is_scoped_to_the_window(chrome_history_db):
    """Hegel: 3 visits all-time, 2 of them in July."""
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 31, 23, 59, tzinfo=timezone.utc)
    hegel = next(e for e in extract(chrome_history_db, start, end) if "Hegel" in e.title)
    assert hegel.visit_count == 2


def test_extract_preserves_timestamps(chrome_history_db):
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 31, 23, 59, tzinfo=timezone.utc)
    entries = extract(chrome_history_db, start, end)
    for e in entries:
        assert e.last_visit.year == 2026
        assert e.last_visit.month == 7
