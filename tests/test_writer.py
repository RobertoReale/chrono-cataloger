from datetime import datetime, timezone

from src.models import ClassifiedEntry, datetime_to_webkit_micros
from src.writer import Writer, slugify


def _ce(cat, sintesi, url="", y=2026, m=7, d=10):
    micros = datetime_to_webkit_micros(datetime(y, m, d, 12, tzinfo=timezone.utc))
    return ClassifiedEntry(
        categoria=cat, sintesi=sintesi, url=url,
        last_visit_micros=micros, normalized_url=url or f"https://x/{sintesi}",
    )


def test_slugify():
    assert slugify("Filosofia e Storia") == "filosofia-e-storia"
    assert slugify("Concetti / Idee") == "concetti-idee"


def _writer(tmp_path, group_by="month"):
    return Writer(
        base_dir=tmp_path / "out",
        group_by=group_by,
        file_format="txt",
        period_start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        processed_ids_path=tmp_path / "state" / "processed_ids.json",
    )


def test_write_creates_month_folder_and_file(tmp_path):
    w = _writer(tmp_path)
    n = w.write([_ce("Libri", "Etica di Spinoza")])
    assert n == 1
    f = tmp_path / "out" / "2026-07" / "libri.txt"
    assert f.exists()
    assert "Etica di Spinoza" in f.read_text(encoding="utf-8")


def test_write_is_idempotent(tmp_path):
    w = _writer(tmp_path)
    e = _ce("Libri", "Etica di Spinoza", url="https://books/spinoza")
    assert w.write([e]) == 1
    # seconda scrittura stessa voce -> 0 nuove
    w2 = _writer(tmp_path)  # ricarica processed_ids da disco
    assert w2.write([e]) == 0
    lines = (tmp_path / "out" / "2026-07" / "libri.txt").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_write_routes_by_visit_date_not_run_date(tmp_path):
    w = _writer(tmp_path, group_by="month")
    w.write([_ce("Libri", "A", d=10, m=7), _ce("Libri", "B", d=2, m=8)])
    assert (tmp_path / "out" / "2026-07" / "libri.txt").exists()
    assert (tmp_path / "out" / "2026-08" / "libri.txt").exists()


def test_group_by_days_buckets(tmp_path):
    w = _writer(tmp_path, group_by="days:10")
    # period_start = 2026-07-01; giorno 3 -> bucket 0 (01-10); giorno 15 -> bucket 1 (11-20)
    w.write([_ce("Libri", "A", d=3), _ce("Libri", "B", d=15)])
    names = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert "2026-07-01_2026-07-10" in names
    assert "2026-07-11_2026-07-20" in names


def test_group_by_all_single_folder(tmp_path):
    w = _writer(tmp_path, group_by="all")
    w.write([_ce("Libri", "A", d=3, m=7), _ce("Libri", "B", d=2, m=8)])
    subdirs = [p.name for p in (tmp_path / "out").iterdir()]
    assert subdirs == ["tutto-il-periodo"]


def test_md_format(tmp_path):
    w = Writer(
        base_dir=tmp_path / "out", group_by="month", file_format="md",
        period_start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        processed_ids_path=tmp_path / "state" / "p.json",
    )
    w.write([_ce("Libri", "Spinoza", url="https://x")])
    content = (tmp_path / "out" / "2026-07" / "libri.md").read_text(encoding="utf-8")
    assert content.startswith("- ")
    assert "(https://x)" in content
