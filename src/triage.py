"""Cheap triage: pre-filtering with a lightweight model (e.g. Haiku).

Takes the cleaned entries, sends them to the cheap model in large batches using
ONLY domain + title, and keeps just the ones marked "relevant". This typically
cuts the volume by 90-95% before the expensive classification stage.

Separation of concerns:
- the USER defines the *criteria* (``triage.prompt`` in the config);
- the CODE enforces the parseable *output format*, so tuning the criteria never
  breaks parsing.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from .llm_client import LLMClient
from .models import HistoryEntry
from .parsing import extract_json_array

# Format instructions appended automatically to the user prompt.
_FORMAT_INSTRUCTIONS = """

You are given a NUMBERED list of entries (one per line, format: "N. domain — title").
Reply EXCLUSIVELY with a JSON array, one object per entry, in the given order:
[{"i": 1, "v": "relevant"}, {"i": 2, "v": "noise"}, ...]
where "i" is the entry number and "v" is either "relevant" or "noise".
Do not add any text before or after the JSON array. Do not use markdown."""


def _domain(url: str) -> str:
    netloc = urlsplit(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _build_batch_prompt(base_prompt: str, batch: list[HistoryEntry]) -> str:
    lines = []
    for idx, e in enumerate(batch, start=1):
        title = (e.title or "").replace("\n", " ").strip() or "(untitled)"
        lines.append(f"{idx}. {_domain(e.normalized_url or e.url)} — {title}")
    listing = "\n".join(lines)
    return f"{base_prompt.rstrip()}\n{_FORMAT_INSTRUCTIONS}\n\nEntries:\n{listing}"


def _parse_verdicts(raw: str, batch_size: int) -> dict[int, bool]:
    """Extract {index(1-based) -> is_relevant} from the model response.

    Tolerant of extra text around the JSON, and of a response cut off partway
    through. If parsing fails entirely, returns an empty dictionary (the caller
    decides the fallback).
    """
    items = extract_json_array(raw)
    if items is None:
        return {}

    verdicts: dict[int, bool] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        v = str(item.get("v", "")).strip().lower()
        if 1 <= i <= batch_size:
            verdicts[i] = v.startswith("relev")  # "relevant"
    return verdicts


def triage(
    entries: list[HistoryEntry],
    client: LLMClient,
    triage_cfg: dict,
    triage_model: str,
    on_progress=None,
    on_warning=None,
) -> list[HistoryEntry]:
    """Filter the entries, keeping only the ones the model marked as relevant.

    If ``triage.enabled`` is False, returns every entry unchanged.
    When a batch cannot be parsed, its entries are KEPT as a precaution (a false
    positive is better than dropping something relevant): fine-grained
    classification will filter them out more accurately anyway.
    """
    if not triage_cfg.get("enabled", True):
        return list(entries)
    if not entries:
        return []

    batch_size = int(triage_cfg.get("batch_size", 200))
    base_prompt = triage_cfg.get("prompt", "")
    kept: list[HistoryEntry] = []

    for start in range(0, len(entries), batch_size):
        batch = entries[start:start + batch_size]
        prompt = _build_batch_prompt(base_prompt, batch)
        # Generous max_tokens: ~30 tokens/entry for the compact JSON array.
        raw = client.complete(prompt, triage_model, max_tokens=min(8000, 40 * len(batch) + 200))
        verdicts = _parse_verdicts(raw, len(batch))

        if not verdicts:
            # Unparseable batch: keep everything, just in case.
            kept.extend(batch)
            if on_warning:
                on_warning(
                    f"triage: unreadable response for entries "
                    f"{start + 1}-{start + len(batch)}; kept all {len(batch)} "
                    "of them, so they go to the expensive stage unfiltered"
                )
        else:
            for idx, e in enumerate(batch, start=1):
                # default True if the model omitted the entry (precaution)
                if verdicts.get(idx, True):
                    kept.append(e)

        if on_progress:
            on_progress(min(start + batch_size, len(entries)), len(entries), len(kept))

    return kept
