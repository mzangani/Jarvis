"""
tools/system.py — Famiglia di tool "Sistema".

Nato in Fase 2 con un solo strumento a sola lettura (`get_system_info`), qui in
Fase 4b la famiglia cresce con strumenti che agiscono sul sistema operativo VIVO
(non più solo sui file):

  - elenca_processi   -> SAFE      (sola lettura)
  - apri_applicazione -> CAUTION   (avvia un programma)
  - chiudi_processo   -> DANGEROUS (termina un processo)
  - screenshot        -> CAUTION   (cattura lo schermo dentro la sandbox)

Filo conduttore (lo stesso di `_leggi_batteria`): PORTABILITÀ ONESTA. La libreria
standard di Python non sa elencare processi, aprire app o fare screenshot in modo
portabile, e per principio NON aggiungiamo dipendenze (psutil, Pillow, mss...).
Quindi rileviamo il sistema operativo e invochiamo lo STRUMENTO NATIVO già
presente (ps/tasklist, open/start, screencapture/PowerShell/grim...). Se su un
dato SO l'azione non è realizzabile o manca lo strumento, lo diciamo con un
errore chiaro: mai una finta esecuzione.

Ogni tool è una terna (schema, funzione, rischio). La CONFERMA per CAUTION/
DANGEROUS NON è qui: la gestisce il loop in brain.py prima di dispatch.
"""

import datetime
import os
import platform
import shutil
import subprocess
from pathlib import Path

import safety
from safety import SAFE, CAUTION, DANGEROUS


def get_system_info() -> str:
    """Raccoglie alcune informazioni di base sul sistema e le restituisce come testo."""
    # Sistema operativo e versione del kernel/release.
    so = f"{platform.system()} {platform.release()}"

    # Ora locale corrente.
    ora = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Spazio disco della partizione che contiene la cartella home dell'utente.
    uso = shutil.disk_usage(Path.home())
    gb = 1024 ** 3  # byte in un gigabyte
    disco = (
        f"{uso.free / gb:.1f} GB liberi su {uso.total / gb:.1f} GB totali "
        f"({uso.used / gb:.1f} GB usati)"
    )

    batteria = _leggi_batteria()

    return (
        f"Sistema operativo: {so}\n"
        f"Ora locale: {ora}\n"
        f"Disco: {disco}\n"
        f"Batteria: {batteria}"
    )


def _leggi_batteria() -> str:
    """
    Legge la batteria in modo "best-effort".

    Nota di progetto: la libreria standard di Python NON ha un'API portabile per
    la batteria, e per principio (Fase 0) evitiamo dipendenze extra come `psutil`.
    Quindi su Linux leggiamo /sys/class/power_supply; su altri sistemi diciamo
    onestamente che il dato non è disponibile, invece di inventarlo.
    """
    base = Path("/sys/class/power_supply")
    if not base.exists():
        return "informazione non disponibile su questo sistema"

    # Cerca la prima cartella BAT* (BAT0, BAT1, ...).
    for bat in sorted(base.glob("BAT*")):
        capacita = bat / "capacity"
        if capacita.exists():
            perc = capacita.read_text().strip()
            stato_file = bat / "status"
            stato = stato_file.read_text().strip() if stato_file.exists() else "?"
            return f"{perc}% ({stato})"

    return "informazione non disponibile su questo sistema"


# --- Helper di portabilità ---------------------------------------------------
# Piccola comodità: eseguire un comando di sistema catturandone l'output.
# NON usa shell=True (passiamo una LISTA di argomenti): così gli argomenti non
# vengono re-interpretati dalla shell e non c'è rischio di iniezione di comandi.
# Impostiamo sempre un timeout: uno strumento di sistema che si impianta non deve
# bloccare per sempre l'agente.
def _esegui(comando: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    """Esegue `comando` (lista di argomenti) e restituisce il CompletedProcess."""
    return subprocess.run(
        comando,
        capture_output=True,
        text=True,          # stdout/stderr come stringhe, non byte
        timeout=timeout,
    )


# --- elenca_processi (SAFE) --------------------------------------------------

# Quanti processi al massimo riportiamo: come per i file, evitiamo di riversare
# centinaia di righe nel contesto del modello.
_MAX_PROCESSI = 60


def elenca_processi() -> str:
    """
    Elenca i processi attivi usando lo strumento nativo del sistema operativo.

    Portabilità onesta:
      - Windows -> `tasklist`
      - Linux/macOS (Unix) -> `ps -axo pid,comm` (sintassi accettata da entrambi)
      - altri SO -> non sappiamo come farlo: erroriamo, non inventiamo.
    """
    sistema = platform.system()
    if sistema == "Windows":
        comando = ["tasklist"]
    elif sistema in ("Linux", "Darwin"):  # Darwin = macOS
        # -a: processi di tutti gli utenti, -x: anche senza terminale,
        # -o pid,comm: solo PID e nome del comando (output compatto e stabile).
        comando = ["ps", "-axo", "pid,comm"]
    else:
        raise RuntimeError(
            f"Elenco processi non supportato su questo sistema operativo: {sistema}."
        )

    esito = _esegui(comando)
    if esito.returncode != 0:
        # Fail loud: lo strumento di sistema ha fallito, riportiamo il suo errore.
        raise RuntimeError(
            f"Il comando '{' '.join(comando)}' è fallito: {esito.stderr.strip()}"
        )

    righe = esito.stdout.splitlines()
    troncato = len(righe) > _MAX_PROCESSI
    testo = "\n".join(righe[:_MAX_PROCESSI])
    if troncato:
        testo += f"\n[...troncato: mostrati {_MAX_PROCESSI} di {len(righe)} righe...]"
    return testo


# --- apri_applicazione (CAUTION) ---------------------------------------------

def apri_applicazione(nome: str) -> str:
    """
    Apre un'applicazione/programma per nome, con lo strumento nativo del SO.

    Portabilità onesta:
      - macOS   -> `open -a <nome>`
      - Windows -> `start "" <nome>` (tramite cmd)
      - Linux   -> lancia il binario di nome <nome> (verificato con which),
                   in modo NON bloccante e staccato dal terminale.
    Se l'app non esiste, fail loud.
    """
    sistema = platform.system()

    if sistema == "Darwin":
        esito = _esegui(["open", "-a", nome])
        if esito.returncode != 0:
            raise RuntimeError(
                f"Impossibile aprire l'applicazione '{nome}': {esito.stderr.strip()}"
            )
        return f"Applicazione '{nome}' avviata."

    if sistema == "Windows":
        # `start` è un comando interno di cmd, non un eseguibile: va invocato via
        # cmd. Il primo "" è il titolo della finestra (argomento richiesto da start
        # quando il target è tra virgolette).
        esito = _esegui(["cmd", "/c", "start", "", nome])
        if esito.returncode != 0:
            raise RuntimeError(
                f"Impossibile aprire l'applicazione '{nome}': {esito.stderr.strip()}"
            )
        return f"Applicazione '{nome}' avviata."

    if sistema == "Linux":
        # Su Linux non c'è un "apri app per nome" universale: proviamo a lanciare
        # un binario che si chiami come <nome>. shutil.which lo cerca nel PATH:
        # se non c'è, fail loud invece di fingere l'avvio.
        percorso = shutil.which(nome)
        if percorso is None:
            raise RuntimeError(
                f"Applicazione '{nome}' non trovata nel PATH di sistema."
            )
        # Popen (non run): l'app grafica non deve bloccare l'agente. Reindirizziamo
        # gli stream su /dev/null così l'output della GUI non sporca il terminale.
        subprocess.Popen(
            [percorso],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return f"Applicazione '{nome}' avviata ({percorso})."

    raise RuntimeError(
        f"Apertura di applicazioni non supportata su questo sistema operativo: {sistema}."
    )


# --- chiudi_processo (DANGEROUS) ---------------------------------------------

def chiudi_processo(pid: int) -> str:
    """
    Termina il processo con il PID dato, usando il meccanismo nativo del SO.

    - Valida che `pid` sia un intero (il modello potrebbe passarlo come stringa).
    - Unix: os.kill(pid, SIGTERM). Se non esiste -> ProcessLookupError; se non
      abbiamo i permessi -> PermissionError. Li traduciamo in messaggi chiari.
    - Windows: `taskkill /PID <pid> /F` (os.kill non invia SIGTERM arbitrari lì).
    """
    # Validazione: accettiamo int o una stringa che rappresenta un intero.
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        raise ValueError(f"Il PID deve essere un numero intero, ricevuto: {pid!r}")

    sistema = platform.system()

    if sistema == "Windows":
        esito = _esegui(["taskkill", "/PID", str(pid), "/F"])
        if esito.returncode != 0:
            # taskkill scrive il motivo (processo inesistente, accesso negato) su
            # stdout o stderr: riportiamo entrambi per non perdere il dettaglio.
            dettaglio = (esito.stderr.strip() or esito.stdout.strip())
            raise RuntimeError(
                f"Impossibile terminare il processo {pid}: {dettaglio}"
            )
        return f"Processo {pid} terminato."

    # Unix (Linux/macOS): usiamo un segnale POSIX standard.
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # Caso noto e preciso: nessun processo con quel PID. Rilanciamo con un
        # messaggio chiaro (non un except silenzioso: rialziamo un errore parlante).
        raise ProcessLookupError(f"Nessun processo con PID {pid}.")
    except PermissionError:
        raise PermissionError(
            f"Permessi insufficienti per terminare il processo {pid}."
        )
    return f"Segnale di terminazione (SIGTERM) inviato al processo {pid}."


# --- screenshot (CAUTION) ----------------------------------------------------

# Su Linux non esiste un tool di screenshot garantito: proviamo una lista di
# strumenti noti, nell'ordine, e usiamo il primo disponibile. Ognuno ha una CLI
# diversa: la funzione costruisce il comando corretto dato il percorso di output.
def _comando_screenshot_linux(strumento: str, destinazione: str) -> list[str]:
    """Restituisce la riga di comando per catturare lo schermo con `strumento`."""
    if strumento == "grim":              # Wayland
        return ["grim", destinazione]
    if strumento == "scrot":             # X11
        return ["scrot", "--overwrite", destinazione]
    if strumento == "gnome-screenshot":  # GNOME
        return ["gnome-screenshot", "-f", destinazione]
    if strumento == "spectacle":         # KDE
        return ["spectacle", "-b", "-n", "-o", destinazione]
    if strumento == "import":            # ImageMagick
        return ["import", "-window", "root", destinazione]
    raise RuntimeError(f"Strumento screenshot non gestito: {strumento}")


def screenshot(percorso_output: str) -> str:
    """
    Cattura lo schermo e salva l'immagine in `percorso_output`, DENTRO la sandbox.

    ensure_in_sandbox() è la PRIMA riga: il file può finire solo nella cartella
    consentita, mai altrove.

    Portabilità onesta (senza dipendenze extra: solo strumenti nativi):
      - macOS   -> `screencapture <file>`
      - Windows -> scriptlet PowerShell con System.Drawing (nessuna installazione)
      - Linux   -> primo strumento disponibile tra grim/scrot/gnome-screenshot/
                   spectacle/import, e solo se c'è una sessione grafica.
    Se la cattura non è possibile su questo sistema, fail loud: niente file finto.
    """
    p = safety.ensure_in_sandbox(percorso_output)  # <- SEMPRE la prima riga
    # Assicuriamo che la cartella di destinazione esista (come fa scrivi_file).
    p.parent.mkdir(parents=True, exist_ok=True)
    destinazione = str(p)

    sistema = platform.system()

    if sistema == "Darwin":
        esito = _esegui(["screencapture", destinazione], timeout=30)
        if esito.returncode != 0:
            raise RuntimeError(
                f"screencapture è fallito: {esito.stderr.strip()}"
            )

    elif sistema == "Windows":
        # PowerShell è presente di serie su Windows moderni. Lo scriptlet cattura
        # l'intero "virtual screen" (tutti i monitor) e lo salva su file. Non serve
        # installare nulla: usa le librerie .NET già nel sistema.
        script = (
            "$ErrorActionPreference='Stop';"
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
            "$s=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
            "$bmp=New-Object System.Drawing.Bitmap($s.Width,$s.Height);"
            "$g=[System.Drawing.Graphics]::FromImage($bmp);"
            "$g.CopyFromScreen($s.Location,[System.Drawing.Point]::Empty,$s.Size);"
            f"$bmp.Save('{destinazione}');"
        )
        esito = _esegui(
            ["powershell", "-NoProfile", "-Command", script], timeout=30
        )
        if esito.returncode != 0:
            raise RuntimeError(
                f"Cattura schermo via PowerShell fallita: {esito.stderr.strip()}"
            )

    elif sistema == "Linux":
        # Serve una sessione grafica: senza DISPLAY (X11) né WAYLAND_DISPLAY non
        # c'è nessuno schermo da catturare (es. server o container headless).
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            raise RuntimeError(
                "Nessuna sessione grafica attiva (DISPLAY/WAYLAND_DISPLAY non "
                "impostati): impossibile fare uno screenshot su questo sistema."
            )
        # Best-effort: primo strumento disponibile nel PATH, nell'ordine preferito.
        candidati = ["grim", "scrot", "gnome-screenshot", "spectacle", "import"]
        strumento = next((c for c in candidati if shutil.which(c)), None)
        if strumento is None:
            raise RuntimeError(
                "Nessuno strumento di screenshot trovato. Installane uno tra: "
                + ", ".join(candidati) + "."
            )
        esito = _esegui(_comando_screenshot_linux(strumento, destinazione), timeout=30)
        if esito.returncode != 0:
            raise RuntimeError(
                f"Lo strumento '{strumento}' è fallito: {esito.stderr.strip()}"
            )

    else:
        raise RuntimeError(
            f"Screenshot non supportato su questo sistema operativo: {sistema}."
        )

    # Verifica finale: lo strumento ha detto OK ma il file deve esistere davvero.
    # Se non c'è, fail loud invece di dichiarare un successo che non c'è stato.
    if not p.exists():
        raise RuntimeError(
            f"Lo strumento non ha prodotto il file atteso: {p}"
        )
    return f"Screenshot salvato in {p} ({p.stat().st_size} byte)."


# --- Definizione dei tool per il modello -------------------------------------
# Ogni description è scritta PER IL MODELLO: cosa fa, quando usarlo, quando NON
# usarlo (con rimando al tool giusto), e note sul formato degli argomenti.

GET_SYSTEM_INFO = {
    "name": "get_system_info",
    "description": (
        "Restituisce informazioni reali di base sul computer dell'utente: "
        "sistema operativo, ora locale, spazio libero/usato sul disco e stato "
        "della batteria. Usa questo strumento quando l'utente chiede dati sul "
        "proprio sistema (es. 'quanto spazio ho sul disco?', 'che ora è?', "
        "'quanta batteria mi resta?') invece di rispondere a memoria. "
        "Non richiede alcun argomento."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

ELENCA_PROCESSI = {
    "name": "elenca_processi",
    "description": (
        "Elenca i processi attualmente in esecuzione sul computer (PID e nome). "
        "Usalo quando l'utente vuole sapere cosa sta girando o cercare il PID di un "
        "programma (es. 'quali processi sono attivi?', 'qual è il PID di Chrome?'). "
        "NON usarlo per terminare un processo (usa chiudi_processo) né per avviarne "
        "uno (usa apri_applicazione). L'elenco può essere troncato se molto lungo. "
        "Non richiede argomenti."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

APRI_APPLICAZIONE = {
    "name": "apri_applicazione",
    "description": (
        "Avvia un'applicazione/programma già installato, dato il suo nome. Usalo "
        "quando l'utente chiede di aprire o lanciare un programma (es. 'apri il "
        "browser', 'lancia la calcolatrice'). NON usarlo per aprire un FILE dentro "
        "la sandbox (quello è compito della famiglia File) né per terminare un "
        "programma (usa chiudi_processo). Nota: il 'nome' è quello con cui il "
        "sistema conosce l'app (su macOS il nome dell'app, es. 'Safari'; su "
        "Windows/Linux il nome dell'eseguibile, es. 'notepad', 'firefox')."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "nome": {
                "type": "string",
                "description": "Nome dell'applicazione o dell'eseguibile da avviare.",
            }
        },
        "required": ["nome"],
    },
}

CHIUDI_PROCESSO = {
    "name": "chiudi_processo",
    "description": (
        "Termina un processo in esecuzione, dato il suo PID (identificatore numerico). "
        "Usalo quando l'utente vuole chiudere/uccidere un programma di cui conosci il "
        "PID. Se non conosci il PID, prima usa elenca_processi per trovarlo. NON usarlo "
        "per avviare programmi (usa apri_applicazione). Attenzione: terminare un "
        "processo può causare perdita di dati non salvati. Il PID è un numero intero."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pid": {
                "type": "integer",
                "description": "PID (numero intero) del processo da terminare.",
            }
        },
        "required": ["pid"],
    },
}

SCREENSHOT = {
    "name": "screenshot",
    "description": (
        "Cattura un'immagine dello schermo e la salva in un file PNG dentro la "
        "sandbox di Jarvis. Usalo quando l'utente chiede uno screenshot o una foto "
        "dello schermo. NON usarlo per leggere il contenuto di un file immagine "
        "esistente. Il percorso di output è relativo alla sandbox (es. "
        "'schermata.png'); i percorsi fuori dalla sandbox vengono rifiutati. Se il "
        "sistema non ha uno strumento di cattura o non c'è una sessione grafica, "
        "l'operazione fallisce con un messaggio chiaro (nessun file finto)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "percorso_output": {
                "type": "string",
                "description": (
                    "Percorso del file immagine da creare, relativo alla sandbox "
                    "(es. 'schermata.png' o 'foto/desktop.png')."
                ),
            }
        },
        "required": ["percorso_output"],
    },
}


# Elenco dei tool esposti da questo file: terne (schema, funzione, livello di rischio).
# La conferma per CAUTION/DANGEROUS è del loop in brain.py, non di questi tool.
TOOLS = [
    (GET_SYSTEM_INFO, get_system_info, SAFE),
    (ELENCA_PROCESSI, elenca_processi, SAFE),
    (APRI_APPLICAZIONE, apri_applicazione, CAUTION),
    (CHIUDI_PROCESSO, chiudi_processo, DANGEROUS),
    (SCREENSHOT, screenshot, CAUTION),
]
