"""
tools/system.py — Famiglia di tool "Sistema".

In Fase 2 contiene un solo strumento, volutamente banale: `get_system_info`.
Serve a vedere il loop agentico in azione con un tool a sola lettura e senza rischi.
Gli altri tool di sistema (processi, screenshot, ...) arriveranno in Fase 4.

Ogni tool è una coppia (schema, funzione):
  - schema: descrive il tool AL MODELLO (nome, descrizione, JSON Schema degli argomenti)
  - funzione: l'implementazione Python che esegue davvero l'azione
"""

import datetime
import platform
import shutil
from pathlib import Path

from safety import SAFE


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


# --- Definizione del tool per il modello -----------------------------------

# La 'description' è scritta PER IL MODELLO: gli spiega quando usare il tool.
# (In Fase 4 vedremo perché una descrizione scritta male è la causa n.1 di
#  agenti che sbagliano strumento.)
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

# Elenco dei tool esposti da questo file: terne (schema, funzione, livello di rischio).
# get_system_info è a sola lettura -> SAFE: non richiederà conferma.
TOOLS = [
    (GET_SYSTEM_INFO, get_system_info, SAFE),
]
