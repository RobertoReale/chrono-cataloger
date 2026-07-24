"""Suddivisione del periodo richiesto in finestre interne di elaborazione.

Le finestre sono indipendenti dalla granularita' di output (``--group-by``):
servono solo a non processare mai un intero anno in un colpo solo. Ogni finestra
completata viene registrata in ``state/checkpoint.json`` così un rilancio riparte
da dove si era interrotto senza rielaborare.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class Window:
    """Finestra temporale [start, end) di elaborazione."""

    start: datetime
    end: datetime

    @property
    def key(self) -> str:
        """Chiave stabile usata nel checkpoint (estremi in ISO date)."""
        return f"{self.start.date().isoformat()}_{self.end.date().isoformat()}"


def resolve_period(
    from_date: datetime | None,
    to_date: datetime | None,
    last_days: int | None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Determina [start, end] dal range esplicito o da ``last_days``.

    Gli estremi sono normalizzati a UTC; ``end`` copre l'intera giornata finale.
    """
    now = now or datetime.now(timezone.utc)

    if last_days is not None:
        if last_days < 1:
            raise ValueError("last_days deve essere >= 1")
        end = now
        start = now - timedelta(days=last_days)
        return _as_utc(start), _as_utc(end)

    if from_date is None or to_date is None:
        raise ValueError("Specificare --from e --to, oppure --last-days.")

    start = _as_utc(from_date)
    # end esteso a fine giornata per includere tutte le visite del giorno 'to'.
    end = _as_utc(to_date)
    if end.hour == 0 and end.minute == 0 and end.second == 0:
        end = end + timedelta(days=1) - timedelta(microseconds=1)
    if end < start:
        raise ValueError("La data 'to' precede la data 'from'.")
    return start, end


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def generate_windows(
    start: datetime,
    end: datetime,
    window_size_days: int,
) -> list[Window]:
    """Divide [start, end] in finestre consecutive di ``window_size_days`` giorni."""
    if window_size_days < 1:
        raise ValueError("window_size_days deve essere >= 1")
    windows: list[Window] = []
    cursor = start
    step = timedelta(days=window_size_days)
    while cursor <= end:
        w_end = min(cursor + step - timedelta(microseconds=1), end)
        windows.append(Window(cursor, w_end))
        cursor = cursor + step
    return windows


# --------------------------------------------------------------------------- #
# Checkpoint
# --------------------------------------------------------------------------- #
def load_checkpoint(path: str | Path) -> set[str]:
    """Restituisce l'insieme delle chiavi di finestra gia' completate."""
    p = Path(path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data.get("completed_windows", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_checkpoint(path: str | Path, completed: set[str]) -> None:
    """Salva l'insieme delle finestre completate (scrittura atomica)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"completed_windows": sorted(completed)}
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def pending_windows(windows: list[Window], completed: set[str]) -> list[Window]:
    """Filtra le finestre non ancora completate, preservando l'ordine."""
    return [w for w in windows if w.key not in completed]
