"""Loading and normalization of the user configuration (``config.yaml``).

Exposes :func:`load_config`, which returns a nested dictionary with defaults
already applied, so the rest of the pipeline can access keys without checking
for their presence every time.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

# Default values: they mirror config.example.yaml. Any key missing from the user
# config is filled in from here.
DEFAULTS: dict[str, Any] = {
    "llm": {
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "triage_model": "claude-haiku-4-5",
        "api_key_env": "ANTHROPIC_API_KEY",
        "base_url": None,
        "max_retries": 3,
        "timeout_seconds": 120,
    },
    "source": {
        "browser": "chrome",
        "history_path": None,
    },
    "processing": {
        "window_size_days": 30,
        "max_batches_per_run": None,
    },
    "filtering": {
        "min_visit_count": 1,
        "domain_blacklist": [],
        "url_keyword_blacklist": [],
        "strip_query_params": True,
    },
    "triage": {
        "enabled": True,
        "batch_size": 200,
        "prompt": (
            "You are given a list of browser history entries (domain + title).\n"
            "For each one, answer only \"relevant\" or \"noise\"."
        ),
    },
    "classification": {
        "batch_size": 50,
        "categories": [],
        "prompt": "",
    },
    "output": {
        "base_dir": "./Study_Archive",
        "group_by": "month",
        "file_format": "txt",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursive merge: ``override`` wins, but missing sub-keys stay from ``base``."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config(path: str | os.PathLike) -> dict:
    """Load ``config.yaml``, applying defaults and validating the critical fields."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {p}. "
            "Copy config.example.yaml to config.yaml."
        )
    with p.open("r", encoding="utf-8") as f:
        user_cfg = yaml.safe_load(f) or {}

    cfg = _deep_merge(DEFAULTS, user_cfg)
    _validate(cfg)
    return cfg


def _validate(cfg: dict) -> None:
    provider = cfg["llm"]["provider"]
    if provider not in ("anthropic", "claude_code", "openai", "ollama"):
        raise ValueError(f"unsupported LLM provider: {provider!r}")

    group_by = cfg["output"]["group_by"]
    if not _valid_group_by(group_by):
        raise ValueError(
            f"invalid output.group_by: {group_by!r} "
            "(expected: month | week | days:N | all)"
        )

    if cfg["output"]["file_format"] not in ("txt", "md", "md_rich", "md_journal"):
        raise ValueError(
            "output.file_format must be 'txt', 'md', 'md_rich' or 'md_journal'"
        )

    browser = cfg["source"]["browser"]
    if browser != "chrome":
        raise ValueError(
            f"unsupported browser: {browser!r} (only 'chrome' is supported; "
            "the extractor speaks the Chrome History schema)"
        )

    if not cfg["classification"]["categories"]:
        raise ValueError("No category defined in classification.categories")

    for cat in cfg["classification"]["categories"]:
        if not isinstance(cat, dict) or not str(cat.get("name", "")).strip():
            raise ValueError(
                f"every classification.categories entry needs a 'name': {cat!r}"
            )

    if not str(cfg["classification"]["prompt"]).strip():
        raise ValueError("classification.prompt is empty: nothing would be asked of the model")

    window = cfg["processing"]["window_size_days"]
    if not isinstance(window, int) or window < 1:
        raise ValueError("processing.window_size_days must be an integer >= 1")


def _valid_group_by(value: str) -> bool:
    if value in ("month", "week", "all"):
        return True
    if isinstance(value, str) and value.startswith("days:"):
        try:
            return int(value.split(":", 1)[1]) >= 1
        except (ValueError, IndexError):
            return False
    return False


def categories_list_text(cfg: dict) -> str:
    """Build the ``{categories_list}`` string to inject into the prompt."""
    lines = []
    for cat in cfg["classification"]["categories"]:
        name = cat["name"]
        desc = cat.get("description", "")
        lines.append(f"- {name}: {desc}" if desc else f"- {name}")
    return "\n".join(lines)
