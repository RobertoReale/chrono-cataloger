from datetime import datetime, timezone

from src.models import (
    ClassifiedEntry,
    HistoryEntry,
    datetime_to_webkit_micros,
    webkit_micros_to_datetime,
)


def test_webkit_roundtrip():
    dt = datetime(2026, 7, 24, 15, 30, tzinfo=timezone.utc)
    micros = datetime_to_webkit_micros(dt)
    back = webkit_micros_to_datetime(micros)
    assert abs((back - dt).total_seconds()) < 1


def test_webkit_known_value():
    # 2021-01-01 00:00 UTC ~ 13253932800000000 microsecondi WebKit
    dt = datetime(2021, 1, 1, tzinfo=timezone.utc)
    assert datetime_to_webkit_micros(dt) == 13253932800000000


def test_history_entry_roundtrip_dict():
    e = HistoryEntry("https://x.com", "T", 3, 13000000000000000, "https://x.com")
    d = e.to_dict()
    e2 = HistoryEntry.from_dict(d)
    assert e2.url == e.url and e2.visit_count == 3


def test_classified_entry_strips_and_validates():
    c = ClassifiedEntry(categoria="  Libri  ", sintesi="  Etica di Spinoza  ", url="")
    assert c.categoria == "Libri"
    assert c.sintesi == "Etica di Spinoza"
