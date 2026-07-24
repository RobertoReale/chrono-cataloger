"""Caricamento e normalizzazione della configurazione utente (``config.yaml``).

Espone :func:`load_config` che restituisce un dizionario annidato con i valori
di default applicati, così il resto della pipeline può accedere alle chiavi
senza controllare ogni volta la loro presenza.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

# Valori di default: rispecchiano config.example.yaml. Qualunque chiave mancante
# nel config utente viene riempita da qui.
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
        "min_visit_duration_seconds": 0,
        "min_visit_count": 1,
        "domain_blacklist": [],
        "url_keyword_blacklist": [],
        "strip_query_params": True,
        "dedupe_by": "url_normalizzato",
    },
    "triage": {
        "enabled": True,
        "batch_size": 200,
        "prompt": (
            "Ricevi una lista di voci di cronologia browser (dominio + titolo).\n"
            "Per ciascuna rispondi solo \"rilevante\" o \"rumore\"."
        ),
    },
    "classification": {
        "batch_size": 50,
        "categories": [],
        "prompt": "",
    },
    "output": {
        "base_dir": "./Archivio_Studio",
        "group_by": "month",
        "file_format": "txt",
        "filename_from": "category",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge ricorsivo: ``override`` vince, ma le sotto-chiavi mancanti restano da ``base``."""
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
    """Carica ``config.yaml`` applicando i default e validando i campi critici."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"File di configurazione non trovato: {p}. "
            "Copia config.example.yaml in config.yaml."
        )
    with p.open("r", encoding="utf-8") as f:
        user_cfg = yaml.safe_load(f) or {}

    cfg = _deep_merge(DEFAULTS, user_cfg)
    _validate(cfg)
    return cfg


def _validate(cfg: dict) -> None:
    provider = cfg["llm"]["provider"]
    if provider not in ("anthropic", "openai", "ollama"):
        raise ValueError(f"provider LLM non supportato: {provider!r}")

    group_by = cfg["output"]["group_by"]
    if not _valid_group_by(group_by):
        raise ValueError(
            f"output.group_by non valido: {group_by!r} "
            "(atteso: month | week | days:N | all)"
        )

    if cfg["output"]["file_format"] not in ("txt", "md"):
        raise ValueError("output.file_format deve essere 'txt' o 'md'")

    if not cfg["classification"]["categories"]:
        raise ValueError("Nessuna categoria definita in classification.categories")

    window = cfg["processing"]["window_size_days"]
    if not isinstance(window, int) or window < 1:
        raise ValueError("processing.window_size_days deve essere un intero >= 1")


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
    """Costruisce la stringa ``{categories_list}`` da iniettare nel prompt."""
    lines = []
    for cat in cfg["classification"]["categories"]:
        name = cat["name"]
        desc = cat.get("description", "")
        lines.append(f"- {name}: {desc}" if desc else f"- {name}")
    return "\n".join(lines)
