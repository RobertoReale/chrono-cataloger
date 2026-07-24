from datetime import datetime, timezone

import pytest

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


def test_slugify_falls_back_for_names_that_transliterate_to_nothing():
    # Would all collapse into one shared file if the fallback were a constant.
    slugs = {slugify("Программирование"), slugify("日本語"), slugify("🎧")}
    assert len(slugs) == 3
    assert all(s.startswith("category-") for s in slugs)


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
    # Buckets are 10-day blocks anchored to a fixed epoch, so two dates 12 days
    # apart land in two different folders.
    w.write([_ce("Books", "A", d=3), _ce("Books", "B", d=15)])
    names = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert names == ["2026-06-26_2026-07-05", "2026-07-06_2026-07-15"]


def test_group_by_days_buckets_do_not_depend_on_the_period(tmp_path):
    """The same visit date must land in the same folder whatever period is run."""
    july = _writer(tmp_path / "a", group_by="days:10")
    june = Writer(
        base_dir=tmp_path / "b" / "out",
        group_by="days:10",
        file_format="txt",
        period_start=datetime(2026, 6, 4, tzinfo=timezone.utc),
        processed_ids_path=tmp_path / "b" / "ids.json",
    )
    visit = datetime(2026, 7, 3, tzinfo=timezone.utc)
    assert july.bucket_name(visit) == june.bucket_name(visit)


def test_invalid_group_by_is_rejected_before_any_work(tmp_path):
    with pytest.raises(ValueError, match="invalid group_by"):
        _writer(tmp_path, group_by="days:x")


def test_slug_keeps_non_ascii_categories_apart(tmp_path):
    w = _writer(tmp_path, group_by="month")
    w.write([_ce("Программирование", "A"), _ce("日本語", "B")])
    files = sorted(p.name for p in (tmp_path / "out" / "2026-07").iterdir())
    assert len(files) == 2 and len(set(files)) == 2


def test_colliding_category_slugs_get_separate_files(tmp_path):
    w = Writer(
        base_dir=tmp_path / "out",
        group_by="month",
        file_format="txt",
        period_start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        processed_ids_path=tmp_path / "ids.json",
        category_order=["AI/ML", "AI & ML"],
    )
    w.write([_ce("AI/ML", "A"), _ce("AI & ML", "B")])
    files = sorted(p.name for p in (tmp_path / "out" / "2026-07").iterdir())
    assert len(files) == 2


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


def _rich_writer(tmp_path):
    return Writer(
        base_dir=tmp_path / "out", group_by="month", file_format="md_rich",
        period_start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        processed_ids_path=tmp_path / "state" / "p.json",
    )


def test_md_rich_writes_heading_and_table(tmp_path):
    w = _rich_writer(tmp_path)
    w.write([_ce("Books", "Spinoza's Ethics", url="https://www.example.com/spinoza")])
    content = (tmp_path / "out" / "2026-07" / "books.md").read_text(encoding="utf-8")
    assert content.startswith("# Books\n")
    assert "| Date | What I learned | Source |" in content
    assert "| --- | --- | --- |" in content
    assert "| 2026-07-10 | Spinoza's Ethics | [example.com](https://www.example.com/spinoza) |" in content


def test_md_rich_header_written_once(tmp_path):
    _rich_writer(tmp_path).write([_ce("Books", "A", url="https://x/a")])
    _rich_writer(tmp_path).write([_ce("Books", "B", url="https://x/b")])
    content = (tmp_path / "out" / "2026-07" / "books.md").read_text(encoding="utf-8")
    assert content.count("# Books") == 1
    assert content.count("| Date |") == 1
    assert content.rstrip().endswith("|")
    assert "A" in content and "B" in content


def test_md_rich_escapes_pipes_and_newlines(tmp_path):
    w = _rich_writer(tmp_path)
    w.write([_ce("Books", "a | b\nc")])
    row = [
        l for l in (tmp_path / "out" / "2026-07" / "books.md")
        .read_text(encoding="utf-8").splitlines() if l.startswith("| 2026")
    ]
    assert len(row) == 1
    # only the 4 cell delimiters are unescaped pipes
    assert row[0].count("|") - row[0].count("\\|") == 4
    assert "a \\| b c" in row[0]


def _journal_writer(tmp_path, category_order=None):
    return Writer(
        base_dir=tmp_path / "out", group_by="month", file_format="md_journal",
        period_start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        processed_ids_path=tmp_path / "state" / "p.json",
        category_order=category_order,
    )


def _journal(tmp_path):
    return (tmp_path / "out" / "2026-07.md").read_text(encoding="utf-8")


def test_md_journal_single_file_with_sections(tmp_path):
    w = _journal_writer(tmp_path)
    assert w.write([_ce("Books", "A"), _ce("Videos", "B")]) == 2
    content = _journal(tmp_path)
    assert content.startswith("# 2026-07\n")
    assert "## Books" in content and "## Videos" in content
    assert content.count("| Date | What I learned | Source |") == 2
    assert "- [Books](#books)" in content
    # no per-category files, and no period sub-folder
    assert not (tmp_path / "out" / "2026-07").exists()


def test_md_journal_appends_into_existing_section(tmp_path):
    _journal_writer(tmp_path).write([_ce("Books", "A", url="https://x/a")])
    _journal_writer(tmp_path).write([_ce("Books", "B", url="https://x/b")])
    content = _journal(tmp_path)
    assert content.count("## Books") == 1
    assert content.count("| Date | What I learned | Source |") == 1
    rows = [l for l in content.splitlines() if l.startswith("| 2026")]
    assert len(rows) == 2
    assert "A" in rows[0] and "B" in rows[1]


def test_md_journal_follows_configured_category_order(tmp_path):
    w = _journal_writer(tmp_path, category_order=["Videos", "Books"])
    w.write([_ce("Books", "A"), _ce("Videos", "B")])
    content = _journal(tmp_path)
    assert content.index("## Videos") < content.index("## Books")
    assert content.index("- [Videos]") < content.index("- [Books]")


def test_md_journal_preserves_handwritten_content(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "2026-07.md").write_text(
        "# Luglio 2026\n\nmia nota personale\n\n"
        "## Books\n\nriga scritta a mano\n",
        encoding="utf-8",
    )
    _journal_writer(tmp_path).write([_ce("Books", "A")])
    content = _journal(tmp_path)
    assert "mia nota personale" in content
    assert "riga scritta a mano" in content
    assert content.startswith("# Luglio 2026")
    assert "| 2026-07-10 | A |" in content


def test_md_journal_one_file_per_period(tmp_path):
    w = _journal_writer(tmp_path)
    w.write([_ce("Books", "A", m=7), _ce("Books", "B", m=8, d=2)])
    names = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert names == ["2026-07.md", "2026-08.md"]


def test_md_rich_builds_index(tmp_path):
    w = _rich_writer(tmp_path)
    w.write([_ce("Books", "A"), _ce("Books", "B"), _ce("Videos", "C")])
    index = (tmp_path / "out" / "2026-07" / "README.md").read_text(encoding="utf-8")
    assert "# 2026-07" in index
    assert "3 entries across 2 categories" in index
    assert "| [Books](books.md) | 2 |" in index
    assert "| [Videos](videos.md) | 1 |" in index
