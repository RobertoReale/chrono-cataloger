# chrono-catalogatore

Trasforma la **cronologia di navigazione di Chrome** in un **diario personale
automatico**: uno storico organizzato per categoria (filosofia, libri, film,
concetti, fatti storici…) di ciò che hai studiato, scoperto e approfondito nel
tempo. Più in generale, è uno **strumento di analisi della propria cronologia**
secondo categorie e prompt scelti liberamente dall'utente.

Gira interamente in locale, tranne la singola chiamata API al modello LLM scelto
(Anthropic di default, sostituibile con OpenAI o un modello locale via Ollama).

```
Archivio_Studio/
└── 2026-07/
    ├── filosofia-e-storia.txt
    ├── concetti-e-parole-nuove.txt
    ├── libri.txt
    ├── fatti-storici-o-attuali.txt
    └── film.txt
```

Esempio di `filosofia-e-storia.txt`:

```
Idee fondamentali di Hegel - Dialettica Servo-Padrone
Il tema di tedesco di Karl Marx per l'esame di licenza liceale (12 agosto 1835)
Idee fondamentali di Spinoza
Carl Schmitt (Il Führer protegge il diritto) e La Notte dei lunghi coltelli
```

## Come funziona (pipeline)

```
Chrome History (SQLite)
   ├─ 1. extractor  → legge il range di date, converte i timestamp WebKit
   ├─ 2. cleaner    → normalizza URL, rimuove tracking, blacklist, dedup   (locale, gratis)
   ├─ 3. triage     → modello economico (Haiku), batch grandi: rilevante/rumore
   ├─ 4. classifier → modello principale (Sonnet), batch piccoli: {categoria, sintesi, url}
   └─ 5. writer     → scrive/aggiorna i .txt per categoria, idempotente
```

Il triage riduce il volume del 90-95% **prima** dello stadio costoso, quindi i
batch di classificazione restano sempre piccoli. Su cronologie lunghe (un anno,
decine di migliaia di voci) l'elaborazione procede sempre a **finestre temporali
interne** con **checkpoint**: un'esecuzione interrotta riparte da dove si era
fermata, senza rielaborare né ripagare le finestre già fatte.

## Installazione

Richiede **Python 3.11+**.

```bash
git clone https://github.com/RobertoReale/chrono-catalogatore.git
cd chrono-catalogatore
python -m venv .venv
# Windows:  .venv\Scripts\activate      |  macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml      # poi adatta config.yaml
export ANTHROPIC_API_KEY=sk-ant-...     # la tua chiave (Windows: setx / $env:)
```

## Uso da riga di comando

```bash
# Range di date esplicito, output mensile (default)
python -m src.main --from 2026-01-01 --to 2026-12-31 --group-by month

# Ultimi 90 giorni, output settimanale
python -m src.main --last-days 90 --group-by week

# Blocchi personalizzati di 10 giorni
python -m src.main --from 2026-06-01 --to 2026-07-31 --group-by days:10

# Un unico file per l'intero periodo
python -m src.main --from 2026-01-01 --to 2026-12-31 --group-by all

# Test limitato: solo le prime 2 finestre, senza classificare/scrivere
python -m src.main --last-days 30 --max-batches-per-run 2 --dry-run
```

**Periodo di estrazione** (`--from/--to` o `--last-days`) e **granularità di
output** (`--group-by`) sono indipendenti: il writer instrada ogni voce nella
cartella giusta guardando il `last_visit_time` originale della voce, non la data
di esecuzione.

| Flag | Descrizione |
|---|---|
| `--from` / `--to` | range esplicito `YYYY-MM-DD` |
| `--last-days N` | alternativa: ultimi N giorni da oggi |
| `--group-by` | `month` \| `week` \| `days:N` \| `all` |
| `--window-size-days` | dimensione finestra interna (override config) |
| `--history-path` | percorso al file `History` di Chrome (override config) |
| `--max-batches-per-run` | limita il numero di finestre per esecuzione (test) |
| `--reset-checkpoint` | azzera il checkpoint e riparte da capo |
| `--dry-run` | estrai + pulisci + triage, senza classificare né scrivere |

## GUI locale (Streamlit)

Per gestire tutto senza toccare `config.yaml` a mano:

```bash
streamlit run src/gui.py
```

La GUI **non duplica** la logica: importa gli stessi moduli della CLI, salva la
configurazione in `config.yaml` e mostra il progresso leggendo lo stesso stato.
CLI e GUI restano quindi intercambiabili.

## Configurazione (`config.yaml`)

Le **categorie** e i **prompt** non sono hardcoded: vivono in `config.yaml`.
Vedi [`config.example.yaml`](config.example.yaml) per il file commentato. In breve:

- `llm`: provider (`anthropic`/`openai`/`ollama`), modello principale, modello di triage.
- `source.history_path`: `null` per auto-rilevamento in base al sistema operativo.
- `processing.window_size_days`: dimensione delle finestre interne di lavoro.
- `filtering`: blacklist domini/keyword, soglie minime, strip dei query param.
- `triage.prompt`: i **criteri** rilevante/rumore (il formato di output lo impone il codice).
- `classification.categories` + `classification.prompt`: le categorie e il prompt di sintesi,
  con il placeholder `{categories_list}`.
- `output`: cartella base, `group_by`, formato file (`txt`/`md`).

### Percorso del file History di Chrome

Auto-rilevato se `source.history_path: null`. Percorsi tipici:

| OS | Percorso |
|---|---|
| Windows | `%LOCALAPPDATA%\Google\Chrome\User Data\Default\History` |
| macOS | `~/Library/Application Support/Google/Chrome/Default/History` |
| Linux | `~/.config/google-chrome/Default/History` |

> Chrome blocca il file mentre è aperto: lo strumento ne fa una **copia
> temporanea** in sola lettura, quindi non serve chiudere il browser.

## Il tuning dei prompt (la parte davvero delicata)

La sfida non è ingegneristica ma di **tuning**: far sì che i prompt di triage e
classificazione producano risultati davvero utili si affina solo guardando
output reali e iterando. Per rendere questa iterazione sicura, il progetto separa
nettamente due responsabilità:

- **Tu** modifichi i *criteri* (`triage.prompt`, `classification.prompt`,
  categorie e descrizioni). È qui che si fa il tuning.
- **Il codice** impone il *formato di output* JSON e lo re-associa alle voci
  originali tramite indice numerico. Così cambiare i criteri non rompe mai il
  parsing.

Difese già integrate contro output imperfetti dell'LLM:

- triage: un batch con risposta non parseabile viene **tenuto per prudenza** (un
  falso positivo è meno grave di scartare qualcosa di rilevante — verrà comunque
  filtrato meglio dalla classificazione);
- classificazione: JSON malformato → **un retry** con richiesta di correzione;
  categorie non riconosciute → scartate (con match case-insensitive al nome
  canonico); voci senza indice → ignorate.

Flusso di tuning consigliato:

1. `--last-days 7 --max-batches-per-run 1` per lavorare su un campione piccolo.
2. Guarda i `.txt` prodotti e il log in `logs/run_<data>.json` (conteggi per
   stadio, token e costo stimati).
3. Ritocca i prompt/categorie e rilancia. Grazie all'idempotenza puoi rilanciare
   sullo stesso periodo senza duplicare le voci già scritte.

## Idempotenza e ripresa

- `state/processed_ids.json`: hash (url normalizzato + categoria) delle voci già
  scritte → rieseguire non duplica nulla.
- `state/checkpoint.json`: ultima finestra completata → un rilancio salta le
  finestre già fatte.
- `logs/run_<data>.json`: voci per stadio, token e costo stimati per esecuzione.

## Automazione periodica

```bash
# cron, ogni domenica sera: recupera l'ultima settimana
0 20 * * 0 cd /path/chrono-catalogatore && python -m src.main --last-days 7 --group-by month
```

Su Windows: Task Scheduler con lo stesso comando. Grazie a idempotenza +
checkpoint è sicuro far girare periodicamente uno script "recupera tutto ciò che
manca" senza duplicati né ripartenze da zero.

## Sviluppo e test

```bash
pip install pytest
pytest -q
```

I test usano un DB SQLite sintetico in stile Chrome e un client LLM finto: non
richiedono né Chrome né una API key, e coprono estrazione, pulizia/dedup,
finestre/checkpoint, scrittura idempotente, parsing di triage/classificazione
(inclusi output malformati) e la pipeline end-to-end.

## Struttura del repository

```
chrono-catalogatore/
├── config.example.yaml      # configurazione di riferimento commentata
├── requirements.txt
├── src/
│   ├── extractor.py         # estrazione da SQLite Chrome, per range di date
│   ├── cleaner.py           # normalizzazione, blacklist, dedup
│   ├── triage.py            # pre-filtro economico (Haiku)
│   ├── llm_client.py        # interfaccia astratta ai provider LLM
│   ├── classifier.py        # classificazione fine + sintesi (Sonnet)
│   ├── writer.py            # scrittura file, idempotenza, granularità output
│   ├── windowing.py         # finestre interne + checkpoint
│   ├── config.py            # caricamento/validazione config
│   ├── costs.py             # stima token/costo per il log
│   ├── models.py            # modelli dati + conversione timestamp WebKit
│   ├── main.py              # CLI di orchestrazione
│   └── gui.py               # GUI locale (Streamlit)
├── tests/
└── README.md
```

## Privacy

Tutta l'elaborazione è locale. L'unico dato che lascia la macchina è
**dominio + titolo** (triage) e **url + titolo + numero di visite** (classificazione),
inviati al provider LLM scelto. Con Ollama nulla lascia la macchina. Le cartelle
`state/`, `logs/` e `Archivio_Studio/` contengono dati personali e sono escluse
dal versioning tramite `.gitignore`.

## Licenza

MIT — vedi [LICENSE](LICENSE).
