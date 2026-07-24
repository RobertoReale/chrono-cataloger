"""Scrittura idempotente dei file di output, per periodo e categoria.

Instrada ogni voce classificata nella sotto-cartella giusta in base al suo
``last_visit_time`` ORIGINALE (non alla data di elaborazione), secondo la
granularita' ``--group-by``. Evita duplicati tramite un hash (url normalizzato +
categoria) registrato in ``state/processed_ids.json``.
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
    """Trasforma un nome categoria in uno slug di file sicuro.

    "Filosofia e Storia" -> "filosofia-e-storia"
    """
    normalized = unicodedata.normalize("NFKD", name)
    ascii_str = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_str = ascii_str.lower()
    ascii_str = re.sub(r"[^a-z0-9]+", "-", ascii_str).strip("-")
    return ascii_str or "senza-categoria"


def _hash_entry(normalized_url: str, categoria: str) -> str:
    h = hashlib.sha256(f"{normalized_url}\x00{categoria}".encode("utf-8"))
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
    """Scrive le voci classificate nei file, in modo idempotente."""

    def __init__(
        self,
        base_dir: str | Path,
        group_by: str,
        file_format: str,
        period_start: datetime,
        processed_ids_path: str | Path,
    ):
        self.base_dir = Path(base_dir)
        self.group_by = group_by
        self.file_format = file_format
        self.period_start = period_start.astimezone(timezone.utc)
        self.processed_ids_path = Path(processed_ids_path)
        self.processed = load_processed_ids(processed_ids_path)

    # --- Bucketing temporale --------------------------------------------- #
    def bucket_name(self, visit: datetime) -> str:
        """Nome della sotto-cartella periodo per una data di visita."""
        visit = visit.astimezone(timezone.utc)
        gb = self.group_by
        if gb == "month":
            return visit.strftime("%Y-%m")
        if gb == "week":
            iso = visit.isocalendar()
            return f"{iso.year}-W{iso.week:02d}"
        if gb == "all":
            return "tutto-il-periodo"
        if gb.startswith("days:"):
            n = int(gb.split(":", 1)[1])
            delta_days = (visit.date() - self.period_start.date()).days
            k = delta_days // n
            bucket_start = self.period_start.date() + timedelta(days=k * n)
            bucket_end = bucket_start + timedelta(days=n - 1)
            return f"{bucket_start.isoformat()}_{bucket_end.isoformat()}"
        raise ValueError(f"group_by non valido: {self.group_by!r}")

    # --- Formattazione riga ---------------------------------------------- #
    def format_line(self, entry: ClassifiedEntry) -> str:
        sintesi = entry.sintesi.strip()
        url = (entry.url or "").strip()
        if url:
            text = f"{sintesi} ({url})"
        else:
            text = sintesi
        if self.file_format == "md":
            return f"- {text}"
        return text

    # --- Scrittura -------------------------------------------------------- #
    def write(self, entries: list[ClassifiedEntry]) -> int:
        """Scrive le voci nuove; ritorna quante ne sono state effettivamente aggiunte.

        Le voci il cui hash e' gia' in processed_ids sono ignorate (idempotenza).
        """
        # Raggruppa per (bucket, categoria) accumulando le nuove righe.
        buckets: dict[tuple[str, str], list[str]] = {}
        newly_processed: list[str] = []

        for entry in entries:
            norm = entry.normalized_url or entry.url or ""
            h = _hash_entry(norm, entry.categoria)
            if h in self.processed:
                continue

            if entry.last_visit_micros is None:
                # senza timestamp non sappiamo il bucket: usa 'all'
                visit = self.period_start
            else:
                visit = webkit_micros_to_datetime(entry.last_visit_micros)

            bucket = self.bucket_name(visit)
            key = (bucket, slugify(entry.categoria))
            buckets.setdefault(key, []).append(self.format_line(entry))

            self.processed.add(h)
            newly_processed.append(h)

        # Append effettivo su disco.
        ext = "md" if self.file_format == "md" else "txt"
        written = 0
        for (bucket, cat_slug), lines in buckets.items():
            folder = self.base_dir / bucket
            folder.mkdir(parents=True, exist_ok=True)
            file_path = folder / f"{cat_slug}.{ext}"
            with file_path.open("a", encoding="utf-8") as f:
                for line in lines:
                    f.write(line + "\n")
                    written += 1

        if newly_processed:
            save_processed_ids(self.processed_ids_path, self.processed)

        return written
