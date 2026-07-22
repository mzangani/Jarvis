# Jarvis

Assistente AI **locale** in Python: conversa in italiano e compie azioni **reali** sul
tuo computer tramite *strumenti* (tool), con un **loop agentico** — il modello DECIDE,
il codice ESEGUE. Usa l'API Anthropic con **livelli di ragionamento** adattivi: parte
economico (`claude-haiku-4-5`) e sale ai modelli più potenti quando il compito lo
richiede (vedi [Livelli di ragionamento](#livelli-di-ragionamento-fase-10)).

> Progetto costruito a fasi, con un occhio didattico. Lo stato di ciascuna fase è in
> [`PIANO.md`](PIANO.md).

---

## Cos'è

Jarvis è un piccolo agente che gira sul tuo computer. Gli scrivi in linguaggio naturale;
lui, quando serve, non risponde "a memoria" ma **usa un tool** che compie un'azione vera
(legge un file, esegue un comando, cerca sul web, ricorda un fatto…), ne osserva il
risultato e continua, per più giri, finché non ha la risposta. Le azioni che modificano
il sistema o i file **chiedono conferma** prima di partire.

## Requisiti

- **Python 3.10 o superiore** (il codice usa la sintassi dei tipi `X | None`).
- Una **chiave API Anthropic** (variabile `ANTHROPIC_API_KEY`).
- Dipendenze: **solo** `anthropic`, `rich`, `python-dotenv`. Tutto il resto è libreria
  standard di Python (vincolo di progetto: niente dipendenze pesanti).

## Installazione e avvio

```bash
# 1. (consigliato) crea e attiva un ambiente virtuale
python -m venv .venv
source .venv/bin/activate        # su Windows: .venv\Scripts\activate

# 2. installa le dipendenze
pip install -r requirements.txt

# 3. crea il tuo file .env dalla copia d'esempio e inserisci la chiave
cp .env.example .env
#   poi apri .env e metti la tua ANTHROPIC_API_KEY

# 4. avvia
python main.py
```

Scrivi `esci` (oppure `exit`/`quit`, o premi Ctrl-C) per terminare.

> Verifica rapida senza chiave né rete: `python smoke_test.py` (vedi
> [Verifica rapida](#verifica-rapida-smoke-test)).

## Architettura

La regola dell'architettura è la **separazione dei ruoli**: il "cervello" non sa nulla
dell'interfaccia, e l'unica parte che stampa è la REPL.

| File / cartella | Ruolo |
|---|---|
| `main.py` | La **REPL**: legge l'input, chiama l'agente, stampa (con `rich`). È l'unico file che scrive a schermo. |
| `server.py` + `ui/` | Il **front end web** (Fase 9): server locale stdlib + pagina HUD stile Iron Man. |
| `brain.py` | Il **loop agentico GENERICO**: tiene la cronologia, chiama l'API, esegue i tool richiesti e rimanda i risultati al modello; gestisce robustezza e compattazione. |
| `safety.py` | **Sicurezza**: livelli di rischio, conferma, sandbox dei file, blacklist della shell, guardiano anti-SSRF per il web. |
| `memory.py` | **Memoria lunga**: fatti persistenti su SQLite. |
| `history.py` | **Memoria breve**: compattazione (riassunto) della cronologia quando cresce troppo. |
| `logger.py` | **Osservabilità**: traccia ogni tool call su file JSONL. |
| `tools/` | Le **famiglie di tool** (`system`, `files`, `shell`, `web`, `memory`), aggregate per `brain.py`. |

### Il loop agentico

Il cuore di Jarvis. Il modello **decide** se e quale tool usare; il nostro codice lo
**esegue** (dopo i controlli di sicurezza) e gli rimanda il risultato, ripetendo finché
il modello non ha una risposta pronta.

```mermaid
flowchart TD
    U[Input utente] --> A["API: messages.create<br/>(system + cronologia + tool)"]
    A --> S{stop_reason?}
    S -->|end_turn / max_tokens| R[Risposta all'utente]
    S -->|tool_use| L[Per ogni tool richiesto]
    L --> P{precheck<br/>categorico}
    P -->|vietato| X[tool_result: rifiutato]
    P -->|ok| C{rischio SAFE?}
    C -->|sì| D[dispatch: esegue il tool]
    C -->|no| Q{conferma utente?}
    Q -->|rifiuta| X
    Q -->|accetta| D
    D --> T[tool_result: esito]
    X --> A
    T --> A
    R --> F([fine turno])
```

Ogni `tool_result` rientra nell'API: è questo che chiude il ciclo. A fine turno, se la
cronologia è troppo lunga, viene **compattata**; se una chiamata all'API fallisce, il
turno degrada con grazia senza far morire la REPL e **senza lasciare la cronologia
spezzata** (vedi [Robustezza](#robustezza-fase-7b)).

## Gli strumenti (tool)

Ogni tool ha un **livello di rischio**: `SAFE` (esegue subito), `CAUTION`/`DANGEROUS`
(chiede conferma prima di agire).

| Famiglia | Tool | Rischio |
|---|---|---|
| **Sistema** | `get_system_info`, `elenca_processi` | SAFE |
| | `apri_applicazione`, `screenshot` | CAUTION |
| | `chiudi_processo` | DANGEROUS |
| **File** | `leggi_file`, `lista_dir`, `cerca_per_nome`, `cerca_nel_contenuto`, `info_file`, `hash_file` | SAFE |
| | `scrivi_file`, `sposta`, `crea_cartella`, `comprimi_zip`, `estrai_zip` | CAUTION |
| **Shell** | `esegui_comando` | DANGEROUS |
| **Web** | `leggi_pagina` | CAUTION |
| | `apri_url` (apre una fonte nel tuo browser, solo dopo il tuo sì) | SAFE |
| | `web_search` (eseguito dai server Anthropic) | — |
| **Memoria** | `ricorda`, `richiama` | SAFE |
| **Appunti** | `leggi_clipboard`, `scrivi_clipboard`, `aggiungi_nota`, `elenca_note` | SAFE |
| **Utilità** | `data_ora`, `meteo` | SAFE |
| **Mac** | `volume`, `musica`, `notifica`, `timer`, `timer_attivi` | SAFE |
| **Calendario** | `impegni`, `promemoria_lista`, `promemoria_aggiungi` | SAFE |
| **Domotica** | `luce`, `tapparella`, `stato_luce`, `punti_scs` (BTicino/SCS) | SAFE |

## Sicurezza e sandbox

- **Conferma esplicita** prima di ogni azione non-`SAFE`: se rifiuti, Jarvis lo sa e può
  proporre un'alternativa.
- **Sandbox dei file**: le operazioni su file sono confinate a una cartella sicura
  (`JARVIS_SANDBOX`, default `~/Jarvis-Sandbox`); i percorsi al suo esterno vengono
  rifiutati — ed è normale.
- **Blacklist della shell**: alcuni comandi distruttivi sono **sempre** vietati, prima
  ancora della conferma.
- **Web anti-SSRF**: `leggi_pagina` accetta solo http/https e blocca gli indirizzi locali
  e di rete privata; non esegue JavaScript.

Jarvis non finge mai di aver eseguito un'azione: l'errore di un tool torna al modello come
osservazione, così può correggersi.

## Memoria

- **Lunga** (`memory.py`): fatti persistenti su SQLite. `ricorda` salva un fatto durevole,
  `richiama` lo ritrova (ricerca per parole chiave, non semantica). I fatti più recenti
  sono **iniettati** nel system prompt a ogni turno (un RAG semplificato). File in
  `JARVIS_MEMORY` (default `~/Jarvis-Sandbox/jarvis_memory.db`).
- **Breve** (`history.py`): quando la cronologia della conversazione supera
  `JARVIS_MAX_TOKENS` (default 40000), la parte più vecchia viene **riassunta** (con
  troncamento come ripiego), tagliando solo su confini sicuri per non spezzare mai una
  coppia `tool_use`/`tool_result`.

## Osservabilità (log)

`logger.py` scrive **ogni** tool call su un file JSONL (`JARVIS_LOG`, default
`~/Jarvis-Sandbox/jarvis.jsonl`): quando, quale tool, input, output (troncato), esito
(`ok`/`errore`/`rifiutato`), durata e rischio. Utile per capire cosa ha fatto Jarvis e per
il debug. Non blocca mai l'assistente: se il file non è scrivibile, avvisa una volta e
prosegue.

## Robustezza (Fase 7b)

Le chiamate all'API sono resistenti ai guasti:

- **Retry** automatico degli errori *transitori* (rete/timeout, `429`, `5xx`/`529`): è
  quello **nativo** dell'SDK, solo configurato — `JARVIS_API_RETRIES` (default 4) e,
  opzionale, `JARVIS_API_TIMEOUT`.
- **Errori permanenti** (es. `401` chiave errata) → messaggio chiaro, nessun retry inutile.
- **Graceful degradation**: se una chiamata fallisce comunque, la REPL **non muore**;
  ricevi un messaggio leggibile e la cronologia resta **coerente** per il turno successivo.

## Variabili d'ambiente

| Variabile | A cosa serve | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Chiave API Anthropic (**obbligatoria**) | — |
| `JARVIS_SANDBOX` | Cartella sicura per le operazioni sui file | `~/Jarvis-Sandbox` |
| `JARVIS_MEMORY` | File SQLite della memoria lunga | `~/Jarvis-Sandbox/jarvis_memory.db` |
| `JARVIS_NOTES` | File di testo delle note veloci (`aggiungi_nota`/`elenca_note`) | `note.txt` (nella sandbox) |
| `JARVIS_MAX_TOKENS` | Soglia token oltre cui compattare la cronologia | `40000` |
| `JARVIS_LOG` | File JSONL delle tool call | `~/Jarvis-Sandbox/jarvis.jsonl` |
| `JARVIS_API_RETRIES` | Ritentativi SDK sugli errori transitori (≥ 0) | `4` |
| `JARVIS_API_TIMEOUT` | Timeout in secondi sulla richiesta API (> 0) | default SDK |
| `JARVIS_VOICE` | Attiva la modalità voce (Fase 6, opzionale) | disattivata |
| `JARVIS_TTS` | Motore TTS della voce: `say` (macOS), `piper`, o `auto` | `auto` (→ `say` su macOS) |
| `JARVIS_SAY_VOICE` | Voce del comando `say` su macOS (es. `Alice`, `Luca`) | voce di sistema |
| `JARVIS_PIPER_MODEL` | Percorso del modello voce piper (.onnx) per il TTS | nome di comodo (da impostare) |
| `JARVIS_VAD` | Rilevazione del silenzio nell'ascolto (`0` = finestra fissa) | attiva |
| `JARVIS_VAD_SOGLIA` | Sensibilità del VAD (energia RMS): più alta = meno sensibile | `0.015` |
| `JARVIS_UI_PORT` | Porta del front end web locale (Fase 9) | `8765` |
| `JARVIS_LIVELLO` | Livello di ragionamento di partenza (`base`/`normale`/`profondo`) | `base` |
| `JARVIS_MODEL_BASE` | Modello del livello base | `claude-haiku-4-5` |
| `JARVIS_MODEL_NORMALE` | Modello del livello normale | `claude-sonnet-4-6` |
| `JARVIS_MODEL_PROFONDO` | Modello del livello profondo | `claude-opus-4-8` |
| `JARVIS_SCS_HOST` | IP del gateway domotico BTicino/SCS (Fase 11) | — (domotica spenta) |
| `JARVIS_SCS_PORT` | Porta OpenWebNet del gateway | `20000` |
| `JARVIS_SCS_PASSWORD` | Password OPEN numerica del gateway (se richiesta) | — |
| `JARVIS_SCS_PUNTI` | Mappa nomi → indirizzi SCS (`"cucina=12, salotto=25"`) | — |

Tutte le opzionali sono documentate anche in [`.env.example`](.env.example).

## Esempi

Tre interazioni che mostrano sottosistemi diversi. Richiedono una `ANTHROPIC_API_KEY`
valida e rete: **provale in locale** dopo l'avvio (`python main.py`).

**1. Sistema (sola lettura, nessuna conferma)**

```
Tu > che sistema operativo uso e quanto spazio ho libero sul disco?
🔧 uso get_system_info({})
Jarvis > Stai usando Linux … e hai … GB liberi sul disco.
```

**2. File nella sandbox (azione con conferma)**

```
Tu > scrivi un file promemoria.txt nella sandbox con dentro "comprare il latte"
🔧 uso scrivi_file({'percorso': 'promemoria.txt', 'contenuto': 'comprare il latte'})
   → Jarvis chiede conferma perché scrivi_file è CAUTION; digita "s" per accettare
Jarvis > Fatto: ho creato promemoria.txt nella sandbox.
```

**3. Memoria persistente (ricorda + richiama)**

```
Tu > ricorda che tengo i miei progetti in ~/Sviluppo
🔧 uso ricorda({'fatto': 'I progetti dell’utente sono in ~/Sviluppo'})
Jarvis > Segnato.

Tu > dove tengo i progetti?
Jarvis > Nella cartella ~/Sviluppo.
```

*(Il testo esatto delle risposte varia: è il modello a formularle.)* Esiste anche la
**ricerca web** (`web_search`): usa la rete e ha un piccolo costo per ricerca, quindi non
è tra gli esempi di base.

## Voce (opzionale, Fase 6)

Jarvis può funzionare **a voce**: microfono → riconoscimento vocale (STT) → il solito loop
→ sintesi vocale (TTS) → altoparlante. È un **guscio** attorno allo stesso cervello
(`voice.py`): il loop non cambia.

È **opzionale e disattivata di default**, perché richiede dipendenze pesanti e hardware
audio. Il core resta a 3 dipendenze.

L'**ingresso** (microfono → testo) usa sempre `faster-whisper` + `sounddevice`. L'**uscita**
(testo → voce, TTS) ha invece **due motori**, scelti automaticamente o via `JARVIS_TTS`:

- **`say` (macOS)** — il sintetizzatore **integrato** in macOS: nessun binario esterno né
  modello da scaricare, nativo Apple Silicon, zero grattacapi. **È il default sul Mac.**
- **`piper`** — TTS neurale locale multipiattaforma (binario esterno + modello voce `.onnx`).
  Il default fuori da macOS, o se imposti `JARVIS_TTS=piper`.

**Setup comune (STT, sempre):**

```bash
pip install -r requirements-voice.txt
```

**Su macOS (consigliato, con `say`):** non serve altro. Opzionale: scegli una voce italiana.

```bash
# (opzionale) elenca le voci disponibili e installane una italiana da
# Impostazioni di Sistema › Accessibilità › Contenuti pronunciati › Voce di sistema
say -v '?' | grep it_IT           # es. Alice, Luca
export JARVIS_SAY_VOICE=Alice     # senza questa, usa la voce di sistema

python main.py --voce             # oppure:  JARVIS_VOICE=1 python main.py
```

**Con piper (Linux, o macOS se preferisci il TTS neurale):**

```bash
# il programma piper + un modello voce .onnx: vedi https://github.com/rhasspy/piper
#   scarica il modello e il suo file .onnx.json accanto (stesso nome base)
#   su Linux serve anche espeak-ng (es. `sudo apt-get install espeak-ng`)
export JARVIS_PIPER_MODEL=/percorso/a/it_IT-riccardo-x_low.onnx   # percorso ESATTO, .onnx incluso
export JARVIS_TTS=piper          # solo se vuoi forzare piper su un Mac
python main.py --voce
```

Di' **«esci»** per terminare. Se le dipendenze audio mancano, Jarvis te lo dice e
**ripiega automaticamente sulla modalità testo**.

L'ascolto usa una **rilevazione del silenzio** (VAD "a energia"): Jarvis smette di
registrare quando smetti di parlare, invece di una finestra fissa. Se il microfono è
rumoroso e parte da solo (o al contrario non ti sente), regola la sensibilità con
`JARVIS_VAD_SOGLIA` (alzala se è troppo sensibile, abbassala se non ti sente); puoi
tornare alla finestra fissa con `JARVIS_VAD=0`.

In modalità voce Jarvis adatta anche il **registro**: risponde **breve e discorsivo, senza
formattazione** (niente elenchi, grassetti, codice o emoji), perché il testo viene *letto*
ad alta voce. E prima di parlare il testo viene comunque **ripulito** dai simboli residui,
così il sintetizzatore non legge markdown ed emoji. Su macOS, se non imposti
`JARVIS_SAY_VOICE`, Jarvis prova a scegliere da solo una **voce italiana** (meglio se ne hai
installata una "Enhanced"/"Premium", che suona molto più naturale).

> **Stato**: il flusso è testato (mock del ciclo + logica VAD pura) e su macOS funziona
> end-to-end con `say`. Con piper i backend audio vanno **collaudati in locale** (servono
> microfono/altoparlanti e il binario piper). Limite residuo: la conferma delle azioni
> CAUTION/DANGEROUS passa ancora dalla **tastiera** anche in modalità voce.

## Mac, Calendario e Domotica (Fase 11)

Tre famiglie da **assistente personale vero**, tutte a zero dipendenze:

- **Mac** (via `osascript`, di serie su macOS): volume, musica (play/pausa/brani),
  notifiche a schermo e **timer** («timer di 10 minuti per la pasta» — nota onesta: il
  timer vive finché Jarvis è aperto). Al primo uso macOS chiede il permesso
  "Automazione", una tantum.
- **Calendario e Promemoria** (app di sistema del Mac): *«che impegni ho domani?»*,
  *«ricordami di chiamare Luca alle 18»* — i promemoria restano nell'app Promemoria e
  suonano anche a Jarvis spento. Le query al Calendario possono richiedere qualche
  secondo su calendari molto pieni.
- **Domotica BTicino/SCS (MyHome)**: Jarvis parla **OpenWebNet** direttamente col
  gateway del tuo impianto (MyHomeServer1, F454, MH202…) — *«accendi la luce in
  cucina»*, *«tira giù le tapparelle»*, *«è accesa la luce in bagno?»*, *«spegni tutte
  le luci»* (punto `0`). Protocollo testuale su TCP, implementato in pura libreria
  standard: niente Home Assistant, niente servizi in mezzo.

**Setup domotica** (in `.env`):

```bash
JARVIS_SCS_HOST=192.168.1.35            # l'IP del tuo gateway SCS
JARVIS_SCS_PUNTI="cucina=12, salotto=25, tapparella camera=7"   # nomi → indirizzi SCS
# JARVIS_SCS_PASSWORD=12345             # solo se il gateway chiede la password OPEN
```

Consiglio: nel configuratore del gateway, aggiungi l'IP del Mac tra gli **IP abilitati**
— così non serve password. I gateway recenti che impongono l'autenticazione HMAC non
sono ancora supportati (Jarvis te lo dice chiaramente se la incontra). Gli indirizzi
(A/PL) dei punti luce li trovi nel progetto dell'impianto o nell'app MyHome_Up.

## Livelli di ragionamento (Fase 10)

Jarvis non usa sempre lo stesso "cervello": lavora a **tre livelli**, e li cambia **da
solo** valutando la complessità del compito — o quando glielo chiedi tu.

| Livello | Modello (default) | Ragionamento | Quando |
|---|---|---|---|
| `base` *(partenza)* | `claude-haiku-4-5` | — | uso quotidiano: domande, tool semplici |
| `normale` | `claude-sonnet-4-6` | — | compiti di media complessità |
| `profondo` | `claude-opus-4-8` | **adattivo** (il modello decide quanto pensare) + effort alto | analisi complesse, progettazione, codice non banale |

Come funziona:
- Il cambio è un **tool** (`imposta_livello`) che il modello chiama quando serve; quando
  lo fa, **il turno ricomincia da capo al nuovo livello** — così la tua richiesta viene
  affrontata per intero dal cervello giusto, non a metà. Una guardia impedisce cambi di
  livello a raffica.
- **Comandi espliciti**: "ragiona di più", "usa il livello profondo", "torna al livello
  base", "non serve pensare tanto" — Jarvis obbedisce.
- **Trasparenza**: ogni cambio è mostrato (🧠 `livello di ragionamento → profondo (motivo)`)
  e tracciato nel log JSONL. I livelli alti costano di più: Jarvis è istruito a tornare
  a `base` quando il lavoro complesso è concluso.
- I modelli di ogni livello si cambiano con `JARVIS_MODEL_BASE/NORMALE/PROFONDO`; il
  livello di partenza con `JARVIS_LIVELLO`.

## Front end web (Fase 9) — l'HUD stile Iron Man

Jarvis ha anche un'interfaccia **web locale** in stile HUD (arc reactor pulsante, tema
scuro/ciano, log dei comandi): un altro *guscio* attorno allo stesso cervello, come la
voce. **Zero dipendenze nuove**: il server è libreria standard (`http.server` + SSE) e la
pagina è un singolo HTML senza risorse esterne.

```bash
python main.py --ui        # oppure:  python server.py
# si apre il browser su http://127.0.0.1:8765
```

- **Conferme nel browser**: quando Jarvis vuole eseguire un'azione CAUTION/DANGEROUS,
  nella pagina compare un pannello di **autorizzazione** (Approva/Nega) con il tool e gli
  argomenti esatti. Se non rispondi entro 2 minuti, l'azione è **rifiutata** (mai
  silenzio-assenso).
- **Modalità vocale continua**: il 🎙 è un interruttore di *conversazione*, non un
  "registra una volta" — acceso, il ciclo è automatico: parli → Jarvis risponde **a
  voce** → torna in ascolto da solo, finché non lo spegni. (Il 🔊 serve solo a farsi
  leggere le risposte quando scrivi da tastiera.) Web Speech API (Chrome, Safari,
  Edge), senza le dipendenze audio Python.
- **Fonti su richiesta**: quando risponde basandosi sul web, Jarvis cita la fonte e ti
  **chiede** se vuoi vederla; se dici sì, la **apre nel browser** (`apri_url`, protetto
  dallo stesso guardiano anti-SSRF di `leggi_pagina`).
- **Voce UMANA, non robotica**: nell'HUD il modello risponde in **registro parlato**
  (breve, discorsivo, senza formattazione) e alla sintesi arriva una versione **ripulita**
  (mai letti asterischi, simboli, emoji o URL). Il menu a tendina elenca le voci italiane
  del tuo sistema **ordinate per qualità**: per il risultato migliore installa una voce
  "Enhanced/Premium" (macOS: Impostazioni › Accessibilità › Contenuti pronunciati) o usa
  Chrome ("Google italiano"). La scelta viene ricordata.
- **Sicurezza**: il server ascolta **solo su 127.0.0.1** (non è raggiungibile dalla rete)
  e serve un turno per volta. Porta configurabile con `JARVIS_UI_PORT`.

## Verifica rapida (smoke-test)

Per un controllo veloce **senza chiave API e senza rete**:

```bash
python smoke_test.py
```

Verifica *a secco* che l'app si importi, che il registro dei tool sia ben formato e che un
tool `SAFE` giri in isolamento. **Non** sostituisce la prova end-to-end degli
[esempi](#esempi), che richiede la chiave e la rete.

## Limiti noti

- **Voce** (Fase 6): scheletro pronto e testato nel flusso non-audio (vedi
  [Voce](#voce-opzionale-fase-6)); i backend audio reali vanno collaudati in locale
  (servono hardware audio e dipendenze pesanti).
- Il **richiamo** della memoria è per parole chiave, non semantico (non capisce i sinonimi).
- Alcune azioni (processi, screenshot, apertura app) usano gli **strumenti nativi del
  sistema operativo**: se su un dato SO manca lo strumento, Jarvis lo dice con un errore
  chiaro invece di fingere.
- **Costi**: ogni chiamata all'API — e in particolare `web_search` — ha un costo.
