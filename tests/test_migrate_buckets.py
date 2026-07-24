"""Migration of the pre-epoch ``days:N`` output folders.

With ``days:10`` the old naming started at the period being processed, so July
opened a ``2026-07-01_2026-07-10`` folder; the epoch anchoring splits those same
days across ``2026-06-26_2026-07-05`` and ``2026-07-06_2026-07-15``.
"""
import pytest

from src import migrate_buckets as mb

OLD = "2026-07-01_2026-07-10"
NEW_A = "2026-06-26_2026-07-05"
NEW_B = "2026-07-06_2026-07-15"

_JOURNAL = """# 2026-07-01_2026-07-10

*Source: Chrome history*

<!-- toc -->
- [Books](#books)
<!-- /toc -->

## Books

| Date | What I learned | Source |
| --- | --- | --- |
| 2026-07-03 | early row | — |
| 2026-07-08 | late row | — |
"""

_RICH = """# Books

*Period: 2026-07-01_2026-07-10*

| Date | What I learned | Source |
| --- | --- | --- |
| 2026-07-03 | early row | — |
| 2026-07-08 | late row | — |
"""


def _plans(base, group_by="days:10"):
    return {p.source.name: p for p in mb.plan_migration(base, group_by)}


def _run(base, group_by="days:10", categories=None):
    plans = mb.plan_migration(base, group_by, categories)
    return mb.apply_migration(base, group_by, plans, categories)


# --- md_journal ---------------------------------------------------------- #
def test_journal_rows_are_split_by_their_own_date(tmp_path):
    (tmp_path / f"{OLD}.md").write_text(_JOURNAL, encoding="utf-8")

    assert _run(tmp_path) == 1

    assert not (tmp_path / f"{OLD}.md").exists()
    early = (tmp_path / f"{NEW_A}.md").read_text(encoding="utf-8")
    late = (tmp_path / f"{NEW_B}.md").read_text(encoding="utf-8")
    assert "early row" in early and "late row" not in early
    assert "late row" in late and "early row" not in late
    # The section structure survives, TOC included.
    assert "## Books" in early
    assert "- [Books](#books)" in early


def test_journal_merges_into_an_existing_target(tmp_path):
    (tmp_path / f"{OLD}.md").write_text(_JOURNAL, encoding="utf-8")
    (tmp_path / f"{NEW_A}.md").write_text(
        "# 2026-06-26_2026-07-05\n\n*Source: Chrome history*\n\n"
        "<!-- toc -->\n<!-- /toc -->\n\n## Books\n\n"
        "| Date | What I learned | Source |\n| --- | --- | --- |\n"
        "| 2026-06-30 | pre-existing | — |\n",
        encoding="utf-8",
    )

    _run(tmp_path)

    text = (tmp_path / f"{NEW_A}.md").read_text(encoding="utf-8")
    assert "pre-existing" in text and "early row" in text
    assert text.count("## Books") == 1


def test_migration_is_idempotent(tmp_path):
    """An interrupted run must be safe to repeat: no row written twice."""
    (tmp_path / f"{OLD}.md").write_text(_JOURNAL, encoding="utf-8")
    _run(tmp_path)
    # Re-create the old file as if the delete had not happened, and re-run.
    (tmp_path / f"{OLD}.md").write_text(_JOURNAL, encoding="utf-8")
    _run(tmp_path)

    assert (tmp_path / f"{NEW_A}.md").read_text(encoding="utf-8").count("early row") == 1


def test_hand_written_journal_text_is_left_alone(tmp_path):
    (tmp_path / f"{OLD}.md").write_text(
        _JOURNAL.replace("## Books\n", "## Books\n\nA note I wrote myself.\n"),
        encoding="utf-8",
    )

    plan = _plans(tmp_path)[f"{OLD}.md"]
    assert plan.status == "blocked"
    assert "hand-written" in plan.note
    assert _run(tmp_path) == 0
    assert (tmp_path / f"{OLD}.md").exists()


def test_hand_written_preamble_is_left_alone(tmp_path):
    (tmp_path / f"{OLD}.md").write_text(
        _JOURNAL.replace("*Source: Chrome history*", "*Source: Chrome history*\n\nMy own intro."),
        encoding="utf-8",
    )
    assert _plans(tmp_path)[f"{OLD}.md"].status == "blocked"


# --- md_rich ------------------------------------------------------------- #
def test_rich_files_are_split_and_the_index_regenerated(tmp_path):
    folder = tmp_path / OLD
    folder.mkdir()
    (folder / "books.md").write_text(_RICH, encoding="utf-8")
    (folder / "README.md").write_text("# stale index\n", encoding="utf-8")

    assert _run(tmp_path) == 1

    assert not folder.exists()
    early = (tmp_path / NEW_A / "books.md").read_text(encoding="utf-8")
    assert early.startswith("# Books")
    assert "early row" in early and "late row" not in early
    assert "late row" in (tmp_path / NEW_B / "books.md").read_text(encoding="utf-8")
    index = (tmp_path / NEW_A / "README.md").read_text(encoding="utf-8")
    assert "[Books](books.md) | 1" in index


def test_rich_hand_written_text_is_left_alone(tmp_path):
    folder = tmp_path / OLD
    folder.mkdir()
    (folder / "books.md").write_text(_RICH + "\nSomething I added.\n", encoding="utf-8")
    assert _plans(tmp_path)[OLD].status == "blocked"


# --- txt / md (no date per line) ----------------------------------------- #
def test_flat_folder_moves_whole_when_its_days_stay_together(tmp_path):
    folder = tmp_path / "2026-07-06_2026-07-15"  # already one new bucket
    folder.mkdir()
    (folder / "books.txt").write_text("a line\n", encoding="utf-8")
    plans = mb.plan_migration(tmp_path, "days:10")
    assert plans[0].status == "aligned"

    # Same days, named the old way (a 10-day folder that happens to fit one bucket).
    other = tmp_path / "2026-07-07_2026-07-14"
    other.mkdir()
    (other / "books.txt").write_text("another line\n", encoding="utf-8")
    _run(tmp_path)
    assert not other.exists()
    text = (folder / "books.txt").read_text(encoding="utf-8")
    assert "a line" in text and "another line" in text


def test_flat_folder_straddling_two_buckets_is_reported_not_guessed(tmp_path):
    folder = tmp_path / OLD
    folder.mkdir()
    (folder / "books.txt").write_text("a line\n", encoding="utf-8")

    plan = _plans(tmp_path)[OLD]
    assert plan.status == "blocked"
    assert NEW_A in plan.note and NEW_B in plan.note
    assert _run(tmp_path) == 0
    assert (folder / "books.txt").exists()


# --- guards -------------------------------------------------------------- #
def test_already_aligned_buckets_are_not_touched(tmp_path):
    (tmp_path / f"{NEW_A}.md").write_text(
        _JOURNAL.replace(OLD, NEW_A), encoding="utf-8"
    )
    plan = _plans(tmp_path)[f"{NEW_A}.md"]
    assert plan.status == "aligned"
    assert _run(tmp_path) == 0


def test_other_granularities_are_refused(tmp_path):
    for group_by in ("month", "week", "all", "days:0", "days:x"):
        with pytest.raises(ValueError, match="days:N"):
            mb.plan_migration(tmp_path, group_by)


def test_unrelated_folders_are_ignored(tmp_path):
    (tmp_path / "2026-07").mkdir()
    (tmp_path / "notes.md").write_text("hello", encoding="utf-8")
    assert mb.plan_migration(tmp_path, "days:10") == []


def test_undated_rows_follow_the_start_of_the_old_folder(tmp_path):
    (tmp_path / f"{OLD}.md").write_text(
        _JOURNAL.replace("| 2026-07-08 | late row | — |", "| — | dateless row | — |"),
        encoding="utf-8",
    )
    plan = _plans(tmp_path)[f"{OLD}.md"]
    assert plan.undated == 1
    _run(tmp_path)
    assert "dateless row" in (tmp_path / f"{NEW_A}.md").read_text(encoding="utf-8")


def test_cli_without_apply_changes_nothing(tmp_path, capsys):
    (tmp_path / f"{OLD}.md").write_text(_JOURNAL, encoding="utf-8")
    rc = mb.main(["--base-dir", str(tmp_path), "--group-by", "days:10",
                  "--config", str(tmp_path / "missing.yaml")])
    assert rc == 0
    out = capsys.readouterr().out
    assert NEW_A in out and "--apply" in out
    assert (tmp_path / f"{OLD}.md").exists()
    assert not (tmp_path / f"{NEW_A}.md").exists()


def test_cli_apply_moves_the_files(tmp_path):
    (tmp_path / f"{OLD}.md").write_text(_JOURNAL, encoding="utf-8")
    rc = mb.main(["--base-dir", str(tmp_path), "--group-by", "days:10", "--apply",
                  "--config", str(tmp_path / "missing.yaml")])
    assert rc == 0
    assert (tmp_path / f"{NEW_A}.md").exists()
    assert not (tmp_path / f"{OLD}.md").exists()
