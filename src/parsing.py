"""Extraction of the JSON array an LLM was asked to return.

Both triage and classification tell the model to reply with a bare JSON array,
and both have to cope with the model doing something slightly different anyway:
wrapping the array in a markdown fence, prefacing it with a sentence, or getting
cut off by ``max_tokens`` halfway through.

A greedy ``\\[.*\\]`` handles the fence but nothing else — it spans from the first
bracket in the whole response to the last one, so a stray "[see below]" in the
preamble is enough to make the parse fail and lose the entire batch. Scanning
for a *balanced* array instead, and salvaging the objects that did complete when
the response was truncated, turns both of those from total losses into no loss
at all.
"""
from __future__ import annotations

import json

# Why a batch could not be read in full; attached to the warnings a run reports.
TRUNCATED = "truncated"
UNPARSEABLE = "unparseable"


def extract_json_array(raw: str) -> list | None:
    """Return the JSON array contained in ``raw``, or None if there is none.

    When several balanced arrays parse, the longest wins: the answer is the big
    array of results, not an example the model happened to write above it.
    """
    array, _ = extract_json_array_status(raw)
    return array


def extract_json_array_status(raw: str) -> tuple[list | None, str | None]:
    """Like :func:`extract_json_array`, plus *why* it is incomplete.

    Returns ``(array, problem)`` where ``problem`` is None when the response
    parsed cleanly, :data:`TRUNCATED` when only a prefix of the array could be
    recovered, and :data:`UNPARSEABLE` when nothing could.
    """
    text = raw or ""
    best: list | None = None
    for start, ch in enumerate(text):
        if ch != "[":
            continue
        end = _matching_bracket(text, start)
        if end is None:
            continue
        try:
            parsed = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list) and (best is None or len(parsed) > len(best)):
            best = parsed
    if best is not None:
        return best, None

    # No array closed properly: the response was very likely cut off mid-write.
    salvaged = _salvage_truncated(text)
    if salvaged:
        return salvaged, TRUNCATED
    return None, UNPARSEABLE


def _matching_bracket(text: str, start: int) -> int | None:
    """Index of the ``]`` closing the ``[`` at ``start``, ignoring brackets in strings."""
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return i
            if depth < 0:
                return None
    return None


def _salvage_truncated(text: str) -> list | None:
    """Recover the complete objects of an array the model never finished.

    ``max_tokens`` cutting a 50-entry answer off at entry 49 should cost one
    entry, not fifty.
    """
    start = text.find("[")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    last_complete: int | None = None
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 1:
                # An element of the outer array just closed.
                last_complete = i
            elif depth <= 0:
                break

    if last_complete is None:
        return None
    try:
        parsed = json.loads(text[start:last_complete + 1] + "]")
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) and parsed else None
