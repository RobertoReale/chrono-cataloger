# Project: automatic LLM-powered cataloger for your Chrome history

## 1. Goal

Build a tool that turns Chrome browsing history into a form of **automatic journaling/diary**: an organized record of what the user has studied, discovered and explored over time (concepts, books, films, historical or current facts), or — more generally — a **tool for analyzing one's own history** according to categories and criteria chosen freely by the user.

Key functional requirements:
1. Extract the Chrome history for a period chosen by the user.
2. Filter and reduce the noise (repeated searches, pages opened by mistake, tracking, operational content such as email/social) before involving the LLM.
3. Classify the relevant entries according to **user-configurable categories and prompts** (not hardcoded in the code).
4. Write/update text files organized by period and by category, in "personal diary" style (one line per entry, a readable summary, not the raw page title).
5. Let the user choose the **extraction period** (date range, or last N days) and the **output granularity** (monthly, weekly, every N days, or a single block) independently of each other.
6. Work efficiently even on very long histories (e.g. a whole year, tens of thousands of raw entries), without wasting tokens or breaking down halfway.
7. Be idempotent: re-running the script over the same period must not duplicate entries in the output files, and an interrupted run must be able to resume where it stopped.
8. Be provider-agnostic with respect to the LLM (Anthropic by default, replaceable with OpenAI or a local model via Ollama).
9. Run entirely locally except for the single API call to the chosen model.

## 2. Output format

Folder structure:

One folder per period, one file per category; the file names are slugs of the
categories defined in `config.yaml`:

```
Study_Archive/
└── 2026-07/
    ├── <category-a>.txt
    ├── <category-b>.txt
    └── <category-c>.txt
```

Contents of one such file:

```
<one-line summary of something read about> (<url>)
<one-line summary of another page, no url when it adds nothing>
```

Formatting rules:
- One line per entry.
- The line is a **readable summary** written by the LLM (max ~20 words), not the raw Chrome page title.
- The URL goes in parentheses only when useful to find the source again (video, specific page).
- No markdown markup/bullet lists unless the user asks for it in the config
  (`file_format: md` for a bullet list, `md_rich` for a structured document).

With `file_format: md_rich` the layout changes: each category file starts with a
`# Category` heading and a table, and every period folder holds a regenerated
`README.md` index.

```
Study_Archive/2026-07/
├── README.md            # index: category -> file, with entry counts
└── books.md
```

```markdown
# Books

*Period: 2026-07* · *Source: Chrome history*

| Date | What I learned | Source |
| --- | --- | --- |
| 2026-07-10 | Spinoza equates God with Nature | [plato.stanford.edu](https://plato.stanford.edu/entries/spinoza/) |
```

Default categories (customizable in the config, not in the code):
- Philosophy and History
- Concepts / New ideas and words
- Books
- Interesting historical or current facts
- Films (watched)

## 3. Pipeline architecture

```
┌──────────────────────┐
│ Chrome History (SQLite)│
└──────────┬────────────┘
           ▼
┌──────────────────────────┐
│ 1. Extractor               │  reads the SQLite DB over the requested date range,
│    (extractor.py)          │  returns (timestamp, url, title, visit_count)
└──────────┬────────────────┘
           ▼
┌──────────────────────────┐
│ 2. Cleaner / Deduper       │  normalizes URLs, strips tracking params,
│    (cleaner.py)            │  domain blacklists, dedup, minimum thresholds
└──────────┬────────────────┘
           ▼
┌──────────────────────────┐
│ 3. Cheap triage            │  large batches (150-300 entries) → cheap
│    (triage.py)             │  model (Haiku) → yes/no "is it worth
│                           │  classifying this in detail?"
└──────────┬────────────────┘
           ▼
┌──────────────────────────┐
│ 4. Classifier               │  small batches (40-60 entries) of ONLY the
│    (classifier.py)          │  surviving entries → main model (Sonnet)
│                           │  → structured JSON {category, summary, url}
└──────────┬────────────────┘
           ▼
┌──────────────────────────┐
│ 5. Writer                  │  writes/updates the .txt files per category,
│    (writer.py)             │  grouped by the chosen granularity,
│                           │  idempotent via state/processed_ids.json
└──────────────────────────┘
```

Cheap triage already cuts the volume by 90-95% before the expensive stage, so the classification batches always stay small: no real Map-Reduce with merging of parallel results is needed, just sequential batch processing.

## 4. Extraction period vs. output granularity

Two independent parameters:

```bash
# Extraction by explicit date range, monthly output (default)
python src/main.py --from 2026-01-01 --to 2026-12-31 --group-by month

# Last N days, weekly output
python src/main.py --last-days 90 --group-by week

# Extraction by range, output in custom N-day blocks
python src/main.py --from 2026-06-01 --to 2026-07-31 --group-by days:10

# A single file for the whole extracted period (no time subdivision)
python src/main.py --from 2026-01-01 --to 2026-12-31 --group-by all
```

Rules:
- `--from` / `--to`: explicit range (required unless `--last-days` is used).
- `--last-days N`: alternative, "the last N days from today".
- `--group-by`: `month | week | days:N | all` — determines **only** how the output folders and files are named/split, regardless of the extracted range.
- `writer.py` routes each classified entry into the right folder by looking at its original `last_visit_time`, not at the date the script ran.

## 5. Efficient handling of very long histories

A year of history can easily contain 20,000-80,000 raw entries. To stay efficient:

| Stage | What it does | Typical reduction | Cost |
|---|---|---|---|
| Extraction | SQL query over the date range | unchanged | zero (local) |
| Cleaning/dedup | Normalizes URLs, strips tracking, domain blacklists, dedup by normalized URL | → thousands of unique entries | zero (local, pure Python) |
| Cheap triage | Lightweight model (Haiku), domain+title only, batches of 150-300 | → hundreds of "potentially valuable" entries | very low |
| Fine classification | Main model (Sonnet), batches of 40-60, with summaries | → a few hundred entries written to files | moderate |

**Windowed processing (mandatory for long periods):**
- Never process a whole year in one go: the script always works in **internal time windows** (e.g. month by month), even when the user asks for the whole year.
- Each window goes through the full pipeline (extraction → cleaning → triage → classification → writing), then the state is saved to `state/checkpoint.json` (last completed window) and `state/processed_ids.json` (hashes of the entries already written).
- If a run is interrupted (rate limit, crash, machine shutdown), a new run resumes from the first incomplete window, without reprocessing — or paying again for — the ones already done.
- One log per run (`logs/run_<date>.json`): number of raw entries, after cleaning, after triage, classified, estimated tokens, and an estimated cost when the provider bills per token.
- A `max_batches_per_run` config parameter to cap a test run.

## 6. Repository structure

```
.
├── config.yaml                  # user configuration (categories, prompts, filters, period)
├── config.example.yaml
├── requirements.txt
├── src/
│   ├── extractor.py             # extraction from the Chrome SQLite DB, by date range
│   ├── cleaner.py               # normalization, blacklists, dedup
│   ├── triage.py                 # cheap pre-filter (Haiku)
│   ├── llm_client.py             # abstract interface to the LLM providers
│   ├── classifier.py             # fine-grained classification + summaries (Sonnet)
│   ├── writer.py                 # file writing, idempotency, output granularity
│   ├── windowing.py               # splits the requested period into processable windows
│   └── main.py                    # orchestration CLI
│   └── gui.py                     # minimal local GUI (see §9)
├── state/
│   ├── processed_ids.json        # hashes of the entries already written (idempotency)
│   └── checkpoint.json           # last completed time window
├── logs/
│   └── run_2026-07-24.json       # entries processed, estimated tokens, estimated cost
├── Study_Archive/
│   └── 2026-07/
│       ├── philosophy-and-history.txt
│       ├── new-concepts-and-words.txt
│       ├── books.txt
│       ├── interesting-historical-or-current-facts.txt
│       └── films.txt
└── README.md
```

## 7. Reference `config.yaml`

```yaml
# --- LLM provider ---
llm:
  provider: anthropic              # anthropic | openai | ollama
  model: claude-sonnet-5            # fine-grained classification
  triage_model: claude-haiku-4-5    # cheap pre-filter
  api_key_env: ANTHROPIC_API_KEY

# --- History source ---
source:
  browser: chrome
  history_path: "~/Library/Application Support/Google/Chrome/Default/History"
  # Windows: "%LOCALAPPDATA%\\Google\\Chrome\\User Data\\Default\\History"
  # Linux:   "~/.config/google-chrome/Default/History"

# --- Period and windowed processing ---
processing:
  window_size_days: 30             # size of the internal working window
                                    # (independent of --group-by, see §4)
  max_batches_per_run: null        # e.g. 5 for a limited test, null = no limit

# --- Filtering / cleaning (free, local) ---
filtering:
  min_visit_count: 1               # visits *within the window*
  domain_blacklist:
    - mail.google.com
    - web.whatsapp.com
    - facebook.com
    - instagram.com
    - accounts.google.com
    - localhost
  url_keyword_blacklist:
    - login
    - checkout
    - dashboard
  strip_query_params: true

# --- Cheap triage ---
triage:
  enabled: true
  batch_size: 200
  prompt: |
    You are given a list of browser history entries (domain + page title).
    For each one, answer only "relevant" or "noise": relevant if the page points
    to personal learning or in-depth content (articles, books, concepts,
    educational videos, long-form news, films); noise if it is operational or
    repetitive content (email, social feeds, generic searches, shopping,
    banking, everyday work tools).

# --- Categories and final classification prompt ---
classification:
  batch_size: 50
  categories:
    - name: "Philosophy and History"
      description: "Philosophical ideas, schools of thought, historical events and episodes explored in depth"
    - name: "New concepts and words"
      description: "Technical terms, new scientific or cultural concepts encountered and explored"
    - name: "Books"
      description: "Books discovered, reviewed, or looked into further"
    - name: "Interesting historical or current facts"
      description: "Specific news or facts, historical or current, explored in depth"
    - name: "Films"
      description: "Films watched, or searched for with the intent of watching them"
  prompt: |
    Analyze the following browser history entries (url, title, visit count).
    For each potentially relevant entry, assign one of these categories:
    {categories_list}
    Write a short line, in personal diary style (max 20 words), about what was
    learned or discovered — do NOT just copy the raw page title.
    Include the URL in parentheses only when useful to find the source again
    (e.g. a video, a specific page).
    Ignore entries that do not clearly fit any category.
    Reply ONLY in JSON, in this format:
    [{{"category": "...", "summary": "...", "url": "..." }}]

# --- Output ---
output:
  base_dir: "./Study_Archive"
  group_by: month                  # month | week | days:N | all (overridable from the CLI)
  file_format: txt                 # txt | md | md_rich
```

Output files are named after the category slug: one file per category per period.

## 8. Module details

### 8.1 `extractor.py`
- Copies the `History` file (SQLite) to a temporary location, because Chrome locks it while running.
- Query joining `visits` and `urls`:
  ```sql
  SELECT u.url, u.title, COUNT(v.id), MAX(v.visit_time)
  FROM urls u JOIN visits v ON v.url = u.id
  WHERE v.visit_time BETWEEN ? AND ?
  GROUP BY u.id
  ```
  `urls` alone would not do: it stores one row per URL with the *all-time* last
  visit and visit count, so a windowed run would miss any page last visited
  outside the window, and would report whole-history counts.
- Chrome timestamps are in microseconds since 1601-01-01 (the WebKit epoch): they must be converted.
- Returns a list of `{url, title, visit_count, last_visit}` records, where
  `visit_count` and `last_visit` are scoped to the window.

### 8.2 `cleaner.py`
- Normalizes URLs: strips `utm_*`, `fbclid`, `session_id`, trailing slashes, etc.
- Applies `domain_blacklist` and `url_keyword_blacklist` from the config.
- Deduplicates by `normalized_url`, summing the `visit_count`s.
- Drops entries below `min_visit_count`.
- Output: a reduced list of unique entries.

### 8.3 `triage.py`
- Splits the cleaned entries into batches (`triage.batch_size`).
- Sends only `domain + title` to a cheap model, along with the triage prompt.
- Receives a relevant/noise flag for each entry.
- Keeps only the entries marked as relevant.

### 8.4 `llm_client.py`
A common interface, one adapter per provider:

```python
class LLMClient:
    def complete(self, prompt: str, model: str) -> str:
        raise NotImplementedError

class AnthropicClient(LLMClient):
    def complete(self, prompt, model):
        # call to api.anthropic.com/v1/messages
        ...

class OpenAIClient(LLMClient):
    ...

class OllamaClient(LLMClient):
    ...

def get_client(provider: str) -> LLMClient:
    ...
```

### 8.5 `classifier.py`
- Takes the relevant entries (after triage, or all of them if triage is disabled).
- Splits them into batches (`classification.batch_size`).
- Builds the prompt by substituting `{categories_list}` with the categories defined in the config.
- Sends each batch, asking for structured JSON output.
- Validates the JSON it receives (retry with a correction if malformed).
- Returns a list of `{category, summary, url}` objects.

### 8.6 `windowing.py`
- Takes `--from`/`--to` (or `--last-days`) and `processing.window_size_days`.
- Generates the list of internal windows to process in sequence.
- Consults `state/checkpoint.json` to skip the windows already completed.

### 8.7 `writer.py`
- Takes `--group-by` to decide which sub-folder/file each classified entry goes into, based on the entry's original `last_visit_time` (not on the processing window).
- For `days:N`, computes the bucket as the interval `[period_start + k*N, period_start + (k+1)*N)`.
- Formats the output according to `file_format` (`txt`, `md` or `md_rich`).
- For `md_rich`: writes the heading + table header once, when the file is created,
  appends one table row per entry (pipes and newlines escaped), and regenerates
  the `README.md` index of every touched period folder.
- Only appends entries whose hash (normalized url + category) is not already in `state/processed_ids.json`.

### 8.8 `main.py`
```bash
python src/main.py \
  --config config.yaml \
  --from 2026-01-01 --to 2026-12-31 \
  --group-by month \
  --window-size-days 30
```
Steps: load config → generate windows (windowing.py) → for each incomplete window: extract → clean → triage → classify → write → update checkpoint and processed_ids → log costs.

## 9. Minimal local GUI

A local interface (in the browser, not an app to install) to manage everything without editing `config.yaml` by hand or using the CLI. Recommended technology: **Streamlit** (a single Python file, no separate frontend to write, started with `streamlit run src/gui.py` and opening a local page).

**GUI sections:**

1. **Extraction period**
   - Date range picker (from/to), or a "last N days" toggle with a numeric field.
   - Path to the Chrome `History` file (pre-filled based on the operating system, editable).

2. **Output granularity**
   - Selection: Monthly / Weekly / Every N days (with an N field) / A single block.
   - Independent of the extraction period, as per §4.

3. **Destination and file names**
   - Field for the output folder (`output.base_dir`), with a file-system picker.
   - Period folder name pattern (e.g. `YYYY-MM`, `YYYY-[W]WW`, customizable).
   - File format: `.txt` or `.md`.
   - File name pattern per category (automatic slug from the category name, or a manual category name → file name mapping).

4. **Categories**
   - Editable table: category name + description, with add/remove/reorder buttons.
   - Textarea for the classification prompt, with the `{categories_list}` placeholder.

5. **Filters**
   - Editable list of blacklisted domains and blacklisted URL keywords.
   - Numeric fields for the minimum thresholds (visits, duration).

6. **LLM provider**
   - Provider selection (Anthropic/OpenAI/Ollama), main model, triage model.
   - An indication of whether the environment variable holding the API key is set (without showing its value).
   - A toggle to enable/disable triage.

7. **Run**
   - A "Start processing" button that saves the current configuration to `config.yaml` and launches the pipeline (`main.py`) in the background.
   - Real-time progress view: the time window being processed, the number of entries per stage (raw → cleaned → triaged → classified), an updated cost/token estimate.
   - A "Stop" button that still saves the checkpoint reached so far (the idempotency of §5 makes this risk-free).

8. **Run history**
   - A list of past runs read from `logs/`, with the period processed, entries written and estimated cost.

**Implementation note:** the GUI does not duplicate the pipeline logic — it imports the functions of `windowing.py`/`main.py` directly and shows progress by reading the same `state/checkpoint.json` and the same logs used by the CLI. This keeps the GUI and the CLI always consistent and interchangeable (you can start a run from the CLI and check its state from the GUI, or vice versa).

## 10. Scheduled automation

```
# cron, every Sunday evening: catch up with the last week
0 20 * * 0 cd /path/to/repo && python -m src.main --config config.yaml --last-days 7 --group-by month
```
On Windows: Task Scheduler with the same command. Thanks to idempotency + checkpoints, it is safe to periodically run a "catch up on everything missing since the start of the year" script, with no duplicates and no starting from scratch.

## 11. Technical stack

- Python 3.11+
- `sqlite3` (standard library) for the extraction
- `pyyaml` for the configuration
- `httpx` for the API calls
- `pydantic` to validate the JSON schema returned by the LLM
- `streamlit` for the local GUI (see §9)
- No external database: state saved in local JSON

## 12. Future extensions (optional, not in the first release)

- Multi-browser support (Firefox, Edge).
- A small local UI (Streamlit) to review/correct the classifications before the final write.
- Extracting the first paragraph of the page (beyond the title alone) for ambiguous entries.
- Aggregate statistics (how many entries per category, trends over time).
- A "multi-profile" mode: several `config.yaml` files for different uses (study, productivity, etc.).

## 13. Recommended implementation order

1. `extractor.py` + `cleaner.py`, verified for real on a week of history.
2. `config.yaml` with the default categories.
3. `llm_client.py` (Anthropic only) + `classifier.py` without triage, to validate the output format end-to-end over a week.
4. `writer.py` with idempotency.
5. Add `triage.py` and measure the real volume reduction.
6. `windowing.py` with checkpoints, tested on a month.
7. Extension to long periods (`--group-by`, multiple windows), tested on a whole year.
8. `gui.py`, wiring the controls to the configuration and to the pipeline already working from the CLI.
9. Automation with cron/Task Scheduler.
