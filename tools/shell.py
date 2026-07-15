"""
tools/shell.py — Famiglia di tool "Shell".

È lo strumento più potente e più pericoloso di Jarvis: esegue un comando di
shell ARBITRARIO sul sistema dell'utente e ne restituisce l'output reale. Per
questo la sua sicurezza è "a cancelli multipli":

  1. check_blacklist()  -> PRIMA riga del tool: i pattern sempre vietati
     (rm -rf /, sudo, mkfs, fork bomb, shutdown...) vengono respinti subito,
     e il comando non viene MAI eseguito.
  2. conferma DANGEROUS -> gestita dal loop in brain.py (safety.confirm()):
     l'utente vede il comando ESATTO e deve dire sì. NON la duplichiamo qui.
  3. output onesto      -> catturiamo stdout, stderr E return code, e li
     restituiamo così come sono. Se il comando fallisce (return code ≠ 0) NON
     fingiamo successo: l'errore è ben visibile.

Scelte di progetto discusse e decise insieme:
  - shell=True: eseguiamo la stringa con la VERA shell del sistema (/bin/sh su
    Unix, cmd su Windows). Così funzionano pipe, redirezioni, &&, glob e
    variabili — è ciò che ci si aspetta da "esegui un comando". È anche la
    "superficie pericolosa" che la blacklist e la conferma sono nate per coprire.
  - CWD = sandbox: i comandi partono dalla cartella sandbox, per coerenza con la
    famiglia File. ATTENZIONE: il CWD NON è un confine di sicurezza (un comando
    può sempre usare percorsi assoluti); a confinare sono blacklist + conferma.

Argini pratici: un timeout (un comando che si impianta non deve bloccare
l'agente per sempre) e un tetto sull'output (un comando logorroico non deve
intasare il contesto del modello), sulla falsariga di _MAX_BYTES in files.py.
"""

import subprocess

import safety
from safety import DANGEROUS

# Tempo massimo concesso a un comando prima di interromperlo. Un po' più alto
# del default di system.py (15s) perché un comando shell può fare di più.
_TIMEOUT = 20  # secondi

# Tetto sull'output rimandato al modello. In text mode subprocess ci dà delle
# stringhe, quindi qui contiamo CARATTERI (non byte come il _MAX_BYTES di
# files.py, che leggeva bytes): l'idea di "non riversare output enorme" è la stessa.
_MAX_OUTPUT = 100_000


def _tronca(testo: str, nome_stream: str) -> str:
    """Taglia `testo` a _MAX_OUTPUT caratteri, con un marcatore EVIDENTE se troncato."""
    if len(testo) <= _MAX_OUTPUT:
        return testo
    return (
        testo[:_MAX_OUTPUT]
        + f"\n[...troncato: mostrati i primi {_MAX_OUTPUT} caratteri di "
        + f"{len(testo)} ({nome_stream})...]"
    )


def esegui_comando(comando: str) -> str:
    """
    Esegue `comando` con la shell di sistema e restituisce output + return code.

    Ordine dei cancelli:
      1. blacklist (qui sotto, prima riga): se il comando è vietato, solleviamo
         PermissionError e NON eseguiamo nulla.
      2. la conferma DANGEROUS è già stata chiesta dal loop in brain.py prima di
         arrivare qui: non va ripetuta.
    """
    # --- CANCELLO 1: blacklist come PRIMISSIMA cosa -------------------------
    # Solleva PermissionError se il comando contiene un pattern sempre vietato.
    # brain.py cattura l'eccezione e la rimanda al modello come errore (loud).
    safety.check_blacklist(comando)

    # Validazione minima: un comando vuoto non ha senso e non lo passiamo alla shell.
    if not comando or not comando.strip():
        raise ValueError("Il comando è vuoto: non c'è niente da eseguire.")

    # CWD = sandbox. sandbox_root() crea la cartella se non esiste ancora.
    cwd = safety.sandbox_root()

    # --- Esecuzione --------------------------------------------------------
    # shell=True: la stringa va a /bin/sh (Unix) o cmd (Windows), scelti in
    # automatico da subprocess in base al sistema operativo. È qui che pipe,
    # redirezioni e && prendono vita — e proprio per questo il comando è passato
    # dalla blacklist e dalla conferma prima di arrivare fin qui.
    # errors="replace": se l'output non è UTF-8 pulito, i caratteri illeggibili
    # diventano '�' invece di far esplodere il tool (come fa leggi_file coi file).
    try:
        esito = subprocess.run(
            comando,
            shell=True,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_TIMEOUT,
            cwd=str(cwd),
        )
    except subprocess.TimeoutExpired:
        # Caso noto e preciso: il comando ha sforato il tempo massimo. Non è un
        # except silenzioso — rialziamo un errore parlante che il modello vedrà.
        raise RuntimeError(
            f"Il comando ha superato il tempo massimo di {_TIMEOUT} secondi ed è "
            f"stato interrotto: «{comando}»."
        )

    # --- Output onesto -----------------------------------------------------
    # Ricomponiamo un resoconto che dice SEMPRE com'è andata: esito, return code,
    # e ciò che il comando ha scritto su stdout e stderr (ciascuno troncato se enorme).
    rc = esito.returncode
    stdout = _tronca(esito.stdout or "", "stdout")
    stderr = _tronca(esito.stderr or "", "stderr")

    # Prima riga = verdetto inequivocabile. Un return code ≠ 0 NON è un finto
    # successo: lo dichiariamo ERRORE, così il modello non può fraintenderlo.
    esito_str = "OK" if rc == 0 else "ERRORE"
    parti = [f"Comando terminato: {esito_str} (return code {rc})."]

    if stdout.strip():
        parti.append(f"--- stdout ---\n{stdout}")
    if stderr.strip():
        parti.append(f"--- stderr ---\n{stderr}")
    # Alcuni comandi riusciti non stampano nulla (es. una redirezione su file):
    # diciamolo esplicitamente invece di restituire un output vuoto e ambiguo.
    if not stdout.strip() and not stderr.strip():
        parti.append("(nessun output su stdout/stderr)")

    # Nota di progetto: NON solleviamo un'eccezione quando rc ≠ 0. Per una shell
    # un'uscita diversa da zero è informazione normale (es. `grep` senza match
    # ritorna 1): il tool ha funzionato, è il comando ad aver riportato un esito.
    # L'errore è comunque LOUD perché rc e stderr sono qui sotto gli occhi del modello.
    return "\n".join(parti)


# --- Schema per il modello ---------------------------------------------------
# Description scritta PER IL MODELLO: cosa fa, quando usarlo, quando NON usarlo
# (con rimando al tool giusto), e note sul formato dell'argomento.

ESEGUI_COMANDO = {
    "name": "esegui_comando",
    "description": (
        "Esegue un comando di shell sul computer dell'utente e restituisce "
        "l'output REALE: stdout, stderr e il return code. Supporta la sintassi "
        "completa della shell (pipe |, redirezioni >, concatenazioni &&, glob *, "
        "variabili). Usalo quando serve un'operazione da riga di comando non "
        "coperta dagli altri tool (es. 'date', 'git status', 'df -h', 'wc -l file'). "
        "NON usarlo quando esiste un tool dedicato, che è più sicuro e mirato: per "
        "leggere/scrivere/spostare/cercare file usa la famiglia File; per aprire "
        "un'app usa apri_applicazione; per elencare o terminare processi usa "
        "elenca_processi/chiudi_processo; per informazioni di base sul sistema usa "
        "get_system_info. Attenzione: è un'azione pericolosa che richiede conferma "
        "dell'utente, e alcuni comandi distruttivi (es. rm -rf /, sudo) sono sempre "
        "rifiutati. L'argomento 'comando' è la riga di comando completa, come la "
        "scriveresti nel terminale; i comandi girano nella cartella sandbox di Jarvis."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "comando": {
                "type": "string",
                "description": (
                    "La riga di comando completa da eseguire (una stringa), es. "
                    "'date' oppure 'ls -la | wc -l'."
                ),
            }
        },
        "required": ["comando"],
    },
}


# Terna (schema, funzione, rischio). La shell è DANGEROUS: la conferma è del
# loop in brain.py, non di questo file.
TOOLS = [
    (ESEGUI_COMANDO, esegui_comando, DANGEROUS),
]
