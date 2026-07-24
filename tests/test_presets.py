import pytest

from src.cleaner import clean
from src.config import DEFAULTS, _validate
from src.models import HistoryEntry, datetime_to_webkit_micros
from src.presets import domains_for, effective_domain_blacklist, load_presets, preset_names

from datetime import datetime, timezone


def _entry(url):
    micros = datetime_to_webkit_micros(datetime(2026, 7, 1, tzinfo=timezone.utc))
    return HistoryEntry(url=url, title="t", visit_count=1, last_visit_micros=micros)


def test_presets_file_is_loadable_and_non_empty():
    groups = load_presets()
    assert "search" in groups
    assert "google.com" in groups["search"]
    assert all(groups[name] for name in groups), "no group may be empty"


def test_domains_for_dedups_and_ignores_unknown_groups():
    out = domains_for(["social", "aggregators", "does-not-exist"])
    assert "pinterest.com" in out  # listed in both groups
    assert out.count("pinterest.com") == 1


def test_effective_blacklist_merges_own_list_and_presets():
    out = effective_domain_blacklist(
        {"domain_blacklist": ["mysite.local", "GOOGLE.com"], "blacklist_presets": ["search"]}
    )
    assert "mysite.local" in out
    assert out.count("google.com") == 1  # own entry, case-normalized, not duplicated


def test_presets_are_off_by_default():
    assert DEFAULTS["filtering"]["blacklist_presets"] == []


def test_clean_applies_presets():
    entries = [_entry("https://www.google.com/search?q=x"), _entry("https://arxiv.org/abs/1")]
    out = clean(entries, {"blacklist_presets": ["search"], "strip_query_params": True})
    assert [e.normalized_url for e in out] == ["https://arxiv.org/abs/1"]


def test_unknown_preset_is_rejected_by_config_validation():
    cfg = {
        "llm": {"provider": "anthropic"},
        "source": {"browser": "chrome"},
        "processing": {"window_size_days": 30},
        "filtering": {"blacklist_presets": ["nope"]},
        "classification": {"categories": [{"name": "X"}], "prompt": "p"},
        "output": {"group_by": "month", "file_format": "txt"},
    }
    with pytest.raises(ValueError, match="nope"):
        _validate(cfg)


def test_preset_names_are_documented_in_the_example_config():
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    text = example.read_text(encoding="utf-8")
    for name in preset_names():
        assert name in text, f"{name} is not mentioned in config.example.yaml"
