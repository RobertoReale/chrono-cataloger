from datetime import datetime, timezone

from src.classifier import classify
from src.models import HistoryEntry, datetime_to_webkit_micros
from tests.conftest import FakeLLMClient


def _cfg():
    return {
        "llm": {"model": "sonnet-model"},
        "classification": {
            "batch_size": 50,
            "categories": [
                {"name": "Libri", "description": "libri"},
                {"name": "Filosofia e Storia", "description": "idee"},
            ],
            "prompt": "Categorie:\n{categories_list}\nClassifica.",
        },
    }


def _entry(url, title):
    micros = datetime_to_webkit_micros(datetime(2026, 7, 5, tzinfo=timezone.utc))
    e = HistoryEntry(url=url, title=title, visit_count=2, last_visit_micros=micros)
    e.normalized_url = url
    return e


def test_classify_parses_and_reinjects_metadata():
    entries = [_entry("https://books/spinoza", "Etica"), _entry("https://x/hegel", "Hegel")]

    def responder(prompt, model, max_tokens):
        return (
            '[{"i":1,"categoria":"Libri","sintesi":"Etica di Spinoza","url":"https://books/spinoza"},'
            '{"i":2,"categoria":"Filosofia e Storia","sintesi":"Dialettica di Hegel","url":""}]'
        )

    client = FakeLLMClient(responder)
    out = classify(entries, client, _cfg())
    assert len(out) == 2
    assert out[0].categoria == "Libri"
    # metadati re-iniettati dalla voce sorgente
    assert out[0].last_visit_micros == entries[0].last_visit_micros
    assert out[0].normalized_url == "https://books/spinoza"


def test_classify_drops_unknown_category():
    entries = [_entry("https://x", "y")]
    client = FakeLLMClient(
        lambda *a: '[{"i":1,"categoria":"Categoria Inesistente","sintesi":"z"}]'
    )
    assert classify(entries, client, _cfg()) == []


def test_classify_case_insensitive_category_match():
    entries = [_entry("https://x", "y")]
    client = FakeLLMClient(lambda *a: '[{"i":1,"categoria":"libri","sintesi":"z"}]')
    out = classify(entries, client, _cfg())
    assert len(out) == 1
    assert out[0].categoria == "Libri"  # normalizzata al nome canonico


def test_classify_retries_on_malformed_then_succeeds():
    entries = [_entry("https://x", "y")]
    state = {"n": 0}

    def responder(prompt, model, max_tokens):
        state["n"] += 1
        if state["n"] == 1:
            return "testo non json"
        return '[{"i":1,"categoria":"Libri","sintesi":"ok"}]'

    client = FakeLLMClient(responder)
    out = classify(entries, client, _cfg())
    assert state["n"] == 2  # ha ritentato
    assert len(out) == 1


def test_classify_skips_entries_missing_index():
    entries = [_entry("https://x", "y")]
    client = FakeLLMClient(lambda *a: '[{"categoria":"Libri","sintesi":"no index"}]')
    assert classify(entries, client, _cfg()) == []
