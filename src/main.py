"""Pipeline orchestration CLI.

Flow: load config -> resolve period -> generate windows -> for each incomplete
window: extract -> clean -> triage -> classify -> write -> update checkpoint and
processed_ids -> log costs.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Supports both running as a module (python -m src.main) and as a script.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src import cleaner, classifier, extractor, triage, windowing
    from src.config import load_config, valid_group_by
    from src.costs import CostTracker, estimate_tokens
    from src.llm_client import LLMClient, LLMError, get_client
    from src.writer import Writer
else:
    from . import cleaner, classifier, extractor, triage, windowing
    from .config import load_config, valid_group_by
    from .costs import CostTracker, estimate_tokens
    from .llm_client import LLMClient, LLMError, get_client
    from .writer import Writer


# State/log roots relative to the project root.
_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = _ROOT / "state"
LOGS_DIR = _ROOT / "logs"
CHECKPOINT_PATH = STATE_DIR / "checkpoint.json"
PROCESSED_IDS_PATH = STATE_DIR / "processed_ids.json"


class _CountingClient(LLMClient):
    """Wrapper that estimates tokens for cost tracking, delegating to the real client."""

    def __init__(self, inner: LLMClient, tracker: CostTracker):
        self._inner = inner
        self._tracker = tracker

    def complete(self, prompt: str, model: str, max_tokens: int = 4096) -> str:
        response = self._inner.complete(prompt, model, max_tokens=max_tokens)
        self._tracker.add(
            model,
            estimate_tokens(prompt),
            estimate_tokens(response or ""),
        )
        return response


def _batch_progress(on_progress, stage: str, window_key: str):
    """Adapt the batch callbacks of triage/classifier to the pipeline signature.

    ``triage`` and ``classifier`` report ``(done, total, produced)`` after every
    batch; the pipeline speaks ``(stage, done, total, extra)``. These are the two
    slow stages — a single batch can take minutes — so this is where per-batch
    feedback actually matters.
    """
    if on_progress is None:
        return None

    def cb(done: int, total: int, produced: int) -> None:
        on_progress(stage, done, total, {"window": window_key, "produced": produced})

    return cb


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chrono-cataloger",
        description="Catalog your Chrome history into a diary by category, via an LLM.",
    )
    p.add_argument("--config", default=str(_ROOT / "config.yaml"), help="path to config.yaml")
    p.add_argument("--from", dest="from_date", help="start date (YYYY-MM-DD)")
    p.add_argument("--to", dest="to_date", help="end date (YYYY-MM-DD)")
    p.add_argument("--last-days", type=int, help="last N days from today")
    p.add_argument(
        "--group-by",
        help="output granularity: month | week | days:N | all (default from config)",
    )
    p.add_argument("--window-size-days", type=int, help="internal window size (overrides config)")
    p.add_argument("--history-path", help="path to the Chrome History file (overrides config)")
    p.add_argument(
        "--max-batches-per-run",
        type=int,
        help="maximum number of windows to process in this run (for testing)",
    )
    p.add_argument(
        "--reset-checkpoint",
        action="store_true",
        help="ignore and clear the checkpoint before starting",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "run extraction+cleaning+triage without classifying or writing; "
            "leaves the checkpoint untouched, so a later real run redoes the windows"
        ),
    )
    return p


def run(args, on_progress=None) -> dict:
    """Run the pipeline. ``on_progress(stage, done, total, extra)`` is optional.

    Returns a summary dictionary (also written to logs/).
    """
    warnings_seen: list[str] = []
    cfg = load_config(args.config, on_warning=warnings_seen.append)

    # CLI overrides. Validated here, not where they are used: everything below
    # costs money, and a typo should not be discovered after paying for it.
    group_by = args.group_by or cfg["output"]["group_by"]
    if not valid_group_by(group_by):
        raise ValueError(
            f"invalid --group-by: {group_by!r} (expected: month | week | days:N | all)"
        )
    window_size = args.window_size_days or cfg["processing"]["window_size_days"]
    if window_size < 1:
        raise ValueError("--window-size-days must be >= 1")
    history_path = args.history_path or cfg["source"].get("history_path")
    max_windows = args.max_batches_per_run
    if max_windows is None:
        max_windows = cfg["processing"].get("max_batches_per_run")

    # Period and windows.
    from_date = _parse_date(args.from_date) if args.from_date else None
    to_date = _parse_date(args.to_date) if args.to_date else None
    if args.last_days is not None and (from_date or to_date):
        warnings_seen.append(
            "--last-days was given together with --from/--to: the explicit dates "
            "are ignored."
        )
    start, end = windowing.resolve_period(from_date, to_date, args.last_days)
    windows = windowing.generate_windows(start, end, window_size)

    if args.reset_checkpoint and CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()

    completed = windowing.load_checkpoint(CHECKPOINT_PATH)
    todo = windowing.pending_windows(windows, completed)
    if max_windows is not None:
        todo = todo[:max_windows]

    # LLM client with cost tracking.
    tracker = CostTracker(cfg["llm"]["provider"])
    real_client = get_client(cfg["llm"])
    client = _CountingClient(real_client, tracker)

    # Shared writer (idempotency persisted across windows).
    writer = Writer(
        base_dir=cfg["output"]["base_dir"],
        group_by=group_by,
        file_format=cfg["output"]["file_format"],
        period_start=start,
        processed_ids_path=PROCESSED_IDS_PATH,
        category_order=[c["name"] for c in cfg["classification"]["categories"]],
    )

    stats = {
        "period": {"from": start.date().isoformat(), "to": end.date().isoformat()},
        "group_by": group_by,
        "window_size_days": window_size,
        "total_windows": len(windows),
        "windows_to_process": len(todo),
        "raw_entries": 0,
        "entries_after_cleaning": 0,
        "entries_after_triage": 0,
        "entries_classified": 0,
        "entries_written": 0,
    }

    for w in todo:
        if on_progress:
            on_progress("window", 0, 1, {"window": w.key})

        raw = extractor.extract(history_path, w.start, w.end, cfg["source"]["browser"])
        stats["raw_entries"] += len(raw)
        if on_progress:
            on_progress("extraction", len(raw), len(raw), {"window": w.key})

        cleaned = cleaner.clean(raw, cfg["filtering"])
        stats["entries_after_cleaning"] += len(cleaned)
        if on_progress:
            on_progress("cleaning", len(cleaned), len(raw), {"window": w.key})

        survivors = triage.triage(
            cleaned,
            client,
            cfg["triage"],
            cfg["llm"]["triage_model"],
            on_progress=_batch_progress(on_progress, "triage", w.key),
            on_warning=warnings_seen.append,
        )
        stats["entries_after_triage"] += len(survivors)
        # triage() emits nothing when it has no batches to run (no entries, or
        # disabled in config): report the stage here so none is silently skipped.
        if on_progress and (not cleaned or not cfg["triage"].get("enabled", True)):
            on_progress(
                "triage", len(cleaned), len(cleaned),
                {"window": w.key, "produced": len(survivors)},
            )

        if args.dry_run:
            # No classification, no writing — and deliberately no checkpoint:
            # marking the window done here would make the next real run skip it.
            continue

        classified = classifier.classify(
            survivors,
            client,
            cfg,
            on_progress=_batch_progress(on_progress, "classification", w.key),
            on_warning=warnings_seen.append,
        )
        stats["entries_classified"] += len(classified)
        if on_progress and not survivors:
            on_progress("classification", 0, 0, {"window": w.key, "produced": 0})


        written = writer.write(classified)
        stats["entries_written"] += written

        completed.add(w.key)
        windowing.save_checkpoint(CHECKPOINT_PATH, completed)
        if on_progress:
            on_progress("writing", written, len(classified), {"window": w.key})

    stats["costs"] = tracker.as_dict()
    stats["warnings"] = warnings_seen
    _write_log(stats)
    return stats


def _write_log(stats: dict) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = LOGS_DIR / f"run_{today}.json"
    # If a log for today already exists, don't overwrite it: add a suffix.
    if path.exists():
        i = 2
        while (LOGS_DIR / f"run_{today}_{i}.json").exists():
            i += 1
        path = LOGS_DIR / f"run_{today}_{i}.json"
    payload = dict(stats)
    payload["timestamp"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _print_summary(stats: dict) -> None:
    print("\n=== Run summary ===")
    print(f"Period:              {stats['period']['from']} -> {stats['period']['to']}")
    print(f"Granularity:         {stats['group_by']}")
    print(f"Windows:             {stats['windows_to_process']}/{stats['total_windows']} processed")
    print(f"Raw entries:         {stats['raw_entries']}")
    print(f"After cleaning:      {stats['entries_after_cleaning']}")
    print(f"After triage:        {stats['entries_after_triage']}")
    print(f"Classified:          {stats['entries_classified']}")
    print(f"Written (new):       {stats['entries_written']}")
    c = stats.get("costs", {})
    cost = c.get("estimated_cost_usd")
    cost_text = f"~${cost}" if cost is not None else c.get("cost_note", "n/a")
    print(
        f"Estimated tokens:    in={c.get('estimated_input_tokens', 0)} "
        f"out={c.get('estimated_output_tokens', 0)}  "
        f"({cost_text})"
    )
    for warning in stats.get("warnings", []):
        print(f"Warning: {warning}", file=sys.stderr)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        stats = run(args, on_progress=_cli_progress)
    except (ValueError, FileNotFoundError, LLMError) as e:
        # LLMError covers the most ordinary failure of all — an unset API key —
        # which used to reach the user as a traceback.
        print(f"Error: {e}", file=sys.stderr)
        return 2
    _print_summary(stats)
    return 0


def _cli_progress(stage: str, done: int, total: int, extra: dict) -> None:
    win = extra.get("window", "")
    produced = extra.get("produced")
    tail = f"  -> {produced} kept" if produced is not None else ""
    print(f"[{win}] {stage}: {done}/{total}{tail}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
