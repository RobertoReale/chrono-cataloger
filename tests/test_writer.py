from datetime import datetime, timezone

from src.models import ClassifiedEntry, datetime_to_webkit_micros
from src.writer import Writer, slugify


def _ce(cat, summary, url="", y=2026, m=7, d=10):
    micros = datetime_to_webkit_micros(datetime(y, m, d, 12, tzinfo=timezone.utc))
    return ClassifiedEntry(
        category=cat, summary=summary, url=url,
        last_visit_micros=micros, normalized_url=url or f"https://x/{summary}",
    )


def test_slugify():
    assert slugify("Philosophy and History") == "philosophy-and-history"
    assert slugify("Concepts / Ideas") == "concepts-ideas"


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
    n = w.write([_ce("Books", "Spinoza's Ethics")])
    assert n == 1
    f = tmp_path / "out" / "2026-07" / "books.txt"
    assert f.exists()
    assert "Spinoza's Ethics" in f.read_text(encoding="utf-8")


def test_write_is_idempotent(tmp_path):
    w = _writer(tmp_path)
    e = _ce("Books", "Spinoza's Ethics", url="https://books/spinoza")
    assert w.write([e]) == 1
    # writing the same entry again -> 0 new lines
    w2 = _writer(tmp_path)  # reloads processed_ids from disk
    assert w2.write([e]) == 0
    lines = (tmp_path / "out" / "2026-07" / "books.txt").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_write_routes_by_visit_date_not_run_date(tmp_path):
    w = _writer(tmp_path, group_by="month")
    w.write([_ce("Books", "A", d=10, m=7), _ce("Books", "B", d=2, m=8)])
    assert (tmp_path / "out" / "2026-07" / "books.txt").exists()
    assert (tmp_path / "out" / "2026-08" / "books.txt").exists()


def test_group_by_days_buckets(tmp_path):
    w = _writer(tmp_path, group_by="days:10")
    # period_start = 2026-07-01; day 3 -> bucket 0 (01-10); day 15 -> bucket 1 (11-20)
    w.write([_ce("Books", "A", d=3), _ce("Books", "B", d=15)])
    names = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert "2026-07-01_2026-07-10" in names
    assert "2026-07-11_2026-07-20" in names


def test_group_by_all_single_folder(tmp_path):
    w = _writer(tmp_path, group_by="all")
    w.write([_ce("Books", "A", d=3, m=7), _ce("Books", "B", d=2, m=8)])
    subdirs = [p.name for p in (tmp_path / "out").iterdir()]
    assert subdirs == ["whole-period"]


def test_md_format(tmp_path):
    w = Writer(
        base_dir=tmp_path / "out", group_by="month", file_format="md",
        period_start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        processed_ids_path=tmp_path / "state" / "p.json",
    )
    w.write([_ce("Books", "Spinoza", url="https://x")])
    content = (tmp_path / "out" / "2026-07" / "books.md").read_text(encoding="utf-8")
    assert content.startswith("- ")
    assert "(https://x)" in content
