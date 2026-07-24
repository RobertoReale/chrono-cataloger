"""Triage economico: pre-filtro con modello leggero (es. Haiku).

Riceve le voci pulite, le manda a batch grandi al modello economico usando SOLO
dominio + titolo, e tiene solo quelle marcate "rilevante". Riduce tipicamente il
volume del 90-95% prima dello stadio costoso di classificazione.

Separazione delle responsabilita':
- l'UTENTE definisce i *criteri* (``triage.prompt`` in config);
- il CODICE impone il *formato di output* parseabile, così il tuning dei criteri
  non rompe mai il parsing.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from .llm_client import LLMClient
from .models import HistoryEntry

# Istruzioni di formato aggiunte automaticamente al prompt utente.
_FORMAT_INSTRUCTIONS = """

Ti viene fornita una lista NUMERATA di voci (una per riga, formato: "N. dominio — titolo").
Rispondi ESCLUSIVAMENTE con un array JSON, un oggetto per ogni voce, nell'ordine dato:
[{{"i": 1, "v": "rilevante"}}, {{"i": 2, "v": "rumore"}}, ...]
dove "i" e' il numero della voce e "v" e' "rilevante" oppure "rumore".
Non aggiungere testo prima o dopo l'array JSON. Non usare markdown."""


def _domain(url: str) -> str:
    netloc = urlsplit(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _build_batch_prompt(base_prompt: str, batch: list[HistoryEntry]) -> str:
    lines = []
    for idx, e in enumerate(batch, start=1):
        title = (e.title or "").replace("\n", " ").strip() or "(senza titolo)"
        lines.append(f"{idx}. {_domain(e.normalized_url or e.url)} — {title}")
    listing = "\n".join(lines)
    return f"{base_prompt.rstrip()}\n{_FORMAT_INSTRUCTIONS}\n\nVoci:\n{listing}"


_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def _parse_verdicts(raw: str, batch_size: int) -> dict[int, bool]:
    """Estrae {indice(1-based) -> is_rilevante} dalla risposta del modello.

    Robusto a testo extra intorno al JSON. Se il parsing fallisce del tutto,
    ritorna un dizionario vuoto (il chiamante decide il fallback).
    """
    match = _JSON_ARRAY_RE.search(raw or "")
    if not match:
        return {}
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
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
            verdicts[i] = v.startswith("rilev")  # "rilevante"
    return verdicts


def triage(
    entries: list[HistoryEntry],
    client: LLMClient,
    triage_cfg: dict,
    triage_model: str,
    on_progress=None,
) -> list[HistoryEntry]:
    """Filtra le voci tenendo solo quelle marcate rilevanti dal modello.

    Se ``triage.enabled`` e' False, ritorna tutte le voci invariate.
    In caso di batch non parseabile, per prudenza le voci di quel batch sono
    TENUTE (falso positivo meglio di scartare qualcosa di rilevante): saranno
    comunque filtrate meglio dalla classificazione fine.
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
        # max_tokens generoso: ~30 token/voce per l'array JSON compatto.
        raw = client.complete(prompt, triage_model, max_tokens=min(8000, 40 * len(batch) + 200))
        verdicts = _parse_verdicts(raw, len(batch))

        if not verdicts:
            # Batch non parseabile: conserva tutto per prudenza.
            kept.extend(batch)
        else:
            for idx, e in enumerate(batch, start=1):
                # default True se il modello ha omesso la voce (prudenza)
                if verdicts.get(idx, True):
                    kept.append(e)

        if on_progress:
            on_progress(min(start + batch_size, len(entries)), len(entries), len(kept))

    return kept
