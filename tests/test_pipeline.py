"""Test end-to-end della pipeline con DB sintetico e client LLM finto."""
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
    if '"categoria"' in prompt:
        # Classificazione: assegna categorie alternate.
        cats = ["Libri", "Filosofia e Storia"]
        objs = [
            {"i": i, "categoria": cats[i % 2], "sintesi": f"sintesi {i}", "url": ""}
            for i in idxs
        ]
        return json.dumps(objs)
    # Triage: tutto rilevante.
    return json.dumps([{"i": i, "v": "rilevante"} for i in idxs])


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
        "triage": {"enabled": True, "batch_size": 200, "prompt": "criteri triage"},
        "classification": {
            "batch_size": 50,
            "categories": [
                {"name": "Libri", "description": "libri"},
                {"name": "Filosofia e Storia", "description": "idee"},
            ],
            "prompt": "Categorie:\n{categories_list}\nClassifica.",
        },
        "output": {"base_dir": str(out_dir), "group_by": "month", "file_format": "txt"},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return p


def test_end_to_end_writes_files(patched_paths, chrome_history_db):
    tmp = patched_paths
    out_dir = tmp / "Archivio"
    cfg_path = _make_config(tmp, chrome_history_db, out_dir)

    stats = main_module.run(_args(cfg_path, out_dir))

    assert stats["voci_grezze"] == 4  # luglio: hegel, mail, youtube, login
    assert stats["voci_dopo_pulizia"] == 2  # tolte mail e login
    assert stats["voci_dopo_triage"] == 2
    assert stats["voci_classificate"] == 2
    assert stats["voci_scritte"] == 2
    # file creati
    assert (out_dir / "2026-07").exists()
    written_files = list((out_dir / "2026-07").glob("*.txt"))
    assert written_files


def test_end_to_end_idempotent_second_run(patched_paths, chrome_history_db):
    tmp = patched_paths
    out_dir = tmp / "Archivio"
    cfg_path = _make_config(tmp, chrome_history_db, out_dir)

    main_module.run(_args(cfg_path, out_dir))
    # Seconda esecuzione con checkpoint azzerato ma processed_ids intatto:
    stats2 = main_module.run(_args(cfg_path, out_dir, reset_checkpoint=True))
    # Nessuna nuova voce scritta (idempotenza via processed_ids).
    assert stats2["voci_scritte"] == 0


def test_checkpoint_resume_skips_completed(patched_paths, chrome_history_db):
    tmp = patched_paths
    out_dir = tmp / "Archivio"
    cfg_path = _make_config(tmp, chrome_history_db, out_dir)

    main_module.run(_args(cfg_path, out_dir))
    # checkpoint scritto: seconda run senza reset -> 0 finestre da processare
    stats2 = main_module.run(_args(cfg_path, out_dir))
    assert stats2["finestre_da_processare"] == 0


def test_dry_run_writes_nothing(patched_paths, chrome_history_db):
    tmp = patched_paths
    out_dir = tmp / "Archivio"
    cfg_path = _make_config(tmp, chrome_history_db, out_dir)
    stats = main_module.run(_args(cfg_path, out_dir, dry_run=True))
    assert stats["voci_scritte"] == 0
    assert not out_dir.exists() or not list(out_dir.glob("**/*.txt"))


def test_log_written(patched_paths, chrome_history_db):
    tmp = patched_paths
    out_dir = tmp / "Archivio"
    cfg_path = _make_config(tmp, chrome_history_db, out_dir)
    main_module.run(_args(cfg_path, out_dir))
    logs = list((tmp / "logs").glob("run_*.json"))
    assert logs
    data = json.loads(logs[0].read_text(encoding="utf-8"))
    assert "costi" in data
