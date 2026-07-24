from datetime import datetime, timezone

from src.models import HistoryEntry, datetime_to_webkit_micros
from src.triage import _parse_verdicts, triage
from tests.conftest import FakeLLMClient


def _entry(url, title):
    micros = datetime_to_webkit_micros(datetime(2026, 7, 1, tzinfo=timezone.utc))
    e = HistoryEntry(url=url, title=title, visit_count=1, last_visit_micros=micros)
    e.normalized_url = url
    return e


def test_parse_verdicts_basic():
    raw = '[{"i":1,"v":"relevant"},{"i":2,"v":"noise"}]'
    v = _parse_verdicts(raw, 2)
    assert v == {1: True, 2: False}


def test_parse_verdicts_tolerates_surrounding_text():
    raw = 'Here is the result: [{"i":1,"v":"relevant"}] done.'
    assert _parse_verdicts(raw, 1) == {1: True}


def test_parse_verdicts_malformed_returns_empty():
    assert _parse_verdicts("not json", 2) == {}


def test_triage_keeps_only_relevant():
    entries = [_entry("https://a.com", "Hegel"), _entry("https://b.com", "Inbox")]

    def responder(prompt, model, max_tokens):
        return '[{"i":1,"v":"relevant"},{"i":2,"v":"noise"}]'

    client = FakeLLMClient(responder)
    cfg = {"enabled": True, "batch_size": 200, "prompt": "triage criteria"}
    out = triage(entries, client, cfg, "haiku-model")
    assert len(out) == 1
    assert out[0].title == "Hegel"


def test_triage_disabled_passthrough():
    entries = [_entry("https://a.com", "x")]
    client = FakeLLMClient(lambda *a: "")
    out = triage(entries, client, {"enabled": False}, "m")
    assert out == entries
    assert client.calls == []


def test_triage_keeps_batch_on_unparseable_response():
    entries = [_entry("https://a.com", "x"), _entry("https://b.com", "y")]
    client = FakeLLMClient(lambda *a: "a response without json")
    cfg = {"enabled": True, "batch_size": 200, "prompt": "triage criteria"}
    out = triage(entries, client, cfg, "m")
    # precaution: unparseable batch -> everything is kept
    assert len(out) == 2


def test_triage_survives_prose_around_the_array():
    entries = [_entry("https://a", "A"), _entry("https://b", "B")]
    client = FakeLLMClient(
        lambda *a: 'Verdicts [one per entry]:\n[{"i":1,"v":"relevant"},{"i":2,"v":"noise"}]'
    )
    out = triage(entries, client, {"enabled": True, "batch_size": 10, "prompt": "p"}, "m")
    assert [e.url for e in out] == ["https://a"]


def test_triage_warns_when_it_keeps_an_unreadable_batch():
    entries = [_entry("https://a", "A")]
    client = FakeLLMClient(lambda *a: "no idea")
    warnings = []
    out = triage(
        entries, client, {"enabled": True, "batch_size": 10, "prompt": "p"}, "m",
        on_warning=warnings.append,
    )
    assert len(out) == 1  # kept, as before
    assert warnings and "unreadable" in warnings[0]


def test_triage_warns_about_entries_the_model_skipped():
    """A partial answer keeps the missing entries — that must not be silent.

    Each one reaches the paid stage unfiltered, so the only other place it would
    show up is the bill.
    """
    entries = [_entry(f"https://{c}", c.upper()) for c in "abc"]
    client = FakeLLMClient(lambda *a: '[{"i":1,"v":"noise"},{"i":2,"v":"relevant"}]')
    warnings = []
    out = triage(
        entries, client, {"enabled": True, "batch_size": 10, "prompt": "p"}, "m",
        on_warning=warnings.append,
    )
    # entry 3 has no verdict: kept, as the precaution intends
    assert [e.url for e in out] == ["https://b", "https://c"]
    assert warnings and "no verdict for 1 of 3" in warnings[0]


def test_triage_says_nothing_when_the_model_answers_in_full():
    entries = [_entry("https://a", "A"), _entry("https://b", "B")]
    client = FakeLLMClient(lambda *a: '[{"i":1,"v":"relevant"},{"i":2,"v":"noise"}]')
    warnings = []
    triage(entries, client, {"enabled": True, "batch_size": 10, "prompt": "p"}, "m",
           on_warning=warnings.append)
    assert warnings == []
