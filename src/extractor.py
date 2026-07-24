"""History extraction from Chrome (the ``History`` SQLite database).

Chrome keeps an exclusive lock on the ``History`` file while it is running, so
we copy it to a temporary location before opening it read-only.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from .models import HistoryEntry, datetime_to_webkit_micros

_QUERY = """
SELECT url, title, visit_count, last_visit_time
FROM urls
WHERE last_visit_time BETWEEN ? AND ?
ORDER BY last_visit_time ASC
"""


def default_history_path(browser: str = "chrome") -> Path:
    """Default path of the History file, based on the operating system."""
    if browser != "chrome":
        raise ValueError(f"Browser not supported for auto-detection: {browser}")

    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA", "")
        return Path(local) / "Google" / "Chrome" / "User Data" / "Default" / "History"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Google"
            / "Chrome"
            / "Default"
            / "History"
        )
    # Linux and other unixes
    return Path.home() / ".config" / "google-chrome" / "Default" / "History"


def resolve_history_path(configured: str | None, browser: str = "chrome") -> Path:
    """Resolve the configured path (expanding ~ and variables), or the auto default."""
    if configured:
        expanded = os.path.expandvars(os.path.expanduser(configured))
        return Path(expanded)
    return default_history_path(browser)


def _copy_locked_db(src: Path) -> Path:
    """Copy the DB (and its WAL/SHM files) into a temporary directory."""
    if not src.exists():
        raise FileNotFoundError(
            f"History file not found: {src}. "
            "Check the path in the config (source.history_path)."
        )
    tmp_dir = Path(tempfile.mkdtemp(prefix="chrono_hist_"))
    dst = tmp_dir / "History"
    shutil.copy2(src, dst)
    # Copy any WAL/SHM sidecar files too, for a consistent read.
    for suffix in ("-wal", "-shm"):
        side = src.with_name(src.name + suffix)
        if side.exists():
            shutil.copy2(side, dst.with_name(dst.name + suffix))
    return dst


def extract(
    history_path: str | os.PathLike | None,
    start: datetime,
    end: datetime,
    browser: str = "chrome",
) -> list[HistoryEntry]:
    """Extract the history entries in the [start, end] range.

    Args:
        history_path: path to the History file, or None for auto-detection.
        start, end: bounds of the period (datetimes; assumed UTC if naive).
        browser: browser name (only 'chrome' is supported).

    Returns:
        A list of :class:`HistoryEntry` sorted by ascending last_visit.
    """
    src = resolve_history_path(
        str(history_path) if history_path is not None else None, browser
    )
    tmp_db = _copy_locked_db(src)

    start_micros = datetime_to_webkit_micros(start)
    end_micros = datetime_to_webkit_micros(end)

    try:
        # Opened read-only through a URI, so the copy is never altered.
        conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
        try:
            rows = conn.execute(_QUERY, (start_micros, end_micros)).fetchall()
        finally:
            conn.close()
    finally:
        _cleanup(tmp_db.parent)

    entries: list[HistoryEntry] = []
    for url, title, visit_count, last_visit in rows:
        entries.append(
            HistoryEntry(
                url=url or "",
                title=title or "",
                visit_count=int(visit_count or 0),
                last_visit_micros=int(last_visit or 0),
            )
        )
    return entries


def _cleanup(tmp_dir: Path) -> None:
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except OSError:
        pass
