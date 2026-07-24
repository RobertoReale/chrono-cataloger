"""Local GUI (Streamlit).

Launch with:  streamlit run src/gui.py

It does not duplicate the pipeline logic: it imports and reuses the modules of
``main.py``, saves the configuration to ``config.yaml`` and shows progress by
reading the same state/logs as the CLI. GUI and CLI stay interchangeable.

Layout: the sidebar holds what you set once (provider, saved state, config
file); the tabs hold the actual work — *Run* is everything needed for a normal
run, the other tabs are the tuning knobs, the output and the run history.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import streamlit as st
import yaml

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src import cleaner, extractor, presets, windowing  # noqa: E402
from src.config import _validate as validate_cfg  # noqa: E402
from src.config import load_config  # noqa: E402

CONFIG_PATH = _ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = _ROOT / "config.example.yaml"
LOGS_DIR = _ROOT / "logs"
STATE_DIR = _ROOT / "state"
CHECKPOINT_PATH = STATE_DIR / "checkpoint.json"
PROCESSED_IDS_PATH = STATE_DIR / "processed_ids.json"

PROVIDERS = ["anthropic", "claude_code", "openai", "ollama"]
GROUP_BY_KINDS = ["month", "week", "days:N", "all"]
GROUP_BY_HELP = {
    "month": "One folder per calendar month (2026-07/).",
    "week": "One folder per ISO week (2026-W30/).",
    "days:N": "One folder per block of N days.",
    "all": "A single folder for the whole period.",
}
# The pipeline stages, in the order they are reported: used to draw the bar.
STAGES = ["extraction", "cleaning", "triage", "classification", "writing"]


# --------------------------------------------------------------------------- #
# Config loading / saving
# --------------------------------------------------------------------------- #
#: Non-fatal remarks the loader made about the file on disk (unknown keys).
_CFG_WARNINGS: list[str] = []


def _load_cfg() -> dict:
    """Load config.yaml, falling back to the example file on first launch."""
    _CFG_WARNINGS.clear()
    path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_CONFIG_PATH
    return load_config(path, on_warning=_CFG_WARNINGS.append)


def _save_cfg(cfg: dict) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def _as_yaml(cfg: dict) -> str:
    return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)


# --------------------------------------------------------------------------- #
# Session state: single source of truth for every editable value
# --------------------------------------------------------------------------- #
def _defaults_from(cfg: dict) -> dict:
    """The value every editable field takes when the session has none yet."""
    gb = cfg["output"]["group_by"]
    return {
        "period_mode": "Last N days",
        "last_days": 30,
        "from_date": date.today() - timedelta(days=30),
        "to_date": date.today(),
        "history_path": cfg["source"].get("history_path") or "",
        "base_dir": cfg["output"]["base_dir"],
        "file_format": cfg["output"]["file_format"],
        "group_by_kind": gb if gb in ("month", "week", "all") else "days:N",
        "group_by_n": int(gb.split(":", 1)[1]) if gb.startswith("days:") else 10,
        "provider": cfg["llm"]["provider"] if cfg["llm"]["provider"] in PROVIDERS else "anthropic",
        "model": cfg["llm"]["model"],
        "triage_model": cfg["llm"]["triage_model"],
        "api_key_env": cfg["llm"]["api_key_env"],
        "base_url": cfg["llm"].get("base_url") or "",
        "max_retries": max(0, int(cfg["llm"].get("max_retries") or 3)),
        # The config may leave it unset (each provider then picks its own);
        # the widget needs a concrete number, so show the HTTP default.
        "timeout_seconds": max(10, int(cfg["llm"].get("timeout_seconds") or 120)),
        "triage_enabled": bool(cfg["triage"]["enabled"]),
        "triage_batch": max(1, int(cfg["triage"]["batch_size"])),
        "triage_prompt": cfg["triage"]["prompt"],
        "categories": [
            {"name": c.get("name", ""), "description": c.get("description", "")}
            for c in cfg["classification"]["categories"]
            if isinstance(c, dict)
        ],
        "class_prompt": cfg["classification"]["prompt"],
        "class_batch": max(1, int(cfg["classification"]["batch_size"])),
        "blacklist_presets": list(cfg["filtering"].get("blacklist_presets") or []),
        "domain_blacklist": "\n".join(cfg["filtering"].get("domain_blacklist") or []),
        "keyword_blacklist": "\n".join(cfg["filtering"].get("url_keyword_blacklist") or []),
        # The widgets below clamp to their own minimum: a value outside a
        # number_input's range makes Streamlit raise instead of drawing.
        "min_visits": max(1, int(cfg["filtering"].get("min_visit_count") or 1)),
        "strip_query": bool(cfg["filtering"].get("strip_query_params", True)),
        "window_size_days": max(1, int(cfg["processing"]["window_size_days"])),
        "max_windows": max(0, int(cfg["processing"].get("max_batches_per_run") or 0)),
    }


def _sync_state(cfg: dict) -> None:
    """Make every editable value present in session_state, and keep a shadow copy.

    Streamlit throws away the state of a widget a run did not draw — which
    happens both for our conditional widgets (the period inputs, the
    provider-specific fields) and for everything below an early ``st.rerun()``.
    Re-seeding from the shadow at the top of each run, before any widget is
    drawn, makes those values survive.
    """
    ss = st.session_state
    for key, default in _defaults_from(cfg).items():
        if key not in ss:
            ss[key] = ss.get(f"_keep_{key}", default)
        ss[f"_keep_{key}"] = ss[key]
    ss.setdefault("run_log", [])
    ss.setdefault("last_stats", None)


def _request_reseed(source: str) -> None:
    """Ask for a reload on the next run.

    Session values cannot be reassigned once their widget has been drawn, so the
    reload is deferred to the top of the next run instead of done in place.
    """
    st.session_state["_reseed_from"] = source
    st.rerun()


def _seed_config(file_cfg: dict) -> dict:
    """The config the session values come from: normally the file, once reloaded."""
    source = st.session_state.pop("_reseed_from", None)
    if not source:
        return file_cfg
    st.session_state.clear()  # drops the widget values *and* their shadows
    return file_cfg if source == "file" else load_config(EXAMPLE_CONFIG_PATH)


def _save(cfg: dict) -> bool:
    """Save the config, refusing to persist something the loader would reject."""
    try:
        validate_cfg(cfg)
    except ValueError as exc:
        st.error(f"Not saved — the configuration is invalid: {exc}")
        return False
    _save_cfg(cfg)
    return True


def _collect_cfg(base: dict) -> dict:
    """Build the config that the current GUI state describes."""
    ss = st.session_state
    cfg = copy.deepcopy(base)

    cfg["llm"]["provider"] = ss.provider
    cfg["llm"]["model"] = ss.model.strip()
    cfg["llm"]["triage_model"] = ss.triage_model.strip()
    cfg["llm"]["api_key_env"] = ss.api_key_env.strip()
    cfg["llm"]["base_url"] = ss.base_url.strip() or None
    cfg["llm"]["max_retries"] = int(ss.max_retries)
    cfg["llm"]["timeout_seconds"] = int(ss.timeout_seconds)

    cfg["source"]["history_path"] = ss.history_path.strip() or None

    cfg["processing"]["window_size_days"] = int(ss.window_size_days)
    cfg["processing"]["max_batches_per_run"] = int(ss.max_windows) or None

    cfg["filtering"]["blacklist_presets"] = list(ss.blacklist_presets)
    cfg["filtering"]["domain_blacklist"] = _lines(ss.domain_blacklist)
    cfg["filtering"]["url_keyword_blacklist"] = _lines(ss.keyword_blacklist)
    cfg["filtering"]["min_visit_count"] = int(ss.min_visits)
    cfg["filtering"]["strip_query_params"] = bool(ss.strip_query)

    cfg["triage"]["enabled"] = bool(ss.triage_enabled)
    cfg["triage"]["batch_size"] = int(ss.triage_batch)
    cfg["triage"]["prompt"] = ss.triage_prompt

    cfg["classification"]["batch_size"] = int(ss.class_batch)
    cfg["classification"]["prompt"] = ss.class_prompt
    cfg["classification"]["categories"] = _clean_categories(ss.categories)

    cfg["output"]["base_dir"] = ss.base_dir.strip()
    cfg["output"]["group_by"] = _group_by()
    cfg["output"]["file_format"] = ss.file_format
    return cfg


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _clean_categories(rows) -> list[dict]:
    out = []
    for row in rows or []:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        out.append({"name": name, "description": str(row.get("description") or "").strip()})
    return out


def _group_by() -> str:
    kind = st.session_state.group_by_kind
    return f"days:{int(st.session_state.group_by_n)}" if kind == "days:N" else kind


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _resolved_history_path(cfg: dict) -> Path | None:
    try:
        return extractor.resolve_history_path(
            cfg["source"].get("history_path"), cfg["source"]["browser"]
        )
    except ValueError:
        return None


def _api_key_ready(cfg: dict) -> tuple[bool, str]:
    """(ready, human-readable explanation) for the credentials of the provider."""
    provider = cfg["llm"]["provider"]
    if provider == "ollama":
        return True, "Local provider (Ollama): no API key needed."
    if provider == "claude_code":
        if shutil.which(cfg["llm"].get("claude_cli_path") or "claude"):
            return True, "Claude Code CLI found — it uses the subscription you logged into."
        return False, "Claude Code CLI not found in PATH. Install it and run `claude` once to log in."
    env = cfg["llm"]["api_key_env"]
    if os.environ.get(env):
        return True, f"Environment variable {env} is set."
    return False, f"Environment variable {env} is not set — the run would fail on the first call."


def _problems(cfg: dict) -> tuple[list[str], list[str]]:
    """Return (blocking errors, non-blocking warnings) for the current settings."""
    errors: list[str] = []
    warnings: list[str] = list(_CFG_WARNINGS)
    ss = st.session_state

    if ss.period_mode == "Date range" and _period()[1] < _period()[0]:
        errors.append("The **To** date precedes the **From** date.")

    hist = _resolved_history_path(cfg)
    if hist is None:
        errors.append("Cannot auto-detect the history path for this browser: set it explicitly.")
    elif not hist.exists():
        errors.append(f"Chrome History file not found: `{hist}`")

    if not cfg["classification"]["categories"]:
        errors.append("No category defined — add at least one in **Categories & prompts**.")
    if not cfg["classification"]["prompt"].strip():
        errors.append("The classification prompt is empty.")
    elif "{categories_list}" not in cfg["classification"]["prompt"]:
        warnings.append(
            "The classification prompt has no `{categories_list}` placeholder: "
            "the model will not be told which categories exist."
        )
    if cfg["triage"]["enabled"] and not cfg["triage"]["prompt"].strip():
        errors.append("Triage is enabled but its prompt is empty.")
    if not cfg["output"]["base_dir"].strip():
        errors.append("The output folder is empty.")
    if not cfg["llm"]["model"].strip():
        errors.append("The main model name is empty.")
    if cfg["triage"]["enabled"] and not cfg["llm"]["triage_model"].strip():
        errors.append("Triage is enabled but the triage model name is empty.")

    ok, message = _api_key_ready(cfg)
    if not ok:
        # A missing API key fails on the first call, so it blocks; a CLI that
        # `which` cannot see may still work, so that one only warns.
        (errors if cfg["llm"]["provider"] in ("anthropic", "openai") else warnings).append(message)
    return errors, warnings


def _period() -> tuple[date, date]:
    """The (from, to) dates of the date-range mode."""
    return st.session_state.from_date, st.session_state.to_date


def _plan(cfg: dict) -> dict | None:
    """What the next run would do: period, windows, how many are still pending."""
    try:
        if st.session_state.period_mode == "Last N days":
            start, end = windowing.resolve_period(None, None, int(st.session_state.last_days))
        else:
            from_date, to_date = _period()
            start, end = windowing.resolve_period(
                datetime.combine(from_date, datetime.min.time()),
                datetime.combine(to_date, datetime.min.time()),
                None,
            )
        windows = windowing.generate_windows(start, end, cfg["processing"]["window_size_days"])
    except ValueError:
        return None

    completed = windowing.load_checkpoint(CHECKPOINT_PATH)
    todo = windowing.pending_windows(windows, completed)
    limit = cfg["processing"]["max_batches_per_run"]
    if limit:
        todo = todo[:limit]
    return {"start": start, "end": end, "windows": windows, "todo": todo}


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
def _sidebar(file_cfg: dict, cfg: dict) -> None:
    with st.sidebar:
        st.subheader("LLM provider")
        st.selectbox(
            "Provider",
            PROVIDERS,
            key="provider",
            help=(
                "anthropic / openai: HTTP API, needs a key. "
                "claude_code: the local Claude Code CLI and its subscription. "
                "ollama: a model running on this machine."
            ),
        )
        st.text_input("Main model", key="model", help="Used for the fine classification.")
        st.text_input("Triage model", key="triage_model", help="Cheap model used to pre-filter.")

        if st.session_state.provider in ("anthropic", "openai"):
            st.text_input("API key environment variable", key="api_key_env")
        if st.session_state.provider in ("ollama", "openai"):
            st.text_input("Base URL (optional)", key="base_url", placeholder="http://localhost:11434")

        ok, message = _api_key_ready(cfg)
        (st.success if ok else st.warning)(message)

        st.divider()
        st.subheader("Configuration")
        dirty = _as_yaml(cfg) != _as_yaml(file_cfg)
        st.caption(f"`{CONFIG_PATH.name}` — {'unsaved changes' if dirty else 'saved'}")
        c1, c2 = st.columns(2)
        if c1.button("Save", width="stretch", disabled=not dirty):
            if _save(cfg):
                st.toast("Configuration saved.")
                st.rerun()
        if c2.button("Reload", width="stretch", help="Discard the edits made here."):
            _request_reseed("file")
        if st.button("Restore example defaults", width="stretch"):
            _request_reseed("example")
        with st.expander("Preview config.yaml"):
            st.code(_as_yaml(cfg), language="yaml")

        st.divider()
        st.subheader("Saved state")
        completed = windowing.load_checkpoint(CHECKPOINT_PATH)
        processed = _processed_count()
        st.caption(
            f"{len(completed)} window(s) marked done · {processed} entries already written"
        )
        st.caption(
            "Windows in the checkpoint are skipped; entries already written are "
            "never duplicated."
        )
        if st.button("Reset checkpoint", width="stretch", disabled=not completed):
            CHECKPOINT_PATH.unlink(missing_ok=True)
            st.toast("Checkpoint cleared — the next run reprocesses every window.")
            st.rerun()
        with st.expander("Danger zone"):
            st.caption(
                "Clearing the written-entries index makes a re-run append the same "
                "lines again to the output files."
            )
            confirm = st.checkbox("I understand, clear it", key="confirm_clear")
            if st.button("Clear written-entries index", disabled=not confirm or not processed):
                PROCESSED_IDS_PATH.unlink(missing_ok=True)
                st.toast("Index cleared.")
                st.rerun()


def _processed_count() -> int:
    if not PROCESSED_IDS_PATH.exists():
        return 0
    try:
        return len(json.loads(PROCESSED_IDS_PATH.read_text(encoding="utf-8")).get("processed", []))
    except (json.JSONDecodeError, OSError):
        return 0


# --------------------------------------------------------------------------- #
# Tab: Run
# --------------------------------------------------------------------------- #
def _tab_run(cfg: dict) -> None:
    st.subheader("What to read")
    left, right = st.columns([1, 2])
    left.radio("Period", ["Last N days", "Date range"], key="period_mode")
    if st.session_state.period_mode == "Last N days":
        right.number_input("Number of days back from today", min_value=1, step=1, key="last_days")
    else:
        d1, d2 = right.columns(2)
        d1.date_input("From", key="from_date")
        d2.date_input("To", key="to_date")

    st.text_input(
        "Chrome History file",
        key="history_path",
        placeholder=str(extractor.default_history_path(cfg["source"]["browser"])),
        help="Leave empty to auto-detect for this operating system. "
             "Chrome may stay open: the file is copied before being read.",
    )
    hist = _resolved_history_path(cfg)
    if hist and hist.exists():
        st.caption(f"Found: `{hist}`")
    elif hist:
        st.caption(f"Not found: `{hist}`")

    st.subheader("What to write")
    o1, o2, o3 = st.columns([2, 1, 1])
    o1.text_input("Output folder", key="base_dir")
    o2.selectbox("One folder per", GROUP_BY_KINDS, key="group_by_kind",
                 help="\n\n".join(f"**{k}** — {v}" for k, v in GROUP_BY_HELP.items()))
    if st.session_state.group_by_kind == "days:N":
        o3.number_input("N days", min_value=1, step=1, key="group_by_n")
    o3.selectbox(
        "File format", ["txt", "md", "md_rich", "md_journal"], key="file_format",
        help="**txt** — one plain line per entry.\n\n"
             "**md** — a bullet list.\n\n"
             "**md_rich** — organized markdown: a heading per category, a table "
             "(date / what I learned / source) and a `README.md` index in every "
             "period folder.\n\n"
             "**md_journal** — a single file per period (`2026-07.md`) with one "
             "`## Category` section and table each, like a handwritten diary.",
    )

    st.divider()
    plan = _plan(cfg)
    errors, warnings = _problems(cfg)

    if plan:
        m1, m2, m3 = st.columns(3)
        m1.metric("Period", f"{plan['start'].date()} → {plan['end'].date()}")
        m2.metric("Windows in period", len(plan["windows"]))
        m3.metric("To process now", len(plan["todo"]))
        if plan["windows"] and not plan["todo"]:
            st.info(
                "Every window of this period is already in the checkpoint, so this "
                "run would do nothing. Use **Reset checkpoint** in the sidebar to "
                "redo them, or pick another period."
            )

    for err in errors:
        st.error(err, icon="🚫")
    for warn in warnings:
        st.warning(warn, icon="⚠️")

    b1, b2, _ = st.columns([1, 1, 2])
    go = b1.button("Run", type="primary", width="stretch", disabled=bool(errors))
    dry = b2.button(
        "Dry run", width="stretch", disabled=bool(errors),
        help="Extract, clean and triage only — nothing is classified, written or checkpointed. "
             "Triage still calls the cheap model.",
    )
    st.caption("Running saves the current settings to config.yaml first.")

    if go or dry:
        _execute(cfg, dry_run=dry, expected_windows=len(plan["todo"]) if plan else 0)

    if st.session_state.last_stats:
        _show_summary(st.session_state.last_stats)


def _execute(cfg: dict, dry_run: bool, expected_windows: int) -> None:
    if not _save(cfg):
        return
    from src.main import run  # imported late: keeps the first paint fast

    by_range = st.session_state.period_mode == "Date range"
    args = SimpleNamespace(
        config=str(CONFIG_PATH),
        from_date=_period()[0].isoformat() if by_range else None,
        to_date=_period()[1].isoformat() if by_range else None,
        last_days=None if by_range else int(st.session_state.last_days),
        group_by=_group_by(),
        window_size_days=None,
        history_path=st.session_state.history_path.strip() or None,
        max_batches_per_run=None,
        reset_checkpoint=False,
        dry_run=dry_run,
    )

    total_steps = max(1, expected_windows * len(STAGES))
    bar = st.progress(0.0, text="Starting…")
    status = st.status("Processing…", expanded=True)
    seen: dict[str, int] = {}
    st.session_state.run_log = []

    def on_progress(stage, done, total, extra):
        window = extra.get("window", "")
        if window not in seen:
            seen[window] = len(seen)
        step = STAGES.index(stage) + 1 if stage in STAGES else 0
        fraction = min(1.0, (seen[window] * len(STAGES) + step) / total_steps)
        bar.progress(
            fraction,
            text=f"Window {seen[window] + 1}/{max(expected_windows, len(seen))} — {stage}",
        )
        produced = extra.get("produced")
        tail = f" → {produced} kept" if produced is not None else ""
        line = f"[{window}] {stage}: {done}/{total}{tail}"
        st.session_state.run_log.append(line)
        status.write(line)

    try:
        stats = run(args, on_progress=on_progress)
    except Exception as exc:  # noqa: BLE001 - every failure belongs on screen
        bar.empty()
        status.update(label="Run failed", state="error")
        st.error(f"{type(exc).__name__}: {exc}")
        st.session_state.last_stats = None
        return

    bar.progress(1.0, text="Done")
    status.update(label="Done", state="complete", expanded=False)
    stats["_dry_run"] = dry_run
    st.session_state.last_stats = stats
    if dry_run:
        st.info("Dry run: nothing was written and the checkpoint is untouched.")


def _show_summary(stats: dict) -> None:
    st.subheader("Last run")
    c = stats.get("costs", {})
    m = st.columns(5)
    m[0].metric("Read", stats["raw_entries"])
    m[1].metric("After cleaning", stats["entries_after_cleaning"])
    m[2].metric("After triage", stats["entries_after_triage"])
    m[3].metric("Classified", stats["entries_classified"])
    m[4].metric("Written", stats["entries_written"])

    cost = c.get("estimated_cost_usd")
    cost_text = f"~${cost}" if cost is not None else "not billed per token"
    st.caption(
        f"Estimated tokens: {c.get('estimated_input_tokens', 0)} in / "
        f"{c.get('estimated_output_tokens', 0)} out — {cost_text} "
        f"({c.get('cost_note', '')})"
    )
    for warning in stats.get("warnings", []):
        st.warning(warning, icon="⚠️")
    if stats["entries_written"] == 0 and not stats.get("_dry_run"):
        st.info(
            "Nothing new was written. Either every entry had already been written "
            "before, or triage/classification kept nothing: check the counters above."
        )
    with st.expander("Full log of this run"):
        st.code("\n".join(st.session_state.run_log) or "(no output)")
        st.json(stats)


# --------------------------------------------------------------------------- #
# Tab: Categories & prompts
# --------------------------------------------------------------------------- #
def _tab_categories() -> None:
    st.subheader("Categories")
    st.caption(
        "One output file per category, per period. The description tells the "
        "model what belongs in it — it is worth being specific."
    )
    # An empty list gives the editor no columns to draw: keep one blank row.
    rows = st.session_state.categories or [{"name": "", "description": ""}]
    edited = st.data_editor(
        rows,
        num_rows="dynamic",
        column_config={
            "name": st.column_config.TextColumn("Name", required=True, width="medium"),
            "description": st.column_config.TextColumn("What goes in it", width="large"),
        },
    )
    st.session_state.categories = list(edited)
    if not _clean_categories(st.session_state.categories):
        st.error("At least one category is required.", icon="🚫")

    st.subheader("Classification prompt")
    st.caption(
        "Runs on the entries that survived triage, with the main model. "
        "`{categories_list}` is replaced with the categories above; the JSON "
        "output format is enforced by the code, so you only write the criteria."
    )
    st.text_area("Prompt", key="class_prompt", height=260, label_visibility="collapsed")
    if "{categories_list}" not in st.session_state.class_prompt:
        st.warning("Missing the `{categories_list}` placeholder.", icon="⚠️")

    st.subheader("Triage")
    st.caption(
        "A cheap first pass that drops the obvious noise before the expensive "
        "stage. Disabling it sends everything to the main model — accurate, "
        "slower and much more expensive."
    )
    st.toggle("Enable triage", key="triage_enabled")
    st.text_area(
        "Triage prompt", key="triage_prompt", height=180,
        disabled=not st.session_state.triage_enabled,
    )


# --------------------------------------------------------------------------- #
# Tab: Filters & tuning
# --------------------------------------------------------------------------- #
def _domain_stats(cfg: dict, days: int) -> list[dict]:
    """Top domains of the real history, with how much each one would cost."""
    end = datetime.now(timezone.utc)
    raw = extractor.extract(
        cfg["source"]["history_path"], end - timedelta(days=days), end,
        cfg["source"]["browser"],
    )
    urls: Counter = Counter()
    visits: Counter = Counter()
    for e in raw:
        # chrome-extension:// & co are dropped by the cleaner anyway, and their
        # "host" is an extension id: never propose them.
        if urlsplit(e.url).scheme not in ("http", "https"):
            continue
        host = cleaner._domain_of(e.url)
        if not host:
            continue
        urls[host] += 1
        visits[host] += e.visit_count

    already = presets.effective_domain_blacklist(cfg["filtering"])
    return [
        {"Domain": host, "URLs": n, "Visits": visits[host]}
        for host, n in urls.most_common(60)
        if not cleaner._domain_blacklisted(host, already)
    ]


def _add_to_blacklist(picked: list[str]) -> None:
    """Append the picked domains to the blacklist textarea.

    This has to be a widget callback: Streamlit refuses to assign to the state of
    a widget that the current run already drew, and the textarea sits above the
    button. Callbacks run before the rerun, so the assignment is legal there.
    """
    ss = st.session_state
    merged = list(dict.fromkeys(_lines(ss.domain_blacklist) + picked))
    ss.domain_blacklist = "\n".join(merged)
    ss["_keep_domain_blacklist"] = ss.domain_blacklist
    ss.suggestions = None
    ss.suggestions_picked = []
    st.toast(f"Added {len(picked)} domain(s). Save to keep them in config.yaml.")


def _suggest_domains(cfg: dict) -> None:
    """Read the history and let the user tick the domains to exclude."""
    st.markdown("**Suggest domains from my history**")
    st.caption(
        "Reads the local history file and ranks the domains you visit most, "
        "excluding what is already filtered. Nothing is sent anywhere."
    )
    s1, s2 = st.columns([1, 2])
    days = s1.number_input(
        "Days to analyze", min_value=7, max_value=3650, value=180, step=30,
        key="suggest_days",
    )
    if s2.button("Analyze history", use_container_width=True):
        try:
            st.session_state.suggestions = _domain_stats(cfg, int(days))
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            st.session_state.suggestions = None
            st.error(f"Could not read the history: {exc}")

    rows = st.session_state.get("suggestions")
    if not rows:
        if rows == []:
            st.success("Nothing left to suggest: every top domain is already filtered.")
        return

    st.caption(
        f"{len(rows)} domains not yet filtered, most visited first. They are what "
        "the model would otherwise be paid to read."
    )
    st.dataframe(rows, hide_index=True, use_container_width=True)

    labels = {f"{r['Domain']}  —  {r['URLs']} URLs": r["Domain"] for r in rows}
    chosen = st.multiselect(
        "Domains to exclude", list(labels), key="suggestions_picked",
        help="Pick the ones that are noise for you, then add them to the list above.",
    )
    picked = [labels[c] for c in chosen]
    st.button(
        f"Add {len(picked)} domain(s) to the blacklist", disabled=not picked,
        on_click=_add_to_blacklist, args=(picked,),
    )


def _tab_filters(cfg: dict) -> None:
    st.subheader("Local filters")
    st.caption("Applied before any model call, so they cost nothing and cut the bill.")

    names = presets.preset_names()
    st.multiselect(
        "Ready-made blacklist groups", names, key="blacklist_presets",
        help="Generic groups shipped with the project, merged with your own list "
             "below. Suffix match, so a domain also covers its subdomains.",
    )
    enabled = presets.domains_for(list(st.session_state.blacklist_presets))
    if enabled:
        with st.expander(f"{len(enabled)} domains from the selected groups"):
            st.code("\n".join(enabled))

    f1, f2 = st.columns(2)
    f1.text_area(
        "Blacklisted domains (one per line)", key="domain_blacklist", height=180,
        help="Your own domains, added to the groups above. "
             "Matched against the host, subdomains included.",
    )
    f2.text_area(
        "Blacklisted URL keywords (one per line)", key="keyword_blacklist", height=180,
        help="An entry is dropped when its URL contains one of these.",
    )
    c1, c2 = st.columns(2)
    c1.number_input(
        "Minimum visits within the window", min_value=1, step=1, key="min_visits",
        help="Raise it to keep only the pages you came back to.",
    )
    c2.toggle(
        "Strip query parameters", key="strip_query",
        help="Removes ?utm_...&co before deduplicating. The params that identify "
             "the content itself (?v=, ?id=) are always kept, otherwise every "
             "YouTube video would collapse into a single entry.",
    )

    st.divider()
    _suggest_domains(cfg)

    st.divider()
    st.subheader("Processing")
    p1, p2 = st.columns(2)
    p1.number_input(
        "Internal window size (days)", min_value=1, step=1, key="window_size_days",
        help="How much history is processed in one go. Each completed window is "
             "checkpointed, so an interrupted run resumes from there.",
    )
    p2.number_input(
        "Max windows per run (0 = no limit)", min_value=0, step=1, key="max_windows",
        help="Useful to try the prompts on a small sample before a long run.",
    )
    b1, b2 = st.columns(2)
    b1.number_input("Triage batch size", min_value=1, step=10, key="triage_batch")
    b2.number_input("Classification batch size", min_value=1, step=5, key="class_batch")

    st.divider()
    st.subheader("Network")
    n1, n2 = st.columns(2)
    n1.number_input("Max retries", min_value=0, step=1, key="max_retries")
    n2.number_input(
        "Timeout (seconds)", min_value=10, step=10, key="timeout_seconds",
        help="The claude_code provider starts a process per call: give it 300s or more.",
    )


# --------------------------------------------------------------------------- #
# Tab: Output
# --------------------------------------------------------------------------- #
def _tab_output(cfg: dict) -> None:
    base = Path(cfg["output"]["base_dir"])
    if not base.is_absolute():
        base = _ROOT / base
    st.caption(f"Reading `{base}`")
    if not base.exists():
        st.info("Nothing produced yet: the output folder does not exist.")
        return

    # A period is a sub-folder (txt/md/md_rich) or, with md_journal, a single file.
    periods = sorted(
        (p for p in base.iterdir() if p.is_dir() or p.suffix == ".md"), reverse=True
    )
    if not periods:
        st.info("The output folder is empty.")
        return

    period = st.selectbox("Period", periods, format_func=lambda p: p.stem)
    files = sorted(f for f in period.iterdir() if f.is_file()) if period.is_dir() else [period]
    if not files:
        st.info("This period contains no file.")
        return

    rendered = st.toggle(
        "Render markdown", value=True,
        help="Show .md files formatted (tables included) instead of raw text.",
    )

    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError as exc:
            st.warning(f"{f.name}: {exc}")
            continue
        count = len([line for line in text.splitlines() if line.strip()])
        with st.expander(f"{f.name} — {count} line(s)", expanded=len(files) == 1):
            if rendered and f.suffix == ".md" and text:
                st.markdown(text)
            else:
                st.text(text or "(empty)")
            st.download_button(
                "Download", text, file_name=f.name, key=f"dl_{period.name}_{f.name}"
            )


# --------------------------------------------------------------------------- #
# Tab: Run history
# --------------------------------------------------------------------------- #
def _tab_history() -> None:
    logs = sorted(LOGS_DIR.glob("run_*.json"), reverse=True) if LOGS_DIR.exists() else []
    if not logs:
        st.info("No run recorded yet.")
        return

    rows = []
    payloads = []
    for log in logs[:30]:
        try:
            data = json.loads(log.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        costs = data.get("costs", {})
        rows.append({
            "run": log.name,
            "when": (data.get("timestamp") or "")[:19].replace("T", " "),
            "period": f"{data.get('period', {}).get('from', '?')} → {data.get('period', {}).get('to', '?')}",
            "read": data.get("raw_entries", 0),
            "written": data.get("entries_written", 0),
            "est. $": costs.get("estimated_cost_usd"),
        })
        payloads.append((log.name, data))

    st.dataframe(rows, hide_index=True)
    for name, data in payloads:
        with st.expander(f"Details — {name}"):
            st.json(data)


# --------------------------------------------------------------------------- #
def main() -> None:
    st.set_page_config(page_title="chrono-cataloger", page_icon="📚", layout="wide")
    st.title("📚 chrono-cataloger")
    st.caption("Turn your Chrome history into a diary organized by the categories you choose.")

    try:
        file_cfg = _load_cfg()
    except (FileNotFoundError, ValueError) as exc:
        st.error(f"Cannot load the configuration: {exc}")
        st.stop()

    _sync_state(_seed_config(file_cfg))
    cfg = _collect_cfg(file_cfg)
    _sidebar(file_cfg, cfg)
    # The sidebar can change the state (provider, reload): recompute before drawing.
    cfg = _collect_cfg(file_cfg)

    tabs = st.tabs(["▶ Run", "🏷 Categories & prompts", "⚙ Filters & tuning", "📄 Output", "🕘 History"])
    with tabs[0]:
        _tab_run(cfg)
    with tabs[1]:
        _tab_categories()
    with tabs[2]:
        _tab_filters(cfg)
    with tabs[3]:
        _tab_output(cfg)
    with tabs[4]:
        _tab_history()


if __name__ == "__main__":
    main()
