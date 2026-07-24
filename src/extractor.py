"""Estrazione della cronologia da Chrome (database SQLite ``History``).

Chrome mantiene un lock esclusivo sul file ``History`` mentre e' in esecuzione,
quindi lo copiamo in una posizione temporanea prima di aprirlo in sola lettura.
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
    """Percorso di default del file History in base al sistema operativo."""
    if browser != "chrome":
        raise ValueError(f"Browser non supportato per l'auto-rilevamento: {browser}")

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
    # Linux e altri unix
    return Path.home() / ".config" / "google-chrome" / "Default" / "History"


def resolve_history_path(configured: str | None, browser: str = "chrome") -> Path:
    """Risolve il percorso configurato (con espansione di ~ e variabili) o l'auto-default."""
    if configured:
        expanded = os.path.expandvars(os.path.expanduser(configured))
        return Path(expanded)
    return default_history_path(browser)


def _copy_locked_db(src: Path) -> Path:
    """Copia il DB (e i suoi file WAL/SHM) in una directory temporanea."""
    if not src.exists():
        raise FileNotFoundError(
            f"File History non trovato: {src}. "
            "Verifica il percorso in config (source.history_path)."
        )
    tmp_dir = Path(tempfile.mkdtemp(prefix="chrono_hist_"))
    dst = tmp_dir / "History"
    shutil.copy2(src, dst)
    # Copia anche gli eventuali sidecar WAL/SHM per una lettura coerente.
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
    """Estrae le voci di cronologia nel range [start, end].

    Args:
        history_path: percorso al file History, o None per auto-rilevamento.
        start, end: estremi del periodo (datetime; se naive assunti UTC).
        browser: nome browser (solo 'chrome' supportato).

    Returns:
        Lista di :class:`HistoryEntry` ordinate per last_visit crescente.
    """
    src = resolve_history_path(
        str(history_path) if history_path is not None else None, browser
    )
    tmp_db = _copy_locked_db(src)

    start_micros = datetime_to_webkit_micros(start)
    end_micros = datetime_to_webkit_micros(end)

    try:
        # Apertura in sola lettura tramite URI, per non alterare la copia.
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
