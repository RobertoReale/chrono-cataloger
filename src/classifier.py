"""Fine-grained classification + summarization with the main model (e.g. Sonnet).

Takes the entries that survived triage, splits them into small batches and asks
the model to assign a category and write a diary-style summary. The output is
JSON validated through pydantic; malformed JSON triggers a single retry with a
correction request.

As with triage: the USER defines categories and criteria (config), the CODE
enforces the format and re-injects the metadata (last_visit, normalized url)
after classification, pairing each result with its source entry by index.
"""
from __future__ import annotations

from pydantic import ValidationError

from .config import categories_list_text
from .llm_client import LLMClient
from .models import ClassifiedEntry, HistoryEntry
from .parsing import TRUNCATED, extract_json_array_status

_FORMAT_INSTRUCTIONS = """

INPUT FORMAT: you are given a NUMBERED list of entries (format: "N. [url] title (visits: X)").
OUTPUT FORMAT: reply EXCLUSIVELY with a JSON array. For EVERY entry that clearly
belongs to a category, include an object:
  {"i": N, "category": "<exact category name>", "summary": "<max 20 words>", "url": "<url or empty string>"}
- "i" is the number of the entry the result refers to (required).
- "category" must be EXACTLY one of the names listed above.
- OMIT entirely any entry that does not clearly fit any category.
- Do not add any text before or after the array. Do not use markdown."""

def _build_batch_prompt(base_prompt: str, categories_text: str, batch: list[HistoryEntry]) -> str:
    prompt = base_prompt.replace("{categories_list}", categories_text)
    lines = []
    for idx, e in enumerate(batch, start=1):
        title = (e.title or "").replace("\n", " ").strip() or "(untitled)"
        url = e.normalized_url or e.url
        lines.append(f"{idx}. [{url}] {title} (visits: {e.visit_count})")
    listing = "\n".join(lines)
    return f"{prompt.rstrip()}\n{_FORMAT_INSTRUCTIONS}\n\nEntries:\n{listing}"


def _valid_category_names(cfg: dict) -> set[str]:
    return {c["name"] for c in cfg["classification"]["categories"]}


def _parse_batch(
    raw: str,
    batch: list[HistoryEntry],
    valid_categories: set[str],
) -> tuple[list[ClassifiedEntry], str | None]:
    """Turn the raw response into validated ClassifiedEntry objects, re-injecting metadata.

    Returns ``(results, problem)``; ``problem`` is None when the response was
    read in full, otherwise the reason it was not (see :mod:`.parsing`).
    """
    items, problem = extract_json_array_status(raw)
    if items is None:
        return [], problem

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
        category = str(item.get("category", "")).strip()
        # Category match: exact, or case-insensitive as a fallback.
        if category not in valid_categories:
            lowered = {c.lower(): c for c in valid_categories}
            category = lowered.get(category.lower(), "")
        if not category:
            continue  # unrecognized category: discard

        source = batch[i - 1]
        try:
            entry = ClassifiedEntry(
                category=category,
                summary=str(item.get("summary", "")).strip(),
                url=str(item.get("url", "") or ""),
                last_visit_micros=source.last_visit_micros,
                normalized_url=source.normalized_url or source.url,
            )
        except ValidationError:
            continue  # empty summary or invalid data
        results.append(entry)
    return results, problem


def classify(
    entries: list[HistoryEntry],
    client: LLMClient,
    cfg: dict,
    on_progress=None,
    on_warning=None,
) -> list[ClassifiedEntry]:
    """Classify the relevant entries into {category, summary, url} objects.

    A batch with malformed JSON is retried once with a correction request; if the
    retry fails too, the batch is skipped. Skipped and partially-read batches are
    reported through ``on_warning(message)``: without it a dropped batch is
    indistinguishable from a model that simply categorized nothing.
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
        # ~80 tokens/entry for url+summary in JSON
        max_tokens = min(16000, 90 * len(batch) + 500)

        raw = client.complete(prompt, model, max_tokens=max_tokens)
        results, problem = _parse_batch(raw, batch, valid_categories)

        if problem is not None and not results:
            # Nothing readable at all: a single retry with an explicit correction.
            retry_prompt = (
                prompt
                + "\n\nWARNING: your previous response was not a valid JSON array. "
                "Reply ONLY with the JSON array, without any other text."
            )
            raw = client.complete(retry_prompt, model, max_tokens=max_tokens)
            results, problem = _parse_batch(raw, batch, valid_categories)

        if problem is not None and on_warning:
            span = f"entries {start + 1}-{start + len(batch)}"
            if problem == TRUNCATED:
                on_warning(
                    f"classification: response cut off for {span}; kept the "
                    f"{len(results)} of {len(batch)} entries that came through "
                    "(lower classification.batch_size to avoid it)"
                )
            else:
                on_warning(
                    f"classification: unreadable response for {span}, batch "
                    f"dropped ({len(batch)} entries lost)"
                )

        all_results.extend(results)

        if on_progress:
            on_progress(min(start + batch_size, len(entries)), len(entries), len(all_results))

    return all_results
