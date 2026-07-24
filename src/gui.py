"""GUI locale minimale (Streamlit).

Avvio:  streamlit run src/gui.py

Non duplica la logica della pipeline: importa e riusa i moduli di ``main.py``,
salva la configurazione in ``config.yaml`` e mostra il progresso leggendo lo
stesso stato/log della CLI. GUI e CLI restano quindi intercambiabili.
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
    st.set_page_config(page_title="chrono-catalogatore", page_icon="📚", layout="wide")
    st.title("📚 chrono-catalogatore")
    st.caption("Cataloga la cronologia Chrome in un diario per categoria via LLM.")

    cfg = _load_cfg()

    # ------------------------------------------------------------------ #
    # 1. Periodo di estrazione
    # ------------------------------------------------------------------ #
    st.header("1. Periodo di estrazione")
    mode = st.radio("Modalita'", ["Range di date", "Ultimi N giorni"], horizontal=True)
    col1, col2 = st.columns(2)
    from_date = to_date = last_days = None
    if mode == "Range di date":
        from_date = col1.date_input("Da")
        to_date = col2.date_input("A")
    else:
        last_days = col1.number_input("Ultimi N giorni", min_value=1, value=30, step=1)

    default_hist = cfg["source"].get("history_path") or str(
        extractor.default_history_path(cfg["source"]["browser"])
    )
    history_path = st.text_input("Percorso file History di Chrome", value=default_hist)

    # ------------------------------------------------------------------ #
    # 2. Granularita' di output
    # ------------------------------------------------------------------ #
    st.header("2. Granularita' di output")
    gcol1, gcol2 = st.columns(2)
    gb_choice = gcol1.selectbox(
        "Raggruppa per",
        ["month", "week", "days:N", "all"],
        index=["month", "week", "days:N", "all"].index(
            cfg["output"]["group_by"] if cfg["output"]["group_by"] in ("month", "week", "all")
            else "days:N"
        ),
    )
    days_n = gcol2.number_input("N (per 'days:N')", min_value=1, value=10, step=1)
    group_by = f"days:{int(days_n)}" if gb_choice == "days:N" else gb_choice

    # ------------------------------------------------------------------ #
    # 3. Destinazione
    # ------------------------------------------------------------------ #
    st.header("3. Destinazione e formato")
    dcol1, dcol2 = st.columns(2)
    base_dir = dcol1.text_input("Cartella di output", value=cfg["output"]["base_dir"])
    file_format = dcol2.selectbox(
        "Formato file", ["txt", "md"], index=0 if cfg["output"]["file_format"] == "txt" else 1
    )

    # ------------------------------------------------------------------ #
    # 4. Categorie
    # ------------------------------------------------------------------ #
    st.header("4. Categorie e prompt di classificazione")
    cats_text = "\n".join(
        f"{c['name']} | {c.get('description', '')}"
        for c in cfg["classification"]["categories"]
    )
    edited_cats = st.text_area(
        "Categorie (una per riga: Nome | Descrizione)", value=cats_text, height=160
    )
    class_prompt = st.text_area(
        "Prompt di classificazione (usa {categories_list})",
        value=cfg["classification"]["prompt"],
        height=200,
    )

    # ------------------------------------------------------------------ #
    # 5. Filtri
    # ------------------------------------------------------------------ #
    st.header("5. Filtri")
    fcol1, fcol2 = st.columns(2)
    domain_bl = fcol1.text_area(
        "Domini in blacklist (uno per riga)",
        value="\n".join(cfg["filtering"]["domain_blacklist"]),
        height=140,
    )
    keyword_bl = fcol2.text_area(
        "Keyword URL in blacklist (una per riga)",
        value="\n".join(cfg["filtering"]["url_keyword_blacklist"]),
        height=140,
    )
    min_visits = st.number_input(
        "Numero minimo di visite", min_value=1, value=int(cfg["filtering"]["min_visit_count"])
    )

    # ------------------------------------------------------------------ #
    # 6. Provider LLM
    # ------------------------------------------------------------------ #
    st.header("6. Provider LLM")
    pcol1, pcol2, pcol3 = st.columns(3)
    provider = pcol1.selectbox(
        "Provider", ["anthropic", "openai", "ollama"],
        index=["anthropic", "openai", "ollama"].index(cfg["llm"]["provider"]),
    )
    model = pcol2.text_input("Modello principale", value=cfg["llm"]["model"])
    triage_model = pcol3.text_input("Modello triage", value=cfg["llm"]["triage_model"])
    triage_enabled = st.toggle("Abilita triage economico", value=cfg["triage"]["enabled"])

    import os
    key_env = cfg["llm"]["api_key_env"]
    if provider == "ollama":
        st.info("Provider locale (Ollama): nessuna API key necessaria.")
    elif os.environ.get(key_env):
        st.success(f"Variabile d'ambiente {key_env}: impostata ✓")
    else:
        st.warning(f"Variabile d'ambiente {key_env}: NON impostata")

    # ------------------------------------------------------------------ #
    # 7. Esecuzione
    # ------------------------------------------------------------------ #
    st.header("7. Esecuzione")
    max_windows = st.number_input(
        "Max finestre per esecuzione (0 = nessun limite)", min_value=0, value=0
    )

    if st.button("💾 Salva configurazione + Avvia", type="primary"):
        # Aggiorna la config con i valori della GUI.
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
        st.success(f"Configurazione salvata in {CONFIG_PATH}")

        # Avvia la pipeline in-process mostrando il progresso.
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

        status = st.status("Elaborazione in corso...", expanded=True)

        def progress(stage, done, total, extra):
            status.write(f"[{extra.get('window', '')}] {stage}: {done}/{total}")

        try:
            stats = run(args, on_progress=progress)
            status.update(label="Completato", state="complete")
            st.subheader("Riepilogo")
            st.json(stats)
        except Exception as e:  # noqa: BLE001 - mostra l'errore all'utente
            status.update(label="Errore", state="error")
            st.error(f"Errore durante l'esecuzione: {e}")

    # ------------------------------------------------------------------ #
    # 8. Storico esecuzioni
    # ------------------------------------------------------------------ #
    st.header("8. Storico esecuzioni")
    if LOGS_DIR.exists():
        logs = sorted(LOGS_DIR.glob("run_*.json"), reverse=True)
        if not logs:
            st.caption("Nessuna esecuzione registrata.")
        for log in logs[:20]:
            import json
            try:
                data = json.loads(log.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            with st.expander(f"{log.name} — scritte {data.get('voci_scritte', 0)} voci"):
                st.json(data)
    else:
        st.caption("Nessuna esecuzione registrata.")


if __name__ == "__main__":
    main()
