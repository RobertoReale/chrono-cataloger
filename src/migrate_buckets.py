"""Move existing ``days:N`` output onto the fixed-epoch bucket naming.

``days:N`` folders used to be numbered from the start of the period being
processed, so the same day landed in a differently named folder depending on
when the run happened to start. They are now anchored to 1970-01-01, which
means output written before that change no longer lines up with what a new run
produces: you end up with two naming conventions side by side.

This script rewrites the old folders into the new names, splitting and merging
where a day now belongs somewhere else::

    python -m src.migrate_buckets --group-by days:10            # what it would do
    python -m src.migrate_buckets --group-by days:10 --apply    # do it

Only ``days:N`` is affected — ``month``, ``week`` and ``all`` never depended on
the period. Nothing else about the archive changes: the idempotency hashes in
``state/processed_ids.json`` are built from (url, category) and carry no bucket,
so they stay valid and a later run still refuses to write the same entry twice.

Rows are placed by the date they carry, so ``md_rich`` and ``md_journal`` can be
split exactly. ``txt`` and ``md`` keep no date per line: such a folder can only
be moved whole, and is left alone (with a message) when its days now straddle
two buckets. Files holding hand-written text are also left alone rather than
being reformatted — the point of this script is to lose nothing.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from .writer import (
    _TABLE_HEADER,
    _TOC_CLOSE,
    _TOC_OPEN,
    _days_of,
    _first_heading,
    _is_data_row,
    _split_sections,
    Writer,
)

_BUCKET_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})$")
_ROW_DATE_RE = re.compile(r"^\| (\d{4}-\d{2}-\d{2}) \|")
_TABLE_LINES = frozenset(_TABLE_HEADER.split("\n"))

#: Lines a generated ``md_journal`` preamble is made of, beyond the heading.
_PREAMBLE_BOILERPLATE = ("*Source: Chrome history*",)
#: Same, for a ``md_rich`` category file.
_RICH_BOILERPLATE_RE = re.compile(r"^\*Period: .*\*$")


@dataclass
class BucketPlan:
    """What the migration would do with one old bucket."""

    source: Path
    kind: str                                        # journal | md_rich | flat
    status: str = "move"                             # move | aligned | blocked
    note: str = ""
    targets: dict[str, int] = field(default_factory=dict)
    undated: int = 0
    #: kind-specific content, filled during analysis and reused when applying.
    payload: object = None

    def describe(self) -> list[str]:
        head = f"{self.source.name}  [{self.kind}]"
        if self.status == "aligned":
            return [f"{head}: already correct"]
        if self.status == "blocked":
            return [f"{head}: SKIPPED — {self.note}"]
        lines = [head]
        for bucket, count in sorted(self.targets.items()):
            lines.append(f"    -> {bucket}  ({count} entries)")
        if self.undated:
            lines.append(
                f"    note: {self.undated} entries carry no date and follow the "
                "first day of the old folder"
            )
        return lines


# --- helpers ------------------------------------------------------------- #
def _parse_bucket(name: str) -> tuple[date, date] | None:
    m = _BUCKET_RE.match(name)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
    except ValueError:
        return None


def _bucket_of(w: Writer, day: date) -> str:
    return w.bucket_name(datetime(day.year, day.month, day.day, tzinfo=timezone.utc))


def _row_date(line: str, fallback: date) -> tuple[date, bool]:
    m = _ROW_DATE_RE.match(line)
    if not m:
        return fallback, True
    try:
        return date.fromisoformat(m.group(1)), False
    except ValueError:
        return fallback, True


def _new_lines(existing: list[str], rows: list[str]) -> list[str]:
    """The rows not already present, so re-running the migration adds nothing."""
    have = set(existing)
    out: list[str] = []
    for row in rows:
        if row in have:
            continue
        have.add(row)
        out.append(row)
    return out


def _writer(base_dir: Path, group_by: str, file_format: str,
            category_order: list[str]) -> Writer:
    return Writer(
        base_dir=base_dir,
        group_by=group_by,
        file_format=file_format,
        period_start=datetime(1970, 1, 1, tzinfo=timezone.utc),
        # Never read and never written: the migration leaves the idempotency
        # state alone, because the hashes do not encode the bucket.
        processed_ids_path=base_dir / "__migration_never_written__.json",
        category_order=category_order,
    )


def _sources(base: Path) -> list[Path]:
    out: list[Path] = []
    for p in sorted(base.iterdir()):
        if p.is_dir() and _parse_bucket(p.name):
            out.append(p)
        elif p.is_file() and p.suffix == ".md" and _parse_bucket(p.stem):
            out.append(p)
    return out


def _bucket_range(p: Path) -> tuple[date, date]:
    parsed = _parse_bucket(p.stem if p.is_file() else p.name)
    assert parsed is not None  # only ever called on a path from _sources
    return parsed


def _detect_kind(p: Path) -> str:
    if p.is_file():
        return "journal"
    for f in p.iterdir():
        if f.suffix == ".md" and f.name != "README.md":
            text = f.read_text(encoding="utf-8", errors="replace")
            if _TABLE_HEADER.split("\n")[0] in text:
                return "md_rich"
    return "flat"


# --- analysis ------------------------------------------------------------ #
def _custom_preamble(preamble: str, bucket: str) -> bool:
    """True when the journal preamble holds anything we did not generate."""
    in_toc = False
    for line in preamble.splitlines():
        stripped = line.strip()
        if stripped == _TOC_OPEN:
            in_toc = True
            continue
        if stripped == _TOC_CLOSE:
            in_toc = False
            continue
        if in_toc or not stripped:
            continue
        if stripped == f"# {bucket}" or stripped in _PREAMBLE_BOILERPLATE:
            continue
        return True
    return False


def _analyze_journal(path: Path, w: Writer, plan: BucketPlan) -> None:
    start, _ = _bucket_range(path)
    text = path.read_text(encoding="utf-8")
    preamble, sections = _split_sections(text)
    if _custom_preamble(preamble, path.stem):
        plan.status, plan.note = "blocked", "its preamble holds hand-written text"
        return

    payload: dict[str, dict[str, list[str]]] = {}
    for category, body in sections.items():
        for line in body:
            if not line.strip() or line in _TABLE_LINES:
                continue
            if not _is_data_row(line):
                plan.status = "blocked"
                plan.note = f'the "{category}" section holds hand-written text'
                return
            day, guessed = _row_date(line, start)
            plan.undated += guessed
            bucket = _bucket_of(w, day)
            payload.setdefault(bucket, {}).setdefault(category, []).append(line)
            plan.targets[bucket] = plan.targets.get(bucket, 0) + 1
    plan.payload = payload


def _analyze_rich(path: Path, w: Writer, plan: BucketPlan) -> None:
    start, _ = _bucket_range(path)
    # bucket -> file name -> (category name, rows)
    payload: dict[str, dict[str, tuple[str, list[str]]]] = {}
    for f in sorted(path.iterdir()):
        if f.name == "README.md" or f.suffix != ".md":
            continue
        category = _first_heading(f) or f.stem
        for line in f.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or line in _TABLE_LINES:
                continue
            if stripped == f"# {category}" or _RICH_BOILERPLATE_RE.match(stripped):
                continue
            if not _is_data_row(line):
                plan.status = "blocked"
                plan.note = f"{f.name} holds hand-written text"
                return
            day, guessed = _row_date(line, start)
            plan.undated += guessed
            bucket = _bucket_of(w, day)
            slot = payload.setdefault(bucket, {}).setdefault(f.name, (category, []))
            slot[1].append(line)
            plan.targets[bucket] = plan.targets.get(bucket, 0) + 1
    plan.payload = payload


def _analyze_flat(path: Path, w: Writer, plan: BucketPlan) -> None:
    start, end = _bucket_range(path)
    first, last = _bucket_of(w, start), _bucket_of(w, end)
    if first != last:
        plan.status = "blocked"
        plan.note = (
            f"its lines carry no date and its days now split across "
            f"{first} and {last} — move it by hand"
        )
        return
    count = 0
    for f in sorted(path.iterdir()):
        if f.is_file():
            count += sum(1 for line in f.read_text(encoding="utf-8").splitlines()
                         if line.strip())
    plan.targets[first] = count
    plan.payload = first


_ANALYZERS = {"journal": _analyze_journal, "md_rich": _analyze_rich, "flat": _analyze_flat}


def plan_migration(base_dir: Path, group_by: str,
                   category_order: list[str] | None = None) -> list[BucketPlan]:
    """Work out what every old bucket under ``base_dir`` should become."""
    if _days_of(group_by) is None:
        raise ValueError(
            f"only days:N output needs migrating, got {group_by!r} "
            "(month, week and all never depended on the period)"
        )
    base = Path(base_dir)
    if not base.is_dir():
        raise FileNotFoundError(f"output folder not found: {base}")

    plans: list[BucketPlan] = []
    for source in _sources(base):
        kind = _detect_kind(source)
        w = _writer(base, group_by,
                    {"journal": "md_journal", "md_rich": "md_rich"}.get(kind, "txt"),
                    list(category_order or []))
        plan = BucketPlan(source=source, kind=kind)
        name = source.stem if source.is_file() else source.name
        if _bucket_of(w, _bucket_range(source)[0]) == name:
            plan.status = "aligned"
            plans.append(plan)
            continue
        _ANALYZERS[kind](source, w, plan)
        if plan.status == "move" and not plan.targets:
            plan.status = "aligned"
            plan.note = "nothing to move"
        plans.append(plan)
    return plans


# --- application --------------------------------------------------------- #
def _apply_journal(plan: BucketPlan, w: Writer) -> None:
    payload: dict[str, dict[str, list[str]]] = plan.payload or {}
    for bucket, by_category in payload.items():
        target = w.base_dir / f"{bucket}.md"
        existing = _split_sections(
            target.read_text(encoding="utf-8")
        )[1] if target.exists() else {}
        fresh = {
            category: _new_lines(existing.get(category, []), rows)
            for category, rows in by_category.items()
        }
        fresh = {c: rows for c, rows in fresh.items() if rows}
        if fresh:
            w._write_journal(bucket, fresh)
    plan.source.unlink()


def _apply_rich(plan: BucketPlan, w: Writer) -> None:
    payload: dict[str, dict[str, tuple[str, list[str]]]] = plan.payload or {}
    for bucket, by_file in payload.items():
        folder = w.base_dir / bucket
        folder.mkdir(parents=True, exist_ok=True)
        for file_name, (category, rows) in by_file.items():
            target = folder / file_name
            if target.exists():
                existing = target.read_text(encoding="utf-8").splitlines()
                rows = _new_lines(existing, rows)
                if not rows:
                    continue
                with target.open("a", encoding="utf-8") as fh:
                    fh.write("\n".join(rows) + "\n")
            else:
                target.write_text(
                    w._file_header(category, bucket) + "\n".join(rows) + "\n",
                    encoding="utf-8",
                )
        w._write_index(folder, bucket)
    shutil.rmtree(plan.source)


def _apply_flat(plan: BucketPlan, w: Writer) -> None:
    bucket: str = plan.payload  # type: ignore[assignment]
    folder = w.base_dir / bucket
    folder.mkdir(parents=True, exist_ok=True)
    for f in sorted(plan.source.iterdir()):
        if not f.is_file():
            continue
        target = folder / f.name
        rows = f.read_text(encoding="utf-8").splitlines()
        existing = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
        rows = _new_lines(existing, rows)
        if not rows:
            continue
        with target.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(rows) + "\n")
    shutil.rmtree(plan.source)


_APPLIERS = {"journal": _apply_journal, "md_rich": _apply_rich, "flat": _apply_flat}


def apply_migration(base_dir: Path, group_by: str, plans: list[BucketPlan],
                    category_order: list[str] | None = None) -> int:
    """Carry out the moves in ``plans``; returns how many buckets were rewritten.

    New content is written before the old bucket is removed, so an interrupted
    run leaves duplicates rather than a hole — and re-running drops them, since
    every merge skips rows the target already has.
    """
    base = Path(base_dir)
    done = 0
    for plan in plans:
        if plan.status != "move":
            continue
        w = _writer(base, group_by,
                    {"journal": "md_journal", "md_rich": "md_rich"}.get(plan.kind, "txt"),
                    list(category_order or []))
        _APPLIERS[plan.kind](plan, w)
        done += 1
    return done


# --- CLI ----------------------------------------------------------------- #
def _config_defaults(config_path: str) -> tuple[str | None, str | None, list[str]]:
    try:
        from .config import load_config
        cfg = load_config(config_path, on_warning=lambda _m: None)
    except (FileNotFoundError, ValueError):
        return None, None, []
    out = cfg.get("output", {})
    categories = [
        c.get("name", "") for c in cfg.get("classification", {}).get("categories", [])
        if isinstance(c, dict)
    ]
    return out.get("base_dir"), out.get("group_by"), [c for c in categories if c]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Move days:N output onto the fixed-epoch bucket naming."
    )
    ap.add_argument("--config", default="config.yaml",
                    help="config to read base_dir/group_by from (default: config.yaml)")
    ap.add_argument("--base-dir", help="output folder (overrides the config)")
    ap.add_argument("--group-by", help="the days:N granularity in use (overrides the config)")
    ap.add_argument("--apply", action="store_true",
                    help="actually move the files (without it, only prints the plan)")
    args = ap.parse_args(argv)

    cfg_base, cfg_group, categories = _config_defaults(args.config)
    base_dir = args.base_dir or cfg_base
    group_by = args.group_by or cfg_group
    if not base_dir:
        print("No output folder: pass --base-dir or point --config at a valid config.",
              file=sys.stderr)
        return 2
    if not group_by:
        print("No granularity: pass --group-by days:N.", file=sys.stderr)
        return 2

    try:
        plans = plan_migration(Path(base_dir), group_by, categories)
    except (ValueError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    movable = [p for p in plans if p.status == "move"]
    blocked = [p for p in plans if p.status == "blocked"]
    for plan in plans:
        for line in plan.describe():
            print(line)
    if not plans:
        print(f"No days:N folders found under {base_dir}.")
        return 0
    if not movable:
        print("\nNothing to move.")
        return 0

    if not args.apply:
        print(f"\n{len(movable)} bucket(s) would be rewritten. "
              "Re-run with --apply to do it.")
        return 0

    done = apply_migration(Path(base_dir), group_by, plans, categories)
    print(f"\n{done} bucket(s) rewritten.")
    if blocked:
        print(f"{len(blocked)} left untouched — see the messages above.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
