# Piano di Jarvis

Assistente AI locale in Python, costruito a fasi. Questo file tiene traccia di cosa
è fatto e cosa manca. Legenda: ✅ fatto · 🔜 prossimo · ⏳ da fare · 💤 opzionale.

## Idea e vincoli

- **Cos'è**: un agente AI locale che conversa in italiano e compie azioni reali sul
  computer tramite *tool*, con un loop agentico (il modello DECIDE, il codice ESEGUE).
- **Modello**: API Anthropic `claude-sonnet-4-6` (chiave in `ANTHROPIC_API_KEY`).
- **Vincolo dipendenze**: SOLO `anthropic`, `rich`, `python-dotenv`. Tutto il resto è
  libreria standard. (Unica eccezione prevista: la FASE 6 "Voce", opzionale.)
- **Fail loud**: niente `except: pass`; un `except` mirato e motivato va bene. Non si
  finge mai un successo: gli errori dei tool tornano al modello come osservazione.
- **Architettura**: `brain.py` è un loop GENERICO; la logica trasversale sta in moduli
  infrastruttura (`safety.py`, `memory.py`, `history.py`, e presto `logger.py`); i tool
  sono famiglie in `tools/`; `main.py` è solo la REPL (nessun'altra parte stampa).

## Stato delle fasi

### ✅ FASE 1–2 — Loop agentico
Ciclo agentico di base + primo tool `get_system_info`. Il modello può chiedere un
tool, riceverne il risultato e continuare, per più giri.

### ✅ FASE 3 — Sicurezza (`safety.py`)
Livelli di rischio SAFE / CAUTION / DANGEROUS, `confirm()`, blacklist di comandi
sempre vietati, `SANDBOX_ROOT` + `ensure_in_sandbox()`, guardiano anti-SSRF
`ensure_url_sicuro()`.

### ✅ FASE 4 — Tool con effetti reali
Quattro famiglie in `tools/`:
- **File** (`files.py`): leggi/scrivi/elenca/cerca/sposta, confinati in sandbox.
- **Sistema** (`system.py`): info, processi, apri app, chiudi processo, screenshot.
- **Shell** (`shell.py`): esecuzione comandi con blacklist e conferma.
- **Web** (`web.py`): `leggi_pagina(url)` (anti-SSRF, HTML→testo) + RICERCA online via
  web search tool nativo dell'API (server tool).

### ✅ FASE 5 — Memoria
- **5a — memoria LUNGA**: fatti persistenti su SQLite. `memory.py` (infra, singleton
  di connessione, FTS5 con ripiego LIKE onesto) + `tools/memory.py` con `ricorda` /
  `richiama` (entrambi SAFE). I fatti recenti sono iniettati nel system prompt all'avvio
  (RAG semplificato: iniezione + richiamo on-demand). Path via `JARVIS_MEMORY`.
- **5b — memoria BREVE**: `history.py` compatta la cronologia della conversazione quando
  `response.usage.input_tokens` supera `JARVIS_MAX_TOKENS` (default 40000). Strategia:
  riassunto della parte vecchia (con troncamento come ripiego), tagliando solo su
  confini sicuri (inizio di un vero turno utente, mai dentro una coppia
  tool_use/tool_result). Visibile via callback `on_note`.

### 💤 FASE 6 — Voce (OPZIONALE) — scheletro pronto, audio da collaudare in locale
Wrapper attorno al loop, senza toccarlo (`voice.py`): microfono → STT → `agent.chat` →
TTS → altoparlante. Disattivabile: opt-in con `--voce` o `JARVIS_VOICE=1`, altrimenti
Jarvis resta testuale e il core resta a 3 dipendenze.
- **Fatto qui**: l'orchestrazione `ciclo_vocale` (con wake word e frasi d'uscita), scritta
  contro BACKEND iniettabili → testata su MOCK del flusso non-audio; import GUARDATI in
  `crea_backend_reali` (se le deps mancano → messaggio chiaro + ripiego sul testo in
  `main.py`); `requirements-voice.txt` separato; smoke-test che verifica il design guardato.
- **Da fare in locale**: collaudare/rifinire i backend audio reali (faster-whisper, piper,
  sounddevice) — servono microfono/altoparlanti, assenti in un ambiente cloud headless —
  e migliorare ascolto (rilevazione del silenzio) e conferma vocale delle azioni.
> **Avvertenza**: le librerie audio sono l'ECCEZIONE al vincolo dipendenze (pesanti,
> specifiche per OS): stanno in `requirements-voice.txt`, mai nel core.

### ✅ FASE 7 — Rifinitura
Consolidamento, stdlib-only. Spezzata in sotto-passi:
- ✅ **7a — `logger.py`**: ogni tool call loggata su file JSONL (`quando`, `tool`, `input`,
  `output` troncato, `is_error`, `esito` ok/errore/rifiutato, `durata_ms`, `rischio`), in
  append. Path via `JARVIS_LOG` (default `~/Jarvis-Sandbox/jarvis.jsonl`). Chiamato da
  `brain.py` attorno al dispatch (durata via `time.monotonic()`); logga anche i rifiuti
  (precheck e conferma). Osservabilità, non blocca mai l'assistente: su path non
  scrivibile avvisa LOUD una volta e prosegue.
- ✅ **7b — retry/errori API**: robustezza delle chiamate a `client.messages.create`.
  Il retry dei guasti TRANSITORI (rete/timeout, 429, ≥500 incl. 529) è quello NATIVO
  dell'SDK, solo configurato (`max_retries` via `JARVIS_API_RETRIES`, default 4; timeout
  opzionale via `JARVIS_API_TIMEOUT`), non reinventato. `descrivi_errore_api()` (funzione
  pura) classifica transitori vs PERMANENTI (401→chiave, 400/413/404/403; 408/409/5xx per
  codice) con messaggi mirati, fail-closed sul non classificabile. Graceful degradation:
  `chat()` non propaga mai un guasto (REPL viva) e, tramite un CHECKPOINT, ripristina una
  cronologia coerente qualunque cosa vada storta a metà turno (errore API, EOFError da una
  conferma, guardia `pause_turn`, troncamento `max_tokens` con tool_use spaiato). Rete di
  sicurezza in `main.py` (except largo, fail loud) come difesa in profondità.
- ✅ **7c — README + esempi**: `README.md` in italiano con cos'è, setup avviabile,
  architettura (moduli + diagramma Mermaid del loop), elenco dei tool, sicurezza/sandbox,
  memoria, osservabilità, robustezza, tabella variabili d'ambiente, 3 esempi end-to-end e
  limiti noti. Aggiunto `smoke_test.py` (stdlib, senza chiave/rete): verifica a secco che
  import, registro dei tool e un tool SAFE reggano. `.env.example` completato con
  `JARVIS_SANDBOX`.

## Mappa branch ↔ fasi

I nomi dei branch NON coincidono con i numeri di FASE (sono sequenziali per conto loro):

| Branch                              | Contenuto                         |
|-------------------------------------|-----------------------------------|
| `claude/jarvis-phase-4a-files`      | FASE 4 — File                     |
| `claude/jarvis-phase-4b-system`     | FASE 4 — Sistema                  |
| `claude/jarvis-phase-5-shell`       | FASE 4 — Shell                    |
| `claude/jarvis-phase-6-web`         | FASE 4 — Web                      |
| `claude/jarvis-phase-7-memory`      | FASE 5 — Memoria (5a + 5b)        |
| `claude/jarvis-phase-8-logger`      | FASE 7a — logger                  |
| `claude/jarvis-phase-9-resilience`  | FASE 7b — retry/errori API        |

Ogni fase parte dal branch della precedente e crea il proprio; si pusha solo sul branch
della fase in corso.

## Variabili d'ambiente

| Variabile           | A cosa serve                                             | Default                              |
|---------------------|----------------------------------------------------------|--------------------------------------|
| `ANTHROPIC_API_KEY` | Chiave API Anthropic (obbligatoria)                      | —                                    |
| `JARVIS_SANDBOX`    | Cartella sicura per le operazioni sui file               | `~/Jarvis-Sandbox`                   |
| `JARVIS_MEMORY`     | File SQLite della memoria lunga                          | `~/Jarvis-Sandbox/jarvis_memory.db`  |
| `JARVIS_MAX_TOKENS` | Soglia token oltre cui compattare la cronologia          | `40000`                              |
| `JARVIS_LOG`        | File JSONL delle tool call (FASE 7a)                     | `~/Jarvis-Sandbox/jarvis.jsonl`      |
| `JARVIS_API_RETRIES`| Ritentativi SDK sugli errori transitori (FASE 7b, >= 0)  | `4`                                  |
| `JARVIS_API_TIMEOUT`| Timeout in secondi sulla richiesta API (FASE 7b, > 0)    | default SDK                          |
| `JARVIS_VOICE`      | Attiva la modalità voce (FASE 6, opzionale)              | disattivata                          |
| `JARVIS_TTS`        | Motore TTS: `say` (macOS), `piper`, o `auto`             | `auto` (→ `say` su macOS)            |
| `JARVIS_SAY_VOICE`  | Voce del comando `say` su macOS (es. `Alice`, `Luca`)    | voce di sistema                      |
| `JARVIS_PIPER_MODEL`| Percorso del modello voce piper (.onnx) per il TTS       | nome di comodo (da impostare)        |
