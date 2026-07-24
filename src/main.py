"""CLI di orchestrazione della pipeline.

Flusso: carica config -> risolve periodo -> genera finestre -> per ogni finestra
non completata: estrai -> pulisci -> triage -> classifica -> scrivi -> aggiorna
checkpoint e processed_ids -> logga costi.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Supporta sia l'esecuzione come modulo (python -m src.main) sia come script.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src import cleaner, classifier, extractor, triage, windowing
    from src.config import load_config
    from src.costs import CostTracker, estimate_tokens
    from src.llm_client import LLMClient, get_client
    from src.writer import Writer
else:
    from . import cleaner, classifier, extractor, triage, windowing
    from .config import load_config
    from .costs import CostTracker, estimate_tokens
    from .llm_client import LLMClient, get_client
    from .writer import Writer


# Radici di stato/log relative alla root del progetto.
_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = _ROOT / "state"
LOGS_DIR = _ROOT / "logs"
CHECKPOINT_PATH = STATE_DIR / "checkpoint.json"
PROCESSED_IDS_PATH = STATE_DIR / "processed_ids.json"


class _CountingClient(LLMClient):
    """Wrapper che stima i token per il tracking dei costi, delegando al client reale."""

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


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chrono-catalogatore",
        description="Cataloga la cronologia Chrome in un diario per categoria via LLM.",
    )
    p.add_argument("--config", default=str(_ROOT / "config.yaml"), help="percorso config.yaml")
    p.add_argument("--from", dest="from_date", help="data inizio (YYYY-MM-DD)")
    p.add_argument("--to", dest="to_date", help="data fine (YYYY-MM-DD)")
    p.add_argument("--last-days", type=int, help="ultimi N giorni da oggi")
    p.add_argument(
        "--group-by",
        help="granularita' output: month | week | days:N | all (default da config)",
    )
    p.add_argument("--window-size-days", type=int, help="dimensione finestra interna (override config)")
    p.add_argument("--history-path", help="percorso al file History di Chrome (override config)")
    p.add_argument(
        "--max-batches-per-run",
        type=int,
        help="numero massimo di finestre da processare in questa esecuzione (test)",
    )
    p.add_argument(
        "--reset-checkpoint",
        action="store_true",
        help="ignora e azzera il checkpoint prima di partire",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="esegui estrazione+pulizia+triage senza scrivere file (nessuna classificazione)",
    )
    return p


def run(args, on_progress=None) -> dict:
    """Esegue la pipeline. ``on_progress(stage, done, total, extra)`` opzionale.

    Ritorna un dizionario di riepilogo (anche scritto in logs/).
    """
    cfg = load_config(args.config)

    # Override da CLI.
    group_by = args.group_by or cfg["output"]["group_by"]
    window_size = args.window_size_days or cfg["processing"]["window_size_days"]
    history_path = args.history_path or cfg["source"].get("history_path")
    max_windows = args.max_batches_per_run
    if max_windows is None:
        max_windows = cfg["processing"].get("max_batches_per_run")

    # Periodo e finestre.
    from_date = _parse_date(args.from_date) if args.from_date else None
    to_date = _parse_date(args.to_date) if args.to_date else None
    start, end = windowing.resolve_period(from_date, to_date, args.last_days)
    windows = windowing.generate_windows(start, end, window_size)

    if args.reset_checkpoint and CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()

    completed = windowing.load_checkpoint(CHECKPOINT_PATH)
    todo = windowing.pending_windows(windows, completed)
    if max_windows is not None:
        todo = todo[:max_windows]

    # Client LLM con tracking costi.
    tracker = CostTracker()
    real_client = get_client(cfg["llm"])
    client = _CountingClient(real_client, tracker)

    # Writer condiviso (idempotenza persistente tra finestre).
    writer = Writer(
        base_dir=cfg["output"]["base_dir"],
        group_by=group_by,
        file_format=cfg["output"]["file_format"],
        period_start=start,
        processed_ids_path=PROCESSED_IDS_PATH,
    )

    stats = {
        "periodo": {"from": start.date().isoformat(), "to": end.date().isoformat()},
        "group_by": group_by,
        "window_size_days": window_size,
        "finestre_totali": len(windows),
        "finestre_da_processare": len(todo),
        "voci_grezze": 0,
        "voci_dopo_pulizia": 0,
        "voci_dopo_triage": 0,
        "voci_classificate": 0,
        "voci_scritte": 0,
    }

    for w in todo:
        if on_progress:
            on_progress("finestra", 0, 1, {"window": w.key})

        raw = extractor.extract(history_path, w.start, w.end, cfg["source"]["browser"])
        stats["voci_grezze"] += len(raw)
        if on_progress:
            on_progress("estrazione", len(raw), len(raw), {"window": w.key})

        cleaned = cleaner.clean(raw, cfg["filtering"])
        stats["voci_dopo_pulizia"] += len(cleaned)
        if on_progress:
            on_progress("pulizia", len(cleaned), len(raw), {"window": w.key})

        survivors = triage.triage(
            cleaned, client, cfg["triage"], cfg["llm"]["triage_model"]
        )
        stats["voci_dopo_triage"] += len(survivors)
        if on_progress:
            on_progress("triage", len(survivors), len(cleaned), {"window": w.key})

        if args.dry_run:
            # Nessuna classificazione/scrittura in dry-run.
            completed.add(w.key)
            windowing.save_checkpoint(CHECKPOINT_PATH, completed)
            continue

        classified = classifier.classify(survivors, client, cfg)
        stats["voci_classificate"] += len(classified)
        if on_progress:
            on_progress("classificazione", len(classified), len(survivors), {"window": w.key})

        written = writer.write(classified)
        stats["voci_scritte"] += written

        completed.add(w.key)
        windowing.save_checkpoint(CHECKPOINT_PATH, completed)
        if on_progress:
            on_progress("scrittura", written, len(classified), {"window": w.key})

    stats["costi"] = tracker.as_dict()
    _write_log(stats)
    return stats


def _write_log(stats: dict) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = LOGS_DIR / f"run_{today}.json"
    # Se esiste gia' un log per oggi, non sovrascrivere: aggiungi un suffisso.
    if path.exists():
        i = 2
        while (LOGS_DIR / f"run_{today}_{i}.json").exists():
            i += 1
        path = LOGS_DIR / f"run_{today}_{i}.json"
    payload = dict(stats)
    payload["timestamp"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _print_summary(stats: dict) -> None:
    print("\n=== Riepilogo esecuzione ===")
    print(f"Periodo:            {stats['periodo']['from']} -> {stats['periodo']['to']}")
    print(f"Granularita':       {stats['group_by']}")
    print(f"Finestre:           {stats['finestre_da_processare']}/{stats['finestre_totali']} processate")
    print(f"Voci grezze:        {stats['voci_grezze']}")
    print(f"Dopo pulizia:       {stats['voci_dopo_pulizia']}")
    print(f"Dopo triage:        {stats['voci_dopo_triage']}")
    print(f"Classificate:       {stats['voci_classificate']}")
    print(f"Scritte (nuove):    {stats['voci_scritte']}")
    c = stats.get("costi", {})
    print(
        f"Token stimati:      in={c.get('input_tokens_stimati', 0)} "
        f"out={c.get('output_tokens_stimati', 0)}  "
        f"(~${c.get('costo_usd_stimato', 0)})"
    )


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        stats = run(args, on_progress=_cli_progress)
    except (ValueError, FileNotFoundError) as e:
        print(f"Errore: {e}", file=sys.stderr)
        return 2
    _print_summary(stats)
    return 0


def _cli_progress(stage: str, done: int, total: int, extra: dict) -> None:
    win = extra.get("window", "")
    print(f"[{win}] {stage}: {done}/{total}")


if __name__ == "__main__":
    raise SystemExit(main())
