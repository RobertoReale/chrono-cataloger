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
