import pytest
import yaml

from src.config import categories_list_text, load_config


def _write(tmp_path, data):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


def _minimal():
    return {
        "classification": {
            "categories": [{"name": "Books", "description": "books"}],
            "prompt": "x {categories_list}",
        }
    }


def test_load_applies_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, _minimal()))
    assert cfg["llm"]["provider"] == "anthropic"
    assert cfg["output"]["group_by"] == "month"
    assert cfg["processing"]["window_size_days"] == 30


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_invalid_group_by_rejected(tmp_path):
    data = _minimal()
    data["output"] = {"group_by": "yearly"}
    with pytest.raises(ValueError):
        load_config(_write(tmp_path, data))


def test_days_group_by_accepted(tmp_path):
    data = _minimal()
    data["output"] = {"group_by": "days:10"}
    cfg = load_config(_write(tmp_path, data))
    assert cfg["output"]["group_by"] == "days:10"


def test_no_categories_rejected(tmp_path):
    data = {"classification": {"categories": [], "prompt": "x"}}
    with pytest.raises(ValueError):
        load_config(_write(tmp_path, data))


def test_empty_classification_prompt_rejected(tmp_path):
    data = {"classification": {"categories": [{"name": "Books"}], "prompt": "   "}}
    with pytest.raises(ValueError):
        load_config(_write(tmp_path, data))


def test_category_without_name_rejected(tmp_path):
    data = {"classification": {"categories": [{"description": "no name"}], "prompt": "x"}}
    with pytest.raises(ValueError):
        load_config(_write(tmp_path, data))


def test_unsupported_browser_rejected(tmp_path):
    data = _minimal()
    data["source"] = {"browser": "firefox"}
    with pytest.raises(ValueError):
        load_config(_write(tmp_path, data))


def test_claude_code_provider_accepted(tmp_path):
    data = _minimal()
    data["llm"] = {"provider": "claude_code"}
    cfg = load_config(_write(tmp_path, data))
    assert cfg["llm"]["provider"] == "claude_code"


def test_empty_yaml_keys_become_empty_lists(tmp_path):
    """`key:` with nothing under it is valid YAML and means None, not []."""
    p = tmp_path / "config.yaml"
    p.write_text(
        "filtering:\n"
        "  blacklist_presets:\n"
        "  domain_blacklist:\n"
        "  url_keyword_blacklist:\n"
        "classification:\n"
        "  categories:\n"
        "    - {name: Books}\n"
        '  prompt: "x {categories_list}"\n',
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg["filtering"]["blacklist_presets"] == []
    assert cfg["filtering"]["domain_blacklist"] == []
    assert cfg["filtering"]["url_keyword_blacklist"] == []


def test_empty_section_falls_back_to_its_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "filtering:\n"
        "triage:\n"
        "classification:\n"
        "  categories:\n"
        "    - {name: Books}\n"
        '  prompt: "x {categories_list}"\n',
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg["filtering"]["min_visit_count"] == 1
    assert cfg["triage"]["batch_size"] == 200


def test_unknown_keys_are_reported(tmp_path):
    data = _minimal()
    data["filtering"] = {"domain_blaklist": ["x.com"]}
    seen = []
    load_config(_write(tmp_path, data), on_warning=seen.append)
    assert seen and "filtering.domain_blaklist" in seen[0]


def test_zero_batch_size_rejected(tmp_path):
    data = _minimal()
    data["triage"] = {"batch_size": 0}
    with pytest.raises(ValueError, match="triage.batch_size"):
        load_config(_write(tmp_path, data))


def test_unset_timeout_accepted(tmp_path):
    data = _minimal()
    data["llm"] = {"timeout_seconds": None}
    assert load_config(_write(tmp_path, data))["llm"]["timeout_seconds"] is None


def test_negative_timeout_rejected(tmp_path):
    data = _minimal()
    data["llm"] = {"timeout_seconds": 0}
    with pytest.raises(ValueError, match="timeout_seconds"):
        load_config(_write(tmp_path, data))


def test_categories_list_text():
    cfg = {"classification": {"categories": [
        {"name": "Books", "description": "the books"},
        {"name": "Films", "description": ""},
    ]}}
    text = categories_list_text(cfg)
    assert "- Books: the books" in text
    assert "- Films" in text
