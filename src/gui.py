"""Minimal local GUI (Streamlit).

Launch with:  streamlit run src/gui.py

It does not duplicate the pipeline logic: it imports and reuses the modules of
``main.py``, saves the configuration to ``config.yaml`` and shows progress by
reading the same state/logs as the CLI. GUI and CLI stay interchangeable.
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st
import yaml

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src import extractor  # noqa: E402
from src.config import load_config  # noqa: E402

CONFIG_PATH = _ROOT / "config.yaml"
LOGS_DIR = _ROOT / "logs"
STATE_DIR = _ROOT / "state"


def _load_cfg() -> dict:
    if CONFIG_PATH.exists():
        return load_config(CONFIG_PATH)
    return load_config(_ROOT / "config.example.yaml")


def _save_cfg(cfg: dict) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def main() -> None:
    st.set_page_config(page_title="chrono-cataloger", page_icon="📚", layout="wide")
    st.title("📚 chrono-cataloger")
    st.caption("Catalog your Chrome history into a diary by category, via an LLM.")

    cfg = _load_cfg()

    # ------------------------------------------------------------------ #
    # 1. Extraction period
    # ------------------------------------------------------------------ #
    st.header("1. Extraction period")
    mode = st.radio("Mode", ["Date range", "Last N days"], horizontal=True)
    col1, col2 = st.columns(2)
    from_date = to_date = last_days = None
    if mode == "Date range":
        from_date = col1.date_input("From")
        to_date = col2.date_input("To")
    else:
        last_days = col1.number_input("Last N days", min_value=1, value=30, step=1)

    default_hist = cfg["source"].get("history_path") or str(
        extractor.default_history_path(cfg["source"]["browser"])
    )
    history_path = st.text_input("Path to the Chrome History file", value=default_hist)

    # ------------------------------------------------------------------ #
    # 2. Output granularity
    # ------------------------------------------------------------------ #
    st.header("2. Output granularity")
    gcol1, gcol2 = st.columns(2)
    gb_choice = gcol1.selectbox(
        "Group by",
        ["month", "week", "days:N", "all"],
        index=["month", "week", "days:N", "all"].index(
            cfg["output"]["group_by"] if cfg["output"]["group_by"] in ("month", "week", "all")
            else "days:N"
        ),
    )
    days_n = gcol2.number_input("N (for 'days:N')", min_value=1, value=10, step=1)
    group_by = f"days:{int(days_n)}" if gb_choice == "days:N" else gb_choice

    # ------------------------------------------------------------------ #
    # 3. Destination
    # ------------------------------------------------------------------ #
    st.header("3. Destination and format")
    dcol1, dcol2 = st.columns(2)
    base_dir = dcol1.text_input("Output folder", value=cfg["output"]["base_dir"])
    file_format = dcol2.selectbox(
        "File format", ["txt", "md"], index=0 if cfg["output"]["file_format"] == "txt" else 1
    )

    # ------------------------------------------------------------------ #
    # 4. Categories
    # ------------------------------------------------------------------ #
    st.header("4. Categories and classification prompt")
    cats_text = "\n".join(
        f"{c['name']} | {c.get('description', '')}"
        for c in cfg["classification"]["categories"]
    )
    edited_cats = st.text_area(
        "Categories (one per line: Name | Description)", value=cats_text, height=160
    )
    class_prompt = st.text_area(
        "Classification prompt (use {categories_list})",
        value=cfg["classification"]["prompt"],
        height=200,
    )

    # ------------------------------------------------------------------ #
    # 5. Filters
    # ------------------------------------------------------------------ #
    st.header("5. Filters")
    fcol1, fcol2 = st.columns(2)
    domain_bl = fcol1.text_area(
        "Blacklisted domains (one per line)",
        value="\n".join(cfg["filtering"]["domain_blacklist"]),
        height=140,
    )
    keyword_bl = fcol2.text_area(
        "Blacklisted URL keywords (one per line)",
        value="\n".join(cfg["filtering"]["url_keyword_blacklist"]),
        height=140,
    )
    min_visits = st.number_input(
        "Minimum number of visits", min_value=1, value=int(cfg["filtering"]["min_visit_count"])
    )

    # ------------------------------------------------------------------ #
    # 6. LLM provider
    # ------------------------------------------------------------------ #
    st.header("6. LLM provider")
    pcol1, pcol2, pcol3 = st.columns(3)
    provider = pcol1.selectbox(
        "Provider", ["anthropic", "openai", "ollama"],
        index=["anthropic", "openai", "ollama"].index(cfg["llm"]["provider"]),
    )
    model = pcol2.text_input("Main model", value=cfg["llm"]["model"])
    triage_model = pcol3.text_input("Triage model", value=cfg["llm"]["triage_model"])
    triage_enabled = st.toggle("Enable cheap triage", value=cfg["triage"]["enabled"])

    import os
    key_env = cfg["llm"]["api_key_env"]
    if provider == "ollama":
        st.info("Local provider (Ollama): no API key needed.")
    elif os.environ.get(key_env):
        st.success(f"Environment variable {key_env}: set ✓")
    else:
        st.warning(f"Environment variable {key_env}: NOT set")

    # ------------------------------------------------------------------ #
    # 7. Run
    # ------------------------------------------------------------------ #
    st.header("7. Run")
    max_windows = st.number_input(
        "Max windows per run (0 = no limit)", min_value=0, value=0
    )

    if st.button("💾 Save configuration + Run", type="primary"):
        # Update the config with the values from the GUI.
        cfg["source"]["history_path"] = history_path
        cfg["output"]["base_dir"] = base_dir
        cfg["output"]["group_by"] = group_by
        cfg["output"]["file_format"] = file_format
        cfg["llm"]["provider"] = provider
        cfg["llm"]["model"] = model
        cfg["llm"]["triage_model"] = triage_model
        cfg["triage"]["enabled"] = bool(triage_enabled)
        cfg["filtering"]["domain_blacklist"] = [d.strip() for d in domain_bl.splitlines() if d.strip()]
        cfg["filtering"]["url_keyword_blacklist"] = [k.strip() for k in keyword_bl.splitlines() if k.strip()]
        cfg["filtering"]["min_visit_count"] = int(min_visits)
        cfg["classification"]["prompt"] = class_prompt
        cats = []
        for line in edited_cats.splitlines():
            if not line.strip():
                continue
            if "|" in line:
                name, desc = line.split("|", 1)
            else:
                name, desc = line, ""
            cats.append({"name": name.strip(), "description": desc.strip()})
        if cats:
            cfg["classification"]["categories"] = cats
        _save_cfg(cfg)
        st.success(f"Configuration saved to {CONFIG_PATH}")

        # Run the pipeline in-process, showing the progress.
        from types import SimpleNamespace
        from src.main import run

        args = SimpleNamespace(
            config=str(CONFIG_PATH),
            from_date=str(from_date) if from_date else None,
            to_date=str(to_date) if to_date else None,
            last_days=int(last_days) if last_days else None,
            group_by=group_by,
            window_size_days=None,
            history_path=history_path,
            max_batches_per_run=int(max_windows) if max_windows else None,
            reset_checkpoint=False,
            dry_run=False,
        )

        status = st.status("Processing...", expanded=True)

        def progress(stage, done, total, extra):
            status.write(f"[{extra.get('window', '')}] {stage}: {done}/{total}")

        try:
            stats = run(args, on_progress=progress)
            status.update(label="Done", state="complete")
            st.subheader("Summary")
            st.json(stats)
        except Exception as e:  # noqa: BLE001 - surface the error to the user
            status.update(label="Error", state="error")
            st.error(f"Error during the run: {e}")

    # ------------------------------------------------------------------ #
    # 8. Run history
    # ------------------------------------------------------------------ #
    st.header("8. Run history")
    if LOGS_DIR.exists():
        logs = sorted(LOGS_DIR.glob("run_*.json"), reverse=True)
        if not logs:
            st.caption("No run recorded yet.")
        for log in logs[:20]:
            import json
            try:
                data = json.loads(log.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            with st.expander(f"{log.name} — {data.get('entries_written', 0)} entries written"):
                st.json(data)
    else:
        st.caption("No run recorded yet.")


if __name__ == "__main__":
    main()
