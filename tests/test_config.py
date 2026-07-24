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


def test_categories_list_text():
    cfg = {"classification": {"categories": [
        {"name": "Books", "description": "the books"},
        {"name": "Films", "description": ""},
    ]}}
    text = categories_list_text(cfg)
    assert "- Books: the books" in text
    assert "- Films" in text
