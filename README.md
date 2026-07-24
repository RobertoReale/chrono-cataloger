# chrono-cataloger

Turns your **Chrome browsing history** into an **automatic personal diary**: a
record, organized by the categories *you* define, of what you have studied,
discovered and explored over time. More generally, it is a **tool for analyzing
your own history** according to categories and prompts you choose freely.

It runs entirely locally, except for the single API call to the LLM you pick
(Anthropic by default, replaceable with OpenAI or a local model via Ollama).

One folder per period, one file per category — the names come from the
categories in your `config.yaml`:

```
Study_Archive/
└── 2026-07/
    ├── <category-a>.txt
    ├── <category-b>.txt
    └── <category-c>.txt
```

Each file is a list of one-line entries, each summarizing what a visited page
was about, with the source URL when it is worth keeping:

```
<one-line summary of something you read about> (<url>)
<one-line summary of another page, no url when it adds nothing>
```

## How it works (pipeline)

```
Chrome History (SQLite)
   ├─ 1. extractor  → reads the date range, converts the WebKit timestamps
   ├─ 2. cleaner    → normalizes URLs, strips tracking, blacklists, dedups  (local, free)
   ├─ 3. triage     → cheap model, large batches: relevant/noise
   ├─ 4. classifier → main model, small batches: {category, summary, url}
   └─ 5. writer     → writes/updates the .txt files per category, idempotently
```

Triage cuts the volume by 90-95% **before** the expensive stage, so the
classification batches always stay small. On long histories (a year, tens of
thousands of entries) processing always proceeds in **internal time windows**
with **checkpoints**: an interrupted run resumes where it stopped, without
reprocessing — or paying again for — the windows already done.

## Installation

Requires **Python 3.11+**.

```bash
git clone https://github.com/RobertoReale/chrono-catalogatore.git
cd chrono-catalogatore
python -m venv .venv
# Windows:  .venv\Scripts\activate      |  macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml      # then adapt config.yaml
export ANTHROPIC_API_KEY=sk-ant-...     # your key (Windows: setx / $env:)
```

### Using a Claude subscription instead of an API key

If you already have Claude Code installed and logged in with a Pro/Max plan,
set `llm.provider: claude_code` in `config.yaml` and skip the API key entirely.
The classifier then shells out to `claude --print --output-format json`, which
reuses the OAuth credentials created by `claude /login`.

```yaml
llm:
  provider: claude_code
  model: claude-sonnet-5
  triage_model: claude-haiku-4-5
  timeout_seconds: 300
```

Prerequisites: the `claude` binary on `PATH` (or `llm.claude_cli_path` pointing
at it) and one interactive `claude` run to complete the login.

Caveats compared with the `anthropic` HTTP provider:

- Usage counts against the subscription's rolling limits instead of being
  billed per token; a long run can hit those limits and stall.
- The run log reports estimated tokens but no dollar figure: nothing is billed
  per token, so a cost estimate would be made up.
- `max_tokens` is not enforceable through the CLI, and each call pays ~1–2 s of
  process startup, so large batches are noticeably slower.
- If `ANTHROPIC_API_KEY` is set it would take precedence inside the CLI, so the
  adapter removes it from the child process environment.

## Command-line usage

```bash
# Explicit date range, monthly output (default)
python -m src.main --from 2026-01-01 --to 2026-12-31 --group-by month

# Last 90 days, weekly output
python -m src.main --last-days 90 --group-by week

# Custom 10-day blocks
python -m src.main --from 2026-06-01 --to 2026-07-31 --group-by days:10

# A single file for the whole period
python -m src.main --from 2026-01-01 --to 2026-12-31 --group-by all

# Limited test: only the first 2 windows, without classifying/writing
python -m src.main --last-days 30 --max-batches-per-run 2 --dry-run
```

The **extraction period** (`--from/--to` or `--last-days`) and the **output
granularity** (`--group-by`) are independent: the writer routes each entry into
the right folder by looking at the entry's original `last_visit_time`, not at
the run date.

| Flag | Description |
|---|---|
| `--from` / `--to` | explicit `YYYY-MM-DD` range |
| `--last-days N` | alternative: the last N days from today |
| `--group-by` | `month` \| `week` \| `days:N` \| `all` |
| `--window-size-days` | internal window size (overrides config) |
| `--history-path` | path to the Chrome `History` file (overrides config) |
| `--max-batches-per-run` | limit the number of windows per run (testing) |
| `--reset-checkpoint` | clear the checkpoint and start over |
| `--dry-run` | extract + clean + triage, without classifying or writing |

## Local GUI (Streamlit)

To manage everything without editing `config.yaml` by hand:

```bash
streamlit run src/gui.py
```

The GUI **does not duplicate** the logic: it imports the same modules as the
CLI, saves the configuration to `config.yaml` and shows progress by reading the
same state. CLI and GUI therefore stay interchangeable.

Layout:

- **sidebar** — provider and models, credential check, save/reload/restore of
  `config.yaml`, and the saved state (completed windows, entries already
  written) with the buttons to clear it;
- **▶ Run** — period, history file, output folder and granularity. Before
  starting it shows what the run would actually do (period, windows, how many
  are still pending) and blocks the button with an explicit message when
  something is missing; **Dry run** stops before classification and writing;
- **🏷 Categories & prompts** — the categories in a table, plus the
  classification and triage prompts (this is where the tuning happens);
- **⚙ Filters & tuning** — blacklists, thresholds, window size, batch sizes,
  timeouts. **Analyze history** ranks the domains you actually visit, hides the
  ones already filtered, and lets you tick the noise: you see `immobiliare.it —
  3297 URLs` before deciding, instead of guessing;
- **📄 Output** — the files produced, per period, readable and downloadable;
- **🕘 History** — the past runs with their counters and estimated cost.

## Configuration (`config.yaml`)

The **categories** and the **prompts** are not hardcoded: they live in
`config.yaml`. See [`config.example.yaml`](config.example.yaml) for the
commented file. In short:

- `llm`: provider (`anthropic`/`claude_code`/`openai`/`ollama`), main model, triage model.
- `source.history_path`: `null` for auto-detection based on the operating system.
- `processing.window_size_days`: size of the internal working windows.
- `filtering`: domain/keyword blacklists, minimum thresholds, query-param stripping.
  - `blacklist_presets`: ready-made groups from
    [`presets/domain_blacklist.yaml`](presets/domain_blacklist.yaml) — `search`,
    `auth_mail`, `social`, `shopping`, `travel_home`, `banking`, `adult`,
    `tools_hosting`, `dev_local`, `aggregators` — merged with your own
    `domain_blacklist` without ever being written into it. `aggregators`
    (reddit, medium, HN) and `dev_local` are off by default: for many people
    those are real sources, not noise.
  - Filtering is a *silent* data loss, so the presets are also off by default in
    the code: an existing `config.yaml` keeps behaving exactly as before, only a
    fresh one from `config.example.yaml` starts with the recommended groups.
- `triage.prompt`: the relevant/noise **criteria** (the output format is enforced by the code).
- `classification.categories` + `classification.prompt`: the categories and the summarization
  prompt, with the `{categories_list}` placeholder.
- `output`: base folder, `group_by`, file format
  (`txt`/`md`/`md_rich`/`md_journal`). The two markdown-heavy formats:
  - `md_rich` — one file per category, opening with a `# Category` heading and a
    `Date | What I learned | Source` table; each period folder gets a `README.md`
    index listing the categories with their entry counts.
  - `md_journal` — a **single file per period** (`2026-07.md`), with one
    `## Category` section and table each, plus a table of contents. Closest to a
    handwritten monthly diary. Sections follow the order of
    `classification.categories`, and anything already in the file — including
    lines you wrote yourself — is preserved.

### Path to the Chrome History file

Auto-detected if `source.history_path: null`. Typical paths:

| OS | Path |
|---|---|
| Windows | `%LOCALAPPDATA%\Google\Chrome\User Data\Default\History` |
| macOS | `~/Library/Application Support/Google/Chrome/Default/History` |
| Linux | `~/.config/google-chrome/Default/History` |

> Chrome locks the file while it is open: the tool makes a **temporary copy** of
> it and reads that copy read-only, so you don't need to close the browser.

## Prompt tuning (the genuinely delicate part)

The challenge is not an engineering one but a **tuning** one: getting the triage
and classification prompts to produce genuinely useful results only comes from
looking at real output and iterating. To make that iteration safe, the project
keeps two responsibilities clearly apart:

- **You** edit the *criteria* (`triage.prompt`, `classification.prompt`,
  categories and descriptions). This is where the tuning happens.
- **The code** enforces the JSON *output format* and pairs it back to the
  original entries through a numeric index. So changing the criteria never
  breaks parsing.

Defenses already built in against imperfect LLM output:

- triage: a batch whose response cannot be parsed is **kept as a precaution** (a
  false positive is less harmful than discarding something relevant — it will be
  filtered out more accurately by classification anyway);
- classification: malformed JSON → **one retry** with a correction request;
  unrecognized categories → discarded (with a case-insensitive match against the
  canonical name); entries without an index → ignored.

Recommended tuning loop:

1. `--last-days 7 --max-batches-per-run 1` to work on a small sample.
2. Look at the `.txt` files produced and at the log in `logs/run_<date>.json`
   (per-stage counts and estimated tokens).
3. Adjust the prompts/categories and run again. Thanks to idempotency you can
   re-run over the same period without duplicating the entries already written.

## Idempotency and resuming

- `state/processed_ids.json`: hashes (normalized url + category) of the entries
  already written → re-running duplicates nothing.
- `state/checkpoint.json`: last completed window → a new run skips the windows
  already done. `--dry-run` deliberately does *not* touch it, so a trial run
  never makes the real run skip a window.
- `logs/run_<date>.json`: entries per stage and estimated tokens per run, plus a
  cost estimate when the provider actually bills per token (`anthropic`,
  `openai`). For `claude_code` and `ollama` the cost is reported as `null` with
  a note, rather than a fabricated `$0`.

## Scheduled automation

```bash
# cron, every Sunday evening: catch up on the last week
0 20 * * 0 cd /path/to/repo && python -m src.main --last-days 7 --group-by month
```

On Windows: Task Scheduler with the same command. Thanks to idempotency +
checkpoints it is safe to periodically run a "catch up on everything missing"
script, with no duplicates and no starting from scratch.

## Development and tests

```bash
pip install pytest
pytest -q
```

The tests use a synthetic Chrome-style SQLite DB and a fake LLM client: they
need neither Chrome nor an API key, and they cover extraction, cleaning/dedup,
windowing/checkpoints, idempotent writing, triage/classification parsing
(including malformed output) and the end-to-end pipeline.

## Repository structure

```
.
├── config.example.yaml      # commented reference configuration
├── requirements.txt
├── presets/
│   └── domain_blacklist.yaml  # ready-made blacklist groups
├── src/
│   ├── extractor.py         # extraction from the Chrome SQLite DB, by date range
│   ├── cleaner.py           # normalization, blacklists, dedup
│   ├── triage.py            # cheap pre-filter
│   ├── llm_client.py        # abstract interface to the LLM providers
│   ├── classifier.py        # fine-grained classification + summaries
│   ├── writer.py            # file writing, idempotency, output granularity
│   ├── windowing.py         # internal windows + checkpoints
│   ├── config.py            # config loading/validation
│   ├── presets.py           # blacklist groups, merged with the user's list
│   ├── costs.py             # token/cost estimation for the log
│   ├── models.py            # data models + WebKit timestamp conversion
│   ├── main.py              # orchestration CLI
│   └── gui.py               # local GUI (Streamlit)
├── tests/
├── DESIGN.md                # full design document
└── README.md
```

## Privacy

All processing is local. The only data that leaves your machine is **domain +
title** (triage) and **url + title + visit count** (classification), sent to the
LLM provider you chose. With Ollama, nothing leaves the machine at all. The
`state/`, `logs/` and `Study_Archive/` folders contain personal data and are
excluded from version control through `.gitignore`.

## License

MIT — see [LICENSE](LICENSE).
