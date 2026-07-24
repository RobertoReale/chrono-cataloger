# chrono-cataloger

Turns your **Chrome browsing history** into an **automatic personal diary**: a
record organized by category (philosophy, books, films, concepts, historical
facts…) of what you have studied, discovered and explored over time. More
generally, it is a **tool for analyzing your own history** according to
categories and prompts you choose freely.

It runs entirely locally, except for the single API call to the LLM you pick
(Anthropic by default, replaceable with OpenAI or a local model via Ollama).

```
Study_Archive/
└── 2026-07/
    ├── philosophy-and-history.txt
    ├── new-concepts-and-words.txt
    ├── books.txt
    ├── interesting-historical-or-current-facts.txt
    └── films.txt
```

Example of `philosophy-and-history.txt`:

```
Hegel's core ideas - the Master-Slave dialectic
Karl Marx's German essay for his high-school leaving exam (12 August 1835)
Spinoza's core ideas
Carl Schmitt (The Führer upholds the law) and the Night of the Long Knives
```

## How it works (pipeline)

```
Chrome History (SQLite)
   ├─ 1. extractor  → reads the date range, converts the WebKit timestamps
   ├─ 2. cleaner    → normalizes URLs, strips tracking, blacklists, dedups  (local, free)
   ├─ 3. triage     → cheap model (Haiku), large batches: relevant/noise
   ├─ 4. classifier → main model (Sonnet), small batches: {category, summary, url}
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
git clone https://github.com/RobertoReale/chrono-cataloger.git
cd chrono-cataloger
python -m venv .venv
# Windows:  .venv\Scripts\activate      |  macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml      # then adapt config.yaml
export ANTHROPIC_API_KEY=sk-ant-...     # your key (Windows: setx / $env:)
```

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

## Configuration (`config.yaml`)

The **categories** and the **prompts** are not hardcoded: they live in
`config.yaml`. See [`config.example.yaml`](config.example.yaml) for the
commented file. In short:

- `llm`: provider (`anthropic`/`openai`/`ollama`), main model, triage model.
- `source.history_path`: `null` for auto-detection based on the operating system.
- `processing.window_size_days`: size of the internal working windows.
- `filtering`: domain/keyword blacklists, minimum thresholds, query-param stripping.
- `triage.prompt`: the relevant/noise **criteria** (the output format is enforced by the code).
- `classification.categories` + `classification.prompt`: the categories and the summarization
  prompt, with the `{categories_list}` placeholder.
- `output`: base folder, `group_by`, file format (`txt`/`md`).

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
   (per-stage counts, estimated tokens and cost).
3. Adjust the prompts/categories and run again. Thanks to idempotency you can
   re-run over the same period without duplicating the entries already written.

## Idempotency and resuming

- `state/processed_ids.json`: hashes (normalized url + category) of the entries
  already written → re-running duplicates nothing.
- `state/checkpoint.json`: last completed window → a new run skips the windows
  already done.
- `logs/run_<date>.json`: entries per stage, estimated tokens and cost per run.

## Scheduled automation

```bash
# cron, every Sunday evening: catch up on the last week
0 20 * * 0 cd /path/chrono-cataloger && python -m src.main --last-days 7 --group-by month
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
chrono-cataloger/
├── config.example.yaml      # commented reference configuration
├── requirements.txt
├── src/
│   ├── extractor.py         # extraction from the Chrome SQLite DB, by date range
│   ├── cleaner.py           # normalization, blacklists, dedup
│   ├── triage.py            # cheap pre-filter (Haiku)
│   ├── llm_client.py        # abstract interface to the LLM providers
│   ├── classifier.py        # fine-grained classification + summaries (Sonnet)
│   ├── writer.py            # file writing, idempotency, output granularity
│   ├── windowing.py         # internal windows + checkpoints
│   ├── config.py            # config loading/validation
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
