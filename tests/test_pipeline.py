"""End-to-end pipeline tests with a synthetic DB and a fake LLM client."""
import json
import re
from types import SimpleNamespace

import pytest

from src import main as main_module
from tests.conftest import FakeLLMClient

_LINE_RE = re.compile(r"^(\d+)\.", re.MULTILINE)


def _indices(prompt: str) -> list[int]:
    return [int(m) for m in _LINE_RE.findall(prompt)]


def _responder(prompt, model, max_tokens):
    idxs = _indices(prompt)
    if '"category"' in prompt:
        # Classification: assign alternating categories.
        cats = ["Books", "Philosophy and History"]
        objs = [
            {"i": i, "category": cats[i % 2], "summary": f"summary {i}", "url": ""}
            for i in idxs
        ]
        return json.dumps(objs)
    # Triage: everything is relevant.
    return json.dumps([{"i": i, "v": "relevant"} for i in idxs])


@pytest.fixture
def patched_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(main_module, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(main_module, "CHECKPOINT_PATH", tmp_path / "state" / "checkpoint.json")
    monkeypatch.setattr(main_module, "PROCESSED_IDS_PATH", tmp_path / "state" / "processed_ids.json")
    monkeypatch.setattr(main_module, "get_client", lambda cfg: FakeLLMClient(_responder))
    return tmp_path


def _args(config_path, tmp_out, **over):
    base = dict(
        config=str(config_path),
        from_date="2026-07-01",
        to_date="2026-07-31",
        last_days=None,
        group_by="month",
        window_size_days=30,
        history_path=None,
        max_batches_per_run=None,
        reset_checkpoint=False,
        dry_run=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _make_config(tmp_path, history_db, out_dir):
    import yaml
    cfg = {
        "llm": {"provider": "anthropic", "model": "claude-sonnet-5",
                "triage_model": "claude-haiku-4-5", "api_key_env": "ANTHROPIC_API_KEY"},
        "source": {"browser": "chrome", "history_path": str(history_db)},
        "processing": {"window_size_days": 30, "max_batches_per_run": None},
        "filtering": {
            "min_visit_count": 1,
            "domain_blacklist": ["mail.google.com"],
            "url_keyword_blacklist": ["login"],
            "strip_query_params": True,
        },
        "triage": {"enabled": True, "batch_size": 200, "prompt": "triage criteria"},
        "classification": {
            "batch_size": 50,
            "categories": [
                {"name": "Books", "description": "books"},
                {"name": "Philosophy and History", "description": "ideas"},
            ],
            "prompt": "Categories:\n{categories_list}\nClassify.",
        },
        "output": {"base_dir": str(out_dir), "group_by": "month", "file_format": "txt"},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return p


def test_end_to_end_writes_files(patched_paths, chrome_history_db):
    tmp = patched_paths
    out_dir = tmp / "Archive"
    cfg_path = _make_config(tmp, chrome_history_db, out_dir)

    stats = main_module.run(_args(cfg_path, out_dir))

    assert stats["raw_entries"] == 4  # July: hegel, mail, youtube, login
    assert stats["entries_after_cleaning"] == 2  # mail and login removed
    assert stats["entries_after_triage"] == 2
    assert stats["entries_classified"] == 2
    assert stats["entries_written"] == 2
    # files created
    assert (out_dir / "2026-07").exists()
    written_files = list((out_dir / "2026-07").glob("*.txt"))
    assert written_files


def test_end_to_end_idempotent_second_run(patched_paths, chrome_history_db):
    tmp = patched_paths
    out_dir = tmp / "Archive"
    cfg_path = _make_config(tmp, chrome_history_db, out_dir)

    main_module.run(_args(cfg_path, out_dir))
    # Second run with the checkpoint cleared but processed_ids intact:
    stats2 = main_module.run(_args(cfg_path, out_dir, reset_checkpoint=True))
    # No new entry written (idempotency through processed_ids).
    assert stats2["entries_written"] == 0


def test_checkpoint_resume_skips_completed(patched_paths, chrome_history_db):
    tmp = patched_paths
    out_dir = tmp / "Archive"
    cfg_path = _make_config(tmp, chrome_history_db, out_dir)

    main_module.run(_args(cfg_path, out_dir))
    # checkpoint written: a second run without reset -> 0 windows to process
    stats2 = main_module.run(_args(cfg_path, out_dir))
    assert stats2["windows_to_process"] == 0


def test_dry_run_writes_nothing(patched_paths, chrome_history_db):
    tmp = patched_paths
    out_dir = tmp / "Archive"
    cfg_path = _make_config(tmp, chrome_history_db, out_dir)
    stats = main_module.run(_args(cfg_path, out_dir, dry_run=True))
    assert stats["entries_written"] == 0
    assert not out_dir.exists() or not list(out_dir.glob("**/*.txt"))


def test_dry_run_leaves_checkpoint_untouched(patched_paths, chrome_history_db):
    """A dry run must not make the following real run skip the window."""
    tmp = patched_paths
    out_dir = tmp / "Archive"
    cfg_path = _make_config(tmp, chrome_history_db, out_dir)

    dry = main_module.run(_args(cfg_path, out_dir, dry_run=True))
    real = main_module.run(_args(cfg_path, out_dir))
    # The real run still sees every window the dry run looked at, and writes.
    assert real["windows_to_process"] == dry["windows_to_process"]
    assert real["entries_written"] == 2


def test_prompts_contain_no_escaped_braces(patched_paths, chrome_history_db, monkeypatch):
    """The JSON examples must reach the model as {...}, not as {{...}}."""
    tmp = patched_paths
    out_dir = tmp / "Archive"
    cfg_path = _make_config(tmp, chrome_history_db, out_dir)

    seen: list[str] = []

    def recording_responder(prompt, model, max_tokens):
        seen.append(prompt)
        return _responder(prompt, model, max_tokens)

    monkeypatch.setattr(
        main_module, "get_client", lambda cfg: FakeLLMClient(recording_responder)
    )

    main_module.run(_args(cfg_path, out_dir))
    assert seen
    for prompt in seen:
        assert "{{" not in prompt and "}}" not in prompt


def _collect_progress(cfg_path, out_dir, **over):
    events = []
    main_module.run(
        _args(cfg_path, out_dir, **over),
        on_progress=lambda s, d, t, e: events.append((s, d, t, e)),
    )
    return events


def test_every_stage_reports_progress(patched_paths, chrome_history_db):
    tmp = patched_paths
    out_dir = tmp / "Archive"
    cfg_path = _make_config(tmp, chrome_history_db, out_dir)

    stages = {s for s, *_ in _collect_progress(cfg_path, out_dir)}
    assert {"extraction", "cleaning", "triage", "classification", "writing"} <= stages


def test_slow_stages_report_per_batch(patched_paths, chrome_history_db):
    """With batch_size 1 each entry must produce its own triage/classification tick."""
    import yaml
    tmp = patched_paths
    out_dir = tmp / "Archive"
    cfg_path = _make_config(tmp, chrome_history_db, out_dir)
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg["triage"]["batch_size"] = 1
    cfg["classification"]["batch_size"] = 1
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

    events = _collect_progress(cfg_path, out_dir)
    # The July window holds the two surviving entries; a later empty window
    # legitimately emits its own 0/0 tick, so scope the assertion to the first.
    window = next(e[3]["window"] for e in events if e[0] == "triage")
    ticks = [e for e in events if e[0] == "triage" and e[3]["window"] == window]
    assert len(ticks) == 2  # two cleaned entries, one batch each
    # Progress is monotonic and ends at the total.
    assert [e[1] for e in ticks] == [1, 2]
    assert all(e[2] == 2 for e in ticks)
    assert all(e[3]["produced"] is not None for e in ticks)


def test_triage_stage_reported_even_when_disabled(patched_paths, chrome_history_db):
    import yaml
    tmp = patched_paths
    out_dir = tmp / "Archive"
    cfg_path = _make_config(tmp, chrome_history_db, out_dir)
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg["triage"]["enabled"] = False
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

    stages = {s for s, *_ in _collect_progress(cfg_path, out_dir)}
    assert "triage" in stages


def test_log_written(patched_paths, chrome_history_db):
    tmp = patched_paths
    out_dir = tmp / "Archive"
    cfg_path = _make_config(tmp, chrome_history_db, out_dir)
    main_module.run(_args(cfg_path, out_dir))
    logs = list((tmp / "logs").glob("run_*.json"))
    assert logs
    data = json.loads(logs[0].read_text(encoding="utf-8"))
    assert "costs" in data


def test_invalid_group_by_override_fails_before_any_llm_call(
    patched_paths, chrome_history_db, monkeypatch
):
    """A CLI typo must not be discovered after a window has been paid for."""
    out_dir = patched_paths / "out"
    cfg_path = _make_config(patched_paths, chrome_history_db, out_dir)

    calls: list[str] = []
    monkeypatch.setattr(
        main_module, "get_client",
        lambda cfg: FakeLLMClient(lambda p, m, t: calls.append(m) or _responder(p, m, t)),
    )

    with pytest.raises(ValueError, match="--group-by"):
        main_module.run(_args(cfg_path, out_dir, group_by="days:x"))
    assert calls == []
    assert not out_dir.exists()


def test_missing_api_key_is_reported_not_raised(
    patched_paths, chrome_history_db, monkeypatch, capsys
):
    """An unset key used to reach the user as a traceback, not a message."""
    from src.llm_client import get_client as real_get_client

    out_dir = patched_paths / "out"
    cfg_path = _make_config(patched_paths, chrome_history_db, out_dir)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(main_module, "get_client", real_get_client)

    rc = main_module.main(
        ["--config", str(cfg_path), "--from", "2026-07-01", "--to", "2026-07-05"]
    )
    assert rc == 2
    assert "Missing Anthropic API key" in capsys.readouterr().err


def test_run_reports_warnings_in_the_stats(patched_paths, chrome_history_db):
    out_dir = patched_paths / "out"
    cfg_path = _make_config(patched_paths, chrome_history_db, out_dir)
    stats = main_module.run(_args(cfg_path, out_dir))
    assert stats["warnings"] == []


def test_conflicting_period_flags_are_reported(patched_paths, chrome_history_db):
    out_dir = patched_paths / "out"
    cfg_path = _make_config(patched_paths, chrome_history_db, out_dir)
    stats = main_module.run(_args(cfg_path, out_dir, last_days=5))
    assert any("--last-days" in w for w in stats["warnings"])
