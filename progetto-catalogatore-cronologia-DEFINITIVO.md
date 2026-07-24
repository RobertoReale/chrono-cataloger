# Progetto: Catalogatore automatico della cronologia Chrome via LLM

## 1. Obiettivo

Costruire uno strumento che trasformi la cronologia di navigazione Chrome in una forma di **journaling/diario automatico**: uno storico organizzato di cosa l'utente ha studiato, scoperto e approfondito nel tempo (concetti, libri, film, fatti storici o attuali), oppure — più in generale — uno **strumento di analisi della propria cronologia** secondo categorie e criteri scelti liberamente dall'utente.

Requisiti funzionali chiave:
1. Estrarre la cronologia di Chrome per un periodo scelto dall'utente.
2. Filtrare e ridurre il rumore (ricerche ripetute, pagine aperte per errore, tracking, contenuti operativi come mail/social) prima di coinvolgere l'LLM.
3. Classificare le voci rilevanti secondo **categorie e prompt configurabili dall'utente** (non hardcoded nel codice).
4. Scrivere/aggiornare file di testo organizzati per periodo e per categoria, in stile "diario personale" (una riga per voce, sintesi leggibile, non il titolo grezzo della pagina).
5. Permettere all'utente di scegliere **periodo di estrazione** (range di date, o ultimi N giorni) e **granularità di output** (mensile, settimanale, ogni N giorni, o un unico blocco), in modo indipendente l'uno dall'altro.
6. Funzionare in modo efficiente anche su cronologie molto lunghe (es. un anno intero, decine di migliaia di voci grezze), senza sprecare token né rompersi a metà strada.
7. Essere idempotente: rieseguire lo script sullo stesso periodo non deve duplicare voci nei file di output, e un'esecuzione interrotta deve poter riprendere da dove si era fermata.
8. Essere provider-agnostico rispetto all'LLM (Anthropic di default, sostituibile con OpenAI o un modello locale via Ollama).
9. Girare interamente in locale tranne la singola chiamata API al modello scelto.

## 2. Formato di output

Struttura cartelle:

```
Archivio_Studio/
└── 2026-07/
    ├── filosofia-e-storia.txt
    ├── concetti-e-parole-nuove.txt
    ├── libri.txt
    ├── fatti-storici-o-attuali.txt
    └── film.txt
```

Contenuto di un file, es. `filosofia-e-storia.txt`:

```
Idee fondamentali di Hegel - Dialettica Servo-Padrone
Il tema di tedesco di Karl Marx per l'esame di licenza liceale (12 agosto 1835)
Idee fondamentali di Spinoza
Carl Schmitt (Il Führer protegge il diritto) e La Notte dei lunghi coltelli
```

Regole di formato:
- Una riga per voce.
- La riga è una **sintesi leggibile** scritta dall'LLM (max ~20 parole), non il titolo grezzo della pagina Chrome.
- URL tra parentesi solo quando utile per ritrovare la fonte (video, pagina specifica).
- Nessun markup markdown/elenchi puntati a meno che l'utente non lo richieda in config (`file_format: md`).

Categorie di default (personalizzabili in config, non nel codice):
- Filosofia e Storia
- Concetti / Idee e parole nuove
- Libri
- Fatti storici o attuali interessanti
- Film (visti)

## 3. Architettura della pipeline

```
┌──────────────────────┐
│ Chrome History (SQLite)│
└──────────┬────────────┘
           ▼
┌──────────────────────────┐
│ 1. Extractor               │  legge SQLite nel range di date richiesto,
│    (extractor.py)          │  restituisce (timestamp, url, titolo, visit_count)
└──────────┬────────────────┘
           ▼
┌──────────────────────────┐
│ 2. Cleaner / Deduper       │  normalizza URL, rimuove tracking params,
│    (cleaner.py)            │  blacklist domini, dedup, soglie minime
└──────────┬────────────────┘
           ▼
┌──────────────────────────┐
│ 3. Triage economico        │  batch grandi (150-300 voci) → modello
│    (triage.py)             │  economico (Haiku) → sì/no "vale la pena
│                           │  classificarla nel dettaglio?"
└──────────┬────────────────┘
           ▼
┌──────────────────────────┐
│ 4. Classifier               │  batch piccoli (40-60 voci) delle SOLE voci
│    (classifier.py)          │  sopravvissute → modello principale (Sonnet)
│                           │  → JSON strutturato {categoria, sintesi, url}
└──────────┬────────────────┘
           ▼
┌──────────────────────────┐
│ 5. Writer                  │  scrive/aggiorna i file .txt per categoria,
│    (writer.py)             │  raggruppati secondo la granularità scelta,
│                           │  idempotente via state/processed_ids.json
└──────────────────────────┘
```

Il triage economico riduce già il volume del 90-95% prima dello stadio costoso, quindi i batch di classificazione restano sempre piccoli: non serve un vero Map-Reduce con fusione di risultati paralleli, solo elaborazione sequenziale a batch.

## 4. Periodo di estrazione vs. granularità di output

Due parametri indipendenti:

```bash
# Estrazione per range di date esplicito, output mensile (default)
python src/main.py --from 2026-01-01 --to 2026-12-31 --group-by month

# Ultimi N giorni, output settimanale
python src/main.py --last-days 90 --group-by week

# Estrazione per range, output a blocchi di N giorni personalizzati
python src/main.py --from 2026-06-01 --to 2026-07-31 --group-by days:10

# Un solo file per tutto il periodo estratto (nessuna suddivisione temporale)
python src/main.py --from 2026-01-01 --to 2026-12-31 --group-by all
```

Regole:
- `--from` / `--to`: range esplicito (obbligatorio se non si usa `--last-days`).
- `--last-days N`: alternativa, "ultimi N giorni da oggi".
- `--group-by`: `month | week | days:N | all` — determina **solo** come vengono nominate/suddivise le cartelle e i file di output, indipendentemente dal range estratto.
- Il `writer.py` instrada ogni voce classificata nella cartella giusta guardando il suo `last_visit_time` originale, non la data di esecuzione dello script.

## 5. Gestione efficiente di cronologie molto lunghe

Un anno di cronologia può contenere facilmente 20.000-80.000 voci grezze. Per restare efficienti:

| Stadio | Cosa fa | Riduzione tipica | Costo |
|---|---|---|---|
| Estrazione | Query SQL sul range di date | invariato | zero (locale) |
| Pulizia/dedup | Normalizza URL, rimuove tracking, blacklist domini, dedup per URL normalizzato | → migliaia di voci uniche | zero (locale, puro Python) |
| Triage economico | Modello leggero (Haiku), solo dominio+titolo, batch da 150-300 | → centinaia di voci "potenzialmente di valore" | bassissimo |
| Classificazione fine | Modello principale (Sonnet), batch da 40-60, con sintesi | → poche centinaia di voci scritte nei file | contenuto |

**Elaborazione a finestre (obbligatoria per periodi lunghi):**
- Non processare mai un intero anno in un colpo solo: lo script lavora sempre a **finestre temporali interne** (es. mese per mese), anche se l'utente chiede l'intero anno.
- Ogni finestra passa per l'intera pipeline (estrazione → pulizia → triage → classificazione → scrittura), poi lo stato viene salvato in `state/checkpoint.json` (ultima finestra completata) e `state/processed_ids.json` (hash delle voci già scritte).
- Se l'esecuzione si interrompe (rate limit, crash, chiusura del PC), un rilancio riparte dall'ultima finestra non completata, senza rielaborare né ripagare quelle già fatte.
- Log per ogni esecuzione (`logs/run_<data>.json`): numero di voci grezze, dopo pulizia, dopo triage, classificate, token stimati, costo stimato.
- Parametro `max_batches_per_run` in config per limitare un'esecuzione di test.

## 6. Struttura del repository

```
chrono-catalogatore/
├── config.yaml                  # configurazione utente (categorie, prompt, filtri, periodo)
├── config.example.yaml
├── requirements.txt
├── src/
│   ├── extractor.py             # estrazione da SQLite Chrome, per range di date
│   ├── cleaner.py               # normalizzazione, blacklist, dedup
│   ├── triage.py                 # pre-filtro economico (Haiku)
│   ├── llm_client.py             # interfaccia astratta ai provider LLM
│   ├── classifier.py             # classificazione fine + sintesi (Sonnet)
│   ├── writer.py                 # scrittura file, idempotenza, granularità output
│   ├── windowing.py               # divide il periodo richiesto in finestre processabili
│   └── main.py                    # CLI di orchestrazione
│   └── gui.py                     # GUI locale minimale (vedi §9)
├── state/
│   ├── processed_ids.json        # hash delle voci già scritte (idempotenza)
│   └── checkpoint.json           # ultima finestra temporale completata
├── logs/
│   └── run_2026-07-24.json       # voci processate, token stimati, costo stimato
├── Archivio_Studio/
│   └── 2026-07/
│       ├── filosofia-e-storia.txt
│       ├── concetti-e-parole-nuove.txt
│       ├── libri.txt
│       ├── fatti-storici-o-attuali.txt
│       └── film.txt
└── README.md
```

## 7. `config.yaml` di riferimento

```yaml
# --- Provider LLM ---
llm:
  provider: anthropic              # anthropic | openai | ollama
  model: claude-sonnet-5            # classificazione fine
  triage_model: claude-haiku-4-5    # pre-filtro economico
  api_key_env: ANTHROPIC_API_KEY

# --- Sorgente cronologia ---
source:
  browser: chrome
  history_path: "~/Library/Application Support/Google/Chrome/Default/History"
  # Windows: "%LOCALAPPDATA%\\Google\\Chrome\\User Data\\Default\\History"
  # Linux:   "~/.config/google-chrome/Default/History"

# --- Periodo ed elaborazione a finestre ---
processing:
  window_size_days: 30             # dimensione della finestra interna di lavoro
                                    # (indipendente da --group-by, vedi §4)
  max_batches_per_run: null        # es. 5 per un test limitato, null = nessun limite

# --- Filtro / pulizia (gratis, locale) ---
filtering:
  min_visit_duration_seconds: 15
  min_visit_count: 1
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
  dedupe_by: url_normalizzato

# --- Triage economico ---
triage:
  enabled: true
  batch_size: 200
  prompt: |
    Ricevi una lista di voci di cronologia browser (dominio + titolo pagina).
    Per ciascuna, rispondi solo "rilevante" o "rumore": rilevante se la pagina
    indica un contenuto di apprendimento/approfondimento personale (articoli,
    libri, concetti, video educativi, notizie approfondite, film); rumore se
    è contenuto operativo/ripetitivo (mail, social feed, ricerche generiche,
    shopping, banking, strumenti di lavoro quotidiani).

# --- Categorie e prompt di classificazione finale ---
classification:
  batch_size: 50
  categories:
    - name: "Filosofia e Storia"
      description: "Idee filosofiche, correnti di pensiero, eventi ed episodi storici approfonditi"
    - name: "Concetti e parole nuove"
      description: "Termini tecnici, concetti scientifici o culturali nuovi incontrati e approfonditi"
    - name: "Libri"
      description: "Libri scoperti, recensiti, o di cui si è cercato approfondimento"
    - name: "Fatti storici o attuali interessanti"
      description: "Notizie o fatti specifici, storici o di attualità, approfonditi"
    - name: "Film"
      description: "Film visti o cercati con l'intento di guardarli"
  prompt: |
    Analizza le seguenti voci di cronologia browser (url, titolo, numero di visite).
    Per ciascuna voce potenzialmente rilevante, assegna una delle categorie:
    {categories_list}
    Scrivi una riga sintetica, in stile diario personale (max 20 parole), di cosa
    è stato imparato/scoperto — NON limitarti a copiare il titolo grezzo della pagina.
    Includi l'URL tra parentesi solo se utile per ritrovare la fonte (es. video, pagina specifica).
    Ignora le voci che non si adattano chiaramente a nessuna categoria.
    Rispondi SOLO in JSON in questo formato:
    [{{"categoria": "...", "sintesi": "...", "url": "..." }}]

# --- Output ---
output:
  base_dir: "./Archivio_Studio"
  group_by: month                  # month | week | days:N | all (sovrascrivibile da CLI)
  file_format: txt                 # txt | md
  filename_from: category
```

## 8. Dettaglio dei moduli

### 8.1 `extractor.py`
- Copia il file `History` (SQLite) in una posizione temporanea, perché Chrome lo blocca mentre è in esecuzione.
- Query sulla tabella `urls`:
  ```sql
  SELECT url, title, visit_count, last_visit_time
  FROM urls
  WHERE last_visit_time BETWEEN ? AND ?
  ```
- I timestamp di Chrome sono in microsecondi dal 1601-01-01 (epoch WebKit): vanno convertiti.
- Restituisce una lista di record `{url, title, visit_count, last_visit}`.

### 8.2 `cleaner.py`
- Normalizza URL: rimuove `utm_*`, `fbclid`, `session_id`, trailing slash, ecc.
- Applica `domain_blacklist` e `url_keyword_blacklist` da config.
- Deduplica per `url_normalizzato`, sommando i `visit_count`.
- Scarta voci sotto `min_visit_count` / `min_visit_duration_seconds` se configurato.
- Output: lista ridotta di voci uniche.

### 8.3 `triage.py`
- Divide le voci pulite in batch (`triage.batch_size`).
- Manda a un modello economico solo `dominio + titolo` con il prompt di triage.
- Riceve un flag rilevante/rumore per ciascuna voce.
- Tiene solo le voci marcate rilevanti.

### 8.4 `llm_client.py`
Interfaccia comune, un adapter per provider:

```python
class LLMClient:
    def complete(self, prompt: str, model: str) -> str:
        raise NotImplementedError

class AnthropicClient(LLMClient):
    def complete(self, prompt, model):
        # chiamata a api.anthropic.com/v1/messages
        ...

class OpenAIClient(LLMClient):
    ...

class OllamaClient(LLMClient):
    ...

def get_client(provider: str) -> LLMClient:
    ...
```

### 8.5 `classifier.py`
- Prende le voci rilevanti (dopo triage, o tutte se triage disabilitato).
- Le divide in batch (`classification.batch_size`).
- Costruisce il prompt sostituendo `{categories_list}` con le categorie definite in config.
- Invia ogni batch, richiedendo output JSON strutturato.
- Valida il JSON ricevuto (retry con correzione se malformato).
- Restituisce lista di oggetti `{categoria, sintesi, url}`.

### 8.6 `windowing.py`
- Prende `--from`/`--to` (o `--last-days`) e `processing.window_size_days`.
- Genera la lista di finestre interne da processare in sequenza.
- Consulta `state/checkpoint.json` per saltare le finestre già completate.

### 8.7 `writer.py`
- Riceve `--group-by` per decidere in quale sotto-cartella/file scrivere ogni voce classificata, in base al `last_visit_time` originale della voce (non alla finestra di elaborazione).
- Per `days:N`, calcola il bucket come intervallo `[data_inizio_periodo + k*N, data_inizio_periodo + (k+1)*N)`.
- Formatta l'output secondo `file_format` (txt o md).
- Aggiunge solo voci il cui hash (url normalizzato + categoria) non è già in `state/processed_ids.json`.

### 8.8 `main.py`
```bash
python src/main.py \
  --config config.yaml \
  --from 2026-01-01 --to 2026-12-31 \
  --group-by month \
  --window-size-days 30
```
Step: carica config → genera finestre (windowing.py) → per ogni finestra non completata: estrai → pulisci → triage → classifica → scrivi → aggiorna checkpoint e processed_ids → log costi.

## 9. GUI locale minimale

Interfaccia locale (nel browser, non un'app da installare) per gestire tutto senza toccare `config.yaml` a mano o usare la CLI. Tecnologia consigliata: **Streamlit** (un solo file Python, nessun frontend separato da scrivere, si avvia con `streamlit run src/gui.py` e apre una pagina in locale).

**Sezioni della GUI:**

1. **Periodo di estrazione**
   - Selettore range di date (da/a), oppure toggle "ultimi N giorni" con campo numerico.
   - Percorso del file `History` di Chrome (precompilato in base al sistema operativo, modificabile).

2. **Granularità di output**
   - Selezione: Mensile / Settimanale / Ogni N giorni (con campo N) / Un unico blocco.
   - Indipendente dal periodo di estrazione, come da §4.

3. **Destinazione e nomi file**
   - Campo per la cartella di output (`output.base_dir`), con selezione da file system.
   - Pattern nome cartella periodo (es. `YYYY-MM`, `YYYY-[W]WW`, personalizzabile).
   - Formato file: `.txt` o `.md`.
   - Pattern nome file per categoria (slug automatico dal nome categoria, o mapping manuale nome categoria → nome file).

4. **Categorie**
   - Tabella editabile: nome categoria + descrizione, con pulsanti aggiungi/rimuovi/riordina.
   - Textarea per il prompt di classificazione, con placeholder `{categories_list}`.

5. **Filtri**
   - Lista editabile di domini in blacklist, parole chiave URL in blacklist.
   - Campi numerici per soglie minime (visite, durata).

6. **Provider LLM**
   - Selezione provider (Anthropic/OpenAI/Ollama), modello principale, modello di triage.
   - Indicazione se la variabile d'ambiente con la API key è impostata (senza mostrarne il valore).
   - Toggle per abilitare/disabilitare il triage.

7. **Esecuzione**
   - Pulsante "Avvia elaborazione" che salva la configurazione corrente in `config.yaml` e lancia la pipeline (`main.py`) in background.
   - Vista di avanzamento in tempo reale: finestra temporale in elaborazione, numero di voci per stadio (grezze → pulite → triage → classificate), stima costo/token aggiornata.
   - Pulsante "Interrompi" che salva comunque il checkpoint raggiunto fino a quel momento (l'idempotenza di §5 lo permette senza rischi).

8. **Storico esecuzioni**
   - Elenco delle esecuzioni passate lette da `logs/`, con periodo processato, voci scritte, costo stimato.

**Nota implementativa:** la GUI non duplica la logica della pipeline — importa direttamente le funzioni di `windowing.py`/`main.py` e mostra il progresso leggendo lo stesso `state/checkpoint.json` e gli stessi log usati dalla CLI. In questo modo GUI e CLI restano sempre coerenti e intercambiabili (si può avviare da CLI e controllare lo stato dalla GUI, o viceversa).

## 10. Automazione periodica

```
# cron, ogni domenica sera: aggiorna con l'ultima settimana
0 20 * * 0 cd /path/chrono-catalogatore && python src/main.py --config config.yaml --last-days 7 --group-by month
```
Su Windows: Task Scheduler con lo stesso comando. Grazie a idempotenza + checkpoint, è sicuro far girare periodicamente uno script "recupera tutto ciò che manca da inizio anno" senza duplicati né ripartenze da zero.

## 11. Stack tecnico

- Python 3.11+
- `sqlite3` (libreria standard) per l'estrazione
- `pyyaml` per la configurazione
- `httpx` per le chiamate API
- `pydantic` per validare lo schema JSON restituito dall'LLM
- `streamlit` per la GUI locale (vedi §9)
- Nessun database esterno: stato salvato in JSON locale

## 12. Estensioni future (facoltative, non nel primo rilascio)

- Supporto multi-browser (Firefox, Edge).
- Piccola UI locale (Streamlit) per rivedere/correggere le classificazioni prima della scrittura definitiva.
- Estrazione del primo paragrafo della pagina (oltre al solo titolo) per voci ambigue.
- Statistiche aggregate (quante voci per categoria, andamento nel tempo).
- Modalità "multi-profilo": più `config.yaml` per usi diversi (studio, produttività, ecc.).

## 13. Ordine di implementazione consigliato

1. `extractor.py` + `cleaner.py`, verifica reale su una settimana di cronologia.
2. `config.yaml` con le categorie di default.
3. `llm_client.py` (solo Anthropic) + `classifier.py` senza triage, per validare il formato di output end-to-end su una settimana.
4. `writer.py` con idempotenza.
5. Aggiungere `triage.py` e misurare la riduzione reale del volume.
6. `windowing.py` con checkpoint, testato su un mese.
7. Estensione a periodi lunghi (`--group-by`, finestre multiple), test su un anno intero.
8. `gui.py`, collegando i controlli alla configurazione e alla pipeline già funzionante da CLI.
9. Automazione con cron/Task Scheduler.