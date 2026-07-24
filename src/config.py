"""Loading and normalization of the user configuration (``config.yaml``).

Exposes :func:`load_config`, which returns a nested dictionary with defaults
already applied, so the rest of the pipeline can access keys without checking
for their presence every time.
"""
from __future__ import annotations

import copy
import os
import warnings
from pathlib import Path
from typing import Any

import yaml

from .presets import preset_names

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
        # None on purpose: each provider picks its own default, because a CLI
        # that starts a process per call needs far longer than an HTTP request.
        "timeout_seconds": None,
        # Only meaningful for provider: claude_code.
        "claude_cli_path": None,
        "claude_cli_args": [],
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
        # Empty on purpose: an existing config must keep filtering exactly what it
        # used to. config.example.yaml enables the recommended groups for new users.
        "blacklist_presets": [],
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


# Keys whose value the pipeline iterates over. An empty YAML key (``key:`` with
# nothing after it) parses as None, which would otherwise crash every consumer.
_LIST_KEYS = (
    ("filtering", "blacklist_presets"),
    ("filtering", "domain_blacklist"),
    ("filtering", "url_keyword_blacklist"),
    ("classification", "categories"),
    ("llm", "claude_cli_args"),
)

# Keys the pipeline calls string methods on, same reasoning.
_TEXT_KEYS = (
    ("llm", "provider"),
    ("llm", "model"),
    ("llm", "triage_model"),
    ("llm", "api_key_env"),
    ("source", "browser"),
    ("triage", "prompt"),
    ("classification", "prompt"),
    ("output", "base_dir"),
    ("output", "group_by"),
    ("output", "file_format"),
)


def _normalize(cfg: dict) -> dict:
    """Repair the shapes an empty YAML key produces.

    ``filtering:`` with nothing under it, or ``domain_blacklist:`` with no list,
    both yield None rather than the empty container the rest of the code assumes.
    Writing that in a config is normal YAML, so it is fixed here rather than
    guarded against at every use site.
    """
    for section, defaults in DEFAULTS.items():
        if not isinstance(cfg.get(section), dict):
            cfg[section] = copy.deepcopy(defaults)
    for section, key in _LIST_KEYS:
        if cfg[section].get(key) is None:
            cfg[section][key] = []
    for section, key in _TEXT_KEYS:
        if cfg[section].get(key) is None:
            cfg[section][key] = DEFAULTS[section][key]
    return cfg


def unknown_keys(user_cfg: dict) -> list[str]:
    """Dotted paths present in the user config that no default declares.

    A typo like ``domain_blaklist`` would otherwise disable a whole blacklist in
    complete silence, since the merge accepts any key.
    """
    found: list[str] = []
    for section, values in (user_cfg or {}).items():
        if section not in DEFAULTS:
            found.append(str(section))
            continue
        if not isinstance(values, dict):
            continue
        for key in values:
            if key not in DEFAULTS[section]:
                found.append(f"{section}.{key}")
    return sorted(found)


def load_config(path: str | os.PathLike, on_warning=None) -> dict:
    """Load ``config.yaml``, applying defaults and validating the critical fields.

    ``on_warning(message)`` receives the non-fatal remarks (unknown keys); it
    defaults to :mod:`warnings`, so the CLI shows them without any plumbing.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {p}. "
            "Copy config.example.yaml to config.yaml."
        )
    with p.open("r", encoding="utf-8") as f:
        user_cfg = yaml.safe_load(f) or {}
    if not isinstance(user_cfg, dict):
        raise ValueError(f"{p} does not contain a YAML mapping.")

    strays = unknown_keys(user_cfg)
    if strays:
        message = (
            f"ignored unknown key(s) in {p.name}: {', '.join(strays)} "
            "(a typo here silently disables the setting you meant)"
        )
        (on_warning or (lambda m: warnings.warn(m, stacklevel=2)))(message)

    cfg = _normalize(_deep_merge(DEFAULTS, user_cfg))
    _validate(cfg)
    return cfg


def _validate(cfg: dict) -> None:
    provider = cfg["llm"]["provider"]
    if provider not in ("anthropic", "claude_code", "openai", "ollama"):
        raise ValueError(f"unsupported LLM provider: {provider!r}")

    group_by = cfg["output"]["group_by"]
    if not valid_group_by(group_by):
        raise ValueError(
            f"invalid output.group_by: {group_by!r} "
            "(expected: month | week | days:N | all)"
        )

    if cfg["output"]["file_format"] not in ("txt", "md", "md_rich", "md_journal"):
        raise ValueError(
            "output.file_format must be 'txt', 'md', 'md_rich' or 'md_journal'"
        )

    unknown = set(cfg["filtering"]["blacklist_presets"]) - set(preset_names())
    if unknown:
        raise ValueError(
            f"unknown filtering.blacklist_presets: {sorted(unknown)} "
            f"(available: {sorted(preset_names())})"
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

    _positive_int(cfg, "processing", "window_size_days")
    _positive_int(cfg, "triage", "batch_size")
    _positive_int(cfg, "classification", "batch_size")

    retries = cfg["llm"]["max_retries"]
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise ValueError("llm.max_retries must be an integer >= 0")

    timeout = cfg["llm"]["timeout_seconds"]
    if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
        raise ValueError("llm.timeout_seconds must be a positive number (or empty)")

    max_windows = cfg["processing"]["max_batches_per_run"]
    if max_windows is not None:
        _positive_int(cfg, "processing", "max_batches_per_run")


def _positive_int(cfg: dict, section: str, key: str) -> None:
    """Reject the values that would only fail much later, mid-run.

    ``batch_size: 0`` in particular reaches ``range(0, n, 0)`` after extraction
    and cleaning have already run.
    """
    value = cfg[section][key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{section}.{key} must be an integer >= 1 (got {value!r})")


def valid_group_by(value) -> bool:
    """True for the ``output.group_by`` values the writer knows how to bucket."""
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
