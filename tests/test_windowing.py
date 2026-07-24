from datetime import datetime, timezone

import pytest

from src.windowing import (
    generate_windows,
    load_checkpoint,
    pending_windows,
    resolve_period,
    save_checkpoint,
)


def _d(y, m, day):
    return datetime(y, m, day, tzinfo=timezone.utc)


def test_resolve_period_range_extends_to_end_of_day():
    start, end = resolve_period(_d(2026, 1, 1), _d(2026, 1, 31), None)
    assert start == _d(2026, 1, 1)
    assert end.day == 31 and end.hour == 23


def test_resolve_period_last_days():
    now = _d(2026, 7, 24)
    start, end = resolve_period(None, None, 7, now=now)
    assert (end - start).days == 7


def test_resolve_period_requires_range_or_last_days():
    with pytest.raises(ValueError):
        resolve_period(None, None, None)


def test_resolve_period_rejects_inverted_range():
    with pytest.raises(ValueError):
        resolve_period(_d(2026, 2, 1), _d(2026, 1, 1), None)


def test_generate_windows_covers_period():
    start, end = resolve_period(_d(2026, 1, 1), _d(2026, 3, 31), None)
    windows = generate_windows(start, end, 30)
    assert windows[0].start == start
    assert windows[-1].end <= end
    # nessun gap: ogni finestra inizia dove finisce (circa) la precedente
    for a, b in zip(windows, windows[1:]):
        assert b.start > a.start


def test_checkpoint_roundtrip(tmp_path):
    p = tmp_path / "checkpoint.json"
    save_checkpoint(p, {"w1", "w2"})
    assert load_checkpoint(p) == {"w1", "w2"}


def test_pending_windows_skips_completed():
    start, end = resolve_period(_d(2026, 1, 1), _d(2026, 3, 31), None)
    windows = generate_windows(start, end, 30)
    done = {windows[0].key}
    pend = pending_windows(windows, done)
    assert windows[0] not in pend
    assert len(pend) == len(windows) - 1
