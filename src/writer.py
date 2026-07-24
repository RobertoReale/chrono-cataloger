"""Idempotent writing of the output files, by period and category.

Routes every classified entry into the right sub-folder based on its ORIGINAL
``last_visit_time`` (not the processing date), following the ``--group-by``
granularity. Duplicates are avoided through a hash (normalized url + category)
recorded in ``state/processed_ids.json``.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import ClassifiedEntry, webkit_micros_to_datetime


def slugify(name: str) -> str:
    """Turn a category name into a safe file slug.

    "Philosophy and History" -> "philosophy-and-history"
    """
    normalized = unicodedata.normalize("NFKD", name)
    ascii_str = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_str = ascii_str.lower()
    ascii_str = re.sub(r"[^a-z0-9]+", "-", ascii_str).strip("-")
    return ascii_str or "uncategorized"


def _md_cell(text: str) -> str:
    """Make a string safe inside a markdown table cell (no pipes, no newlines)."""
    return re.sub(r"\s+", " ", text.replace("|", "\\|")).strip()


def _link_label(url: str) -> str:
    """Short, readable label for a link: the domain, without the ``www.`` prefix."""
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://(?:www\.)?([^/?#]+)", url)
    return m.group(1) if m else url


_TABLE_HEADER: str = "| Date | What I learned | Source |\n| --- | --- | --- |"

# The table of contents of a journal file lives between these markers, so it can
# be regenerated without touching anything the user wrote around it.
_TOC_OPEN = "<!-- toc -->"
_TOC_CLOSE = "<!-- /toc -->"


def _split_sections(text: str) -> tuple[str, dict[str, list[str]]]:
    """Split a journal file into its preamble and its ``## Category`` sections.

    The section bodies are returned verbatim so that re-writing the file never
    reformats (or loses) what is already there.
    """
    preamble: list[str] = []
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
        elif current is None:
            preamble.append(line)
        else:
            sections[current].append(line)
    return "\n".join(preamble).strip("\n"), sections


def _render_toc(preamble: str, categories: list[str]) -> str:
    """Refresh the TOC between the markers; leave the preamble alone if absent."""
    if _TOC_OPEN not in preamble or _TOC_CLOSE not in preamble:
        return preamble
    links = "\n".join(f"- [{c}](#{slugify(c)})" for c in categories)
    head, rest = preamble.split(_TOC_OPEN, 1)
    _, tail = rest.split(_TOC_CLOSE, 1)
    return f"{head}{_TOC_OPEN}\n{links}\n{_TOC_CLOSE}{tail}"


def _is_data_row(line: str) -> bool:
    """True for a table row written by ``_format_table_row`` (not header/separator)."""
    return bool(re.match(r"^\| (?:\d{4}-\d{2}-\d{2}|—) \|", line))


def _first_heading(path: Path) -> str:
    """The ``# Title`` of a markdown file, if present."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    except OSError:
        pass
    return ""


def _hash_entry(normalized_url: str, category: str) -> str:
    h = hashlib.sha256(f"{normalized_url}\x00{category}".encode("utf-8"))
    return h.hexdigest()


def load_processed_ids(path: str | Path) -> set[str]:
    p = Path(path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data.get("processed", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_processed_ids(path: str | Path, processed: set[str]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"processed": sorted(processed)}
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


class Writer:
    """Writes the classified entries to files, idempotently."""

    def __init__(
        self,
        base_dir: str | Path,
        group_by: str,
        file_format: str,
        period_start: datetime,
        processed_ids_path: str | Path,
        category_order: list[str] | None = None,
    ):
        self.base_dir = Path(base_dir)
        self.group_by = group_by
        self.file_format = file_format
        # Order the sections of a md_journal file follow; anything not listed
        # here (a category renamed in the config, say) is kept after them.
        self.category_order = list(category_order or [])
        self.period_start = period_start.astimezone(timezone.utc)
        self.processed_ids_path = Path(processed_ids_path)
        self.processed = load_processed_ids(processed_ids_path)

    # --- Time bucketing --------------------------------------------------- #
    def bucket_name(self, visit: datetime) -> str:
        """Name of the period sub-folder for a given visit date."""
        visit = visit.astimezone(timezone.utc)
        gb = self.group_by
        if gb == "month":
            return visit.strftime("%Y-%m")
        if gb == "week":
            iso = visit.isocalendar()
            return f"{iso.year}-W{iso.week:02d}"
        if gb == "all":
            return "whole-period"
        if gb.startswith("days:"):
            n = int(gb.split(":", 1)[1])
            delta_days = (visit.date() - self.period_start.date()).days
            k = delta_days // n
            bucket_start = self.period_start.date() + timedelta(days=k * n)
            bucket_end = bucket_start + timedelta(days=n - 1)
            return f"{bucket_start.isoformat()}_{bucket_end.isoformat()}"
        raise ValueError(f"invalid group_by: {self.group_by!r}")

    # --- Line formatting -------------------------------------------------- #
    def format_line(self, entry: ClassifiedEntry, visit: datetime | None = None) -> str:
        summary = entry.summary.strip()
        url = (entry.url or "").strip()
        if self.file_format in ("md_rich", "md_journal"):
            return self._format_table_row(entry, visit)
        if url:
            text = f"{summary} ({url})"
        else:
            text = summary
        if self.file_format == "md":
            return f"- {text}"
        return text

    def _format_table_row(self, entry: ClassifiedEntry, visit: datetime | None) -> str:
        date = visit.astimezone(timezone.utc).strftime("%Y-%m-%d") if visit else "—"
        summary = _md_cell(entry.summary)
        url = (entry.url or "").strip()
        source = f"[{_md_cell(_link_label(url))}]({url})" if url else "—"
        return f"| {date} | {summary} | {source} |"

    def _file_header(self, category: str, bucket: str) -> str:
        """Front matter written once, when a ``md_rich`` file is first created."""
        return (
            f"# {category}\n\n"
            f"*Period: {bucket}* · *Source: Chrome history*\n\n"
            f"{_TABLE_HEADER}\n"
        )

    # --- Index ------------------------------------------------------------ #
    @staticmethod
    def _count_rows(file_path: Path) -> int:
        """Number of data rows in a ``md_rich`` table (header and separator excluded)."""
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        return sum(1 for line in lines if _is_data_row(line))

    def _write_index(self, folder: Path, bucket: str) -> None:
        """(Re)generate ``README.md``: one table with the categories of the period."""
        files = sorted(
            p for p in folder.glob("*.md") if p.name != "README.md"
        )
        rows = []
        total = 0
        for p in files:
            title = _first_heading(p) or p.stem
            count = self._count_rows(p)
            total += count
            rows.append(f"| [{_md_cell(title)}]({p.name}) | {count} |")

        content = (
            f"# {bucket}\n\n"
            f"*{total} entries across {len(files)} categories.*\n\n"
            "| Category | Entries |\n"
            "| --- | ---: |\n" + "\n".join(rows) + "\n"
        )
        (folder / "README.md").write_text(content, encoding="utf-8")

    # --- Journal (one file per period, one section per category) ----------- #
    def _journal_preamble(self, bucket: str) -> str:
        return (
            f"# {bucket}\n\n"
            "*Source: Chrome history*\n\n"
            f"{_TOC_OPEN}\n{_TOC_CLOSE}"
        )

    def _section_order(self, names: list[str]) -> list[str]:
        """Configured categories first, in config order; the rest keeps its own."""
        known = [c for c in self.category_order if c in names]
        rest = [n for n in names if n not in known]
        return known + rest

    def _write_journal(self, bucket: str, new_rows: dict[str, list[str]]) -> None:
        """Merge the new rows into ``<bucket>.md``, one ``##`` section per category.

        Unlike the other formats this rewrites the file instead of appending, so
        rows can land inside the right section. Everything already on disk is
        preserved verbatim: only the TOC is regenerated.
        """
        path = self.base_dir / f"{bucket}.md"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        preamble, sections = _split_sections(text)
        if not preamble:
            preamble = self._journal_preamble(bucket)

        for category, rows in new_rows.items():
            if category not in sections:
                sections[category] = _TABLE_HEADER.split("\n")
            table = sections[category]
            # Drop the blank lines the previous write left at the end, so the new
            # rows stay attached to the table.
            while table and not table[-1].strip():
                table.pop()
            table.extend(rows)

        order = self._section_order(list(sections))
        preamble = _render_toc(preamble, order)

        parts = [preamble]
        for category in order:
            body = "\n".join(sections[category]).strip("\n")
            parts.append(f"## {category}\n\n{body}")
        content = "\n\n".join(parts) + "\n"

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)

    # --- Writing ---------------------------------------------------------- #
    def write(self, entries: list[ClassifiedEntry]) -> int:
        """Write the new entries; returns how many were actually added.

        Entries whose hash is already in processed_ids are skipped (idempotency).
        """
        # Group by (bucket, category), accumulating the new lines.
        buckets: dict[tuple[str, str], list[str]] = {}
        # Original category names, kept for the headings of the md_rich files.
        cat_names: dict[str, str] = {}
        newly_processed: list[str] = []

        for entry in entries:
            norm = entry.normalized_url or entry.url or ""
            h = _hash_entry(norm, entry.category)
            if h in self.processed:
                continue

            if entry.last_visit_micros is None:
                # without a timestamp we cannot tell the bucket: fall back to the period start
                visit = self.period_start
            else:
                visit = webkit_micros_to_datetime(entry.last_visit_micros)

            bucket = self.bucket_name(visit)
            cat_slug = slugify(entry.category)
            key = (bucket, cat_slug)
            buckets.setdefault(key, []).append(self.format_line(entry, visit))
            cat_names.setdefault(cat_slug, entry.category)

            self.processed.add(h)
            newly_processed.append(h)

        # md_journal keeps every category of a period in a single file, so it
        # merges sections instead of appending to one file per category.
        if self.file_format == "md_journal":
            written = 0
            per_bucket: dict[str, dict[str, list[str]]] = {}
            for (bucket, cat_slug), lines in buckets.items():
                per_bucket.setdefault(bucket, {})[cat_names[cat_slug]] = lines
                written += len(lines)
            for bucket, by_category in per_bucket.items():
                self._write_journal(bucket, by_category)
            if newly_processed:
                save_processed_ids(self.processed_ids_path, self.processed)
            return written

        # Actual append to disk.
        ext = "txt" if self.file_format == "txt" else "md"
        written = 0
        for (bucket, cat_slug), lines in buckets.items():
            folder = self.base_dir / bucket
            folder.mkdir(parents=True, exist_ok=True)
            file_path = folder / f"{cat_slug}.{ext}"
            is_new = not file_path.exists()
            with file_path.open("a", encoding="utf-8") as f:
                if is_new and self.file_format == "md_rich":
                    f.write(self._file_header(cat_names[cat_slug], bucket))
                for line in lines:
                    f.write(line + "\n")
                    written += 1

        if self.file_format == "md_rich":
            for bucket in {b for b, _ in buckets}:
                self._write_index(self.base_dir / bucket, bucket)

        if newly_processed:
            save_processed_ids(self.processed_ids_path, self.processed)

        return written
