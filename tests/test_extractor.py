import shutil
import sqlite3
from datetime import datetime, timezone

from src.extractor import extract
from src.models import datetime_to_webkit_micros


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


# --- WAL journalling (how Chrome actually keeps the file) ----------------- #
def _wal_db(folder, keep_open=True):
    """A WAL-mode History whose newest rows live in the -wal sidecar."""
    path = folder / "History"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        "CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT,"
        " visit_count INTEGER, last_visit_time INTEGER);"
        "CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);"
    )
    conn.commit()
    t = datetime_to_webkit_micros(datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc))
    conn.execute("INSERT INTO urls VALUES (1, 'https://x.test/a', 'A', 1, ?)", (t,))
    conn.execute("INSERT INTO visits VALUES (1, 1, ?)", (t,))
    conn.commit()  # committed into the WAL, not yet checkpointed into History
    return path, conn


def _july(path):
    return extract(path, datetime(2026, 7, 1, tzinfo=timezone.utc),
                   datetime(2026, 7, 31, tzinfo=timezone.utc))


def test_extract_reads_rows_that_are_still_in_the_wal(tmp_path):
    """Chrome is open: the newest visits are in the sidecar, not in History."""
    path, conn = _wal_db(tmp_path)
    try:
        assert path.with_name("History-wal").exists()
        assert [e.url for e in _july(path)] == ["https://x.test/a"]
    finally:
        conn.close()


def test_extract_reads_a_wal_db_with_no_shm(tmp_path):
    """After a crash the -shm can be missing; a read-only open must still cope."""
    path, conn = _wal_db(tmp_path)
    try:
        copy = tmp_path / "copy"
        copy.mkdir()
        shutil.copy2(path, copy / "History")
        shutil.copy2(path.with_name("History-wal"), copy / "History-wal")
        assert [e.url for e in _july(copy / "History")] == ["https://x.test/a"]
    finally:
        conn.close()
