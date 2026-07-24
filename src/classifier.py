"""Classificazione fine + sintesi con il modello principale (es. Sonnet).

Prende le voci sopravvissute al triage, le divide in batch piccoli e chiede al
modello di assegnare una categoria e scrivere una sintesi in stile diario.
L'output e' JSON validato via pydantic; in caso di JSON malformato si ritenta
una volta con una richiesta di correzione.

Come per il triage: l'UTENTE definisce categorie e criteri (config), il CODICE
impone il formato e re-inietta i metadati (last_visit, url normalizzato) dopo la
classificazione, associando ogni risultato alla voce originale tramite indice.
"""
from __future__ import annotations

import json
import re

from pydantic import ValidationError

from .config import categories_list_text
from .llm_client import LLMClient
from .models import ClassifiedEntry, HistoryEntry

_FORMAT_INSTRUCTIONS = """

FORMATO INPUT: ti viene data una lista NUMERATA di voci (formato: "N. [url] titolo (visite: X)").
FORMATO OUTPUT: rispondi ESCLUSIVAMENTE con un array JSON. Per OGNI voce che rientra
chiaramente in una categoria, includi un oggetto:
  {{"i": N, "categoria": "<nome categoria esatto>", "sintesi": "<max 20 parole>", "url": "<url o stringa vuota>"}}
- "i" e' il numero della voce a cui il risultato si riferisce (obbligatorio).
- "categoria" deve essere ESATTAMENTE uno dei nomi elencati sopra.
- OMETTI del tutto le voci che non si adattano chiaramente a nessuna categoria.
- Non aggiungere testo prima o dopo l'array. Non usare markdown."""

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def _build_batch_prompt(base_prompt: str, categories_text: str, batch: list[HistoryEntry]) -> str:
    prompt = base_prompt.replace("{categories_list}", categories_text)
    lines = []
    for idx, e in enumerate(batch, start=1):
        title = (e.title or "").replace("\n", " ").strip() or "(senza titolo)"
        url = e.normalized_url or e.url
        lines.append(f"{idx}. [{url}] {title} (visite: {e.visit_count})")
    listing = "\n".join(lines)
    return f"{prompt.rstrip()}\n{_FORMAT_INSTRUCTIONS}\n\nVoci:\n{listing}"


def _extract_json_array(raw: str) -> list | None:
    match = _JSON_ARRAY_RE.search(raw or "")
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, list) else None
    except json.JSONDecodeError:
        return None


def _valid_category_names(cfg: dict) -> set[str]:
    return {c["name"] for c in cfg["classification"]["categories"]}


def _parse_batch(
    raw: str,
    batch: list[HistoryEntry],
    valid_categories: set[str],
) -> list[ClassifiedEntry]:
    """Trasforma la risposta grezza in ClassifiedEntry validate, re-iniettando i metadati."""
    items = _extract_json_array(raw)
    if items is None:
        return []

    results: list[ClassifiedEntry] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        if not (1 <= i <= len(batch)):
            continue
        categoria = str(item.get("categoria", "")).strip()
        # Match categoria: esatto, o case-insensitive come fallback.
        if categoria not in valid_categories:
            lowered = {c.lower(): c for c in valid_categories}
            categoria = lowered.get(categoria.lower(), "")
        if not categoria:
            continue  # categoria non riconosciuta: scarta

        source = batch[i - 1]
        try:
            entry = ClassifiedEntry(
                categoria=categoria,
                sintesi=str(item.get("sintesi", "")).strip(),
                url=str(item.get("url", "") or ""),
                last_visit_micros=source.last_visit_micros,
                normalized_url=source.normalized_url or source.url,
            )
        except ValidationError:
            continue  # sintesi vuota o dati non validi
        results.append(entry)
    return results


def classify(
    entries: list[HistoryEntry],
    client: LLMClient,
    cfg: dict,
    on_progress=None,
) -> list[ClassifiedEntry]:
    """Classifica le voci rilevanti in oggetti {categoria, sintesi, url}.

    Un batch con JSON malformato viene ritentato una volta con una richiesta di
    correzione; se anche il retry fallisce, il batch viene saltato (loggato dal
    chiamante tramite il conteggio dei risultati).
    """
    if not entries:
        return []

    class_cfg = cfg["classification"]
    batch_size = int(class_cfg.get("batch_size", 50))
    base_prompt = class_cfg.get("prompt", "")
    categories_text = categories_list_text(cfg)
    valid_categories = _valid_category_names(cfg)
    model = cfg["llm"]["model"]

    all_results: list[ClassifiedEntry] = []

    for start in range(0, len(entries), batch_size):
        batch = entries[start:start + batch_size]
        prompt = _build_batch_prompt(base_prompt, categories_text, batch)
        # ~80 token/voce per url+sintesi in JSON
        max_tokens = min(16000, 90 * len(batch) + 500)

        raw = client.complete(prompt, model, max_tokens=max_tokens)
        results = _parse_batch(raw, batch, valid_categories)

        if not results and _extract_json_array(raw) is None:
            # JSON completamente illeggibile: un solo retry con correzione esplicita.
            retry_prompt = (
                prompt
                + "\n\nATTENZIONE: la risposta precedente non era un array JSON valido. "
                "Rispondi SOLO con l'array JSON, senza alcun altro testo."
            )
            raw = client.complete(retry_prompt, model, max_tokens=max_tokens)
            results = _parse_batch(raw, batch, valid_categories)

        all_results.extend(results)

        if on_progress:
            on_progress(min(start + batch_size, len(entries)), len(entries), len(all_results))

    return all_results
