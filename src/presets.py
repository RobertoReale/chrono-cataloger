"""Ready-made domain blacklist groups shipped with the project.

They live in ``presets/domain_blacklist.yaml`` rather than in the code so they
can be edited without touching Python, and they are kept separate from the
user's own ``filtering.domain_blacklist``: the two are merged only when the
filters actually run, so the GUI can always show what came from where.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

PRESETS_PATH = Path(__file__).resolve().parent.parent / "presets" / "domain_blacklist.yaml"


@lru_cache(maxsize=1)
def load_presets() -> dict[str, list[str]]:
    """All the groups, as ``{name: [domain, ...]}``. Empty if the file is gone."""
    try:
        data = yaml.safe_load(PRESETS_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return {
        str(name): [str(d).strip().lower() for d in domains or [] if str(d).strip()]
        for name, domains in data.items()
        if isinstance(domains, list)
    }


def preset_names() -> list[str]:
    return list(load_presets())


def domains_for(names: list[str]) -> list[str]:
    """The domains of the given groups, deduplicated, unknown names ignored."""
    presets = load_presets()
    seen: dict[str, None] = {}
    for name in names:
        for domain in presets.get(str(name), []):
            seen.setdefault(domain, None)
    return list(seen)


def effective_domain_blacklist(filtering: dict) -> list[str]:
    """The user's blacklist plus the domains of the presets it enables."""
    own = [str(d).strip().lower() for d in filtering.get("domain_blacklist") or []]
    merged = dict.fromkeys(d for d in own if d)
    for domain in domains_for(filtering.get("blacklist_presets") or []):
        merged.setdefault(domain, None)
    return list(merged)
