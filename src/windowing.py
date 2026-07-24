"""Splitting the requested period into internal processing windows.

The windows are independent of the output granularity (``--group-by``): their
only purpose is to never process a whole year in one go. Each completed window
is recorded in ``state/checkpoint.json``, so a new run resumes where the
previous one stopped instead of reprocessing everything.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class Window:
    """A [start, end) processing time window."""

    start: datetime
    end: datetime

    @property
    def key(self) -> str:
        """Stable key used in the checkpoint (bounds as ISO dates)."""
        return f"{self.start.date().isoformat()}_{self.end.date().isoformat()}"


def resolve_period(
    from_date: datetime | None,
    to_date: datetime | None,
    last_days: int | None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Determine [start, end] from the explicit range or from ``last_days``.

    The bounds are normalized to UTC; ``end`` covers the whole final day.
    """
    now = now or datetime.now(timezone.utc)

    if last_days is not None:
        if last_days < 1:
            raise ValueError("last_days must be >= 1")
        end = now
        start = now - timedelta(days=last_days)
        return _as_utc(start), _as_utc(end)

    if from_date is None or to_date is None:
        raise ValueError("Specify --from and --to, or --last-days.")

    start = _as_utc(from_date)
    # end extended to the end of the day, to include every visit on the 'to' day.
    end = _as_utc(to_date)
    if end.hour == 0 and end.minute == 0 and end.second == 0:
        end = end + timedelta(days=1) - timedelta(microseconds=1)
    if end < start:
        raise ValueError("The 'to' date precedes the 'from' date.")
    return start, end


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def generate_windows(
    start: datetime,
    end: datetime,
    window_size_days: int,
) -> list[Window]:
    """Split [start, end] into consecutive windows of ``window_size_days`` days."""
    if window_size_days < 1:
        raise ValueError("window_size_days must be >= 1")
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
    """Return the set of window keys that have already been completed."""
    p = Path(path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data.get("completed_windows", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_checkpoint(path: str | Path, completed: set[str]) -> None:
    """Save the set of completed windows (atomic write)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"completed_windows": sorted(completed)}
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def pending_windows(windows: list[Window], completed: set[str]) -> list[Window]:
    """Filter out the windows already completed, preserving the order."""
    return [w for w in windows if w.key not in completed]
