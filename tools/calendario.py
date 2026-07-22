"""
tools/calendario.py — Famiglia "Calendario": impegni e promemoria del Mac.

Parla con le app di sistema Calendario e Promemoria via osascript (AppleScript),
presente di serie su macOS: zero dipendenze. È la famiglia più "assistente personale"
di tutte: «che impegni ho domani?», «ricordami di chiamare Luca alle 18».

Al primo uso macOS chiede il permesso di automazione su Calendario/Promemoria (una
tantum). Su sistemi non-macOS i tool falliscono LOUD con un messaggio chiaro.

Note oneste:
- Interrogare Calendario via AppleScript può essere LENTO su calendari molto pieni
  (anche diversi secondi): timeout largo e finestre di ricerca brevi.
- `promemoria_aggiungi` è SAFE pur scrivendo nei Promemoria dell'utente: è un'azione
  ADDITIVA, visibile e cancellabile in un tocco, chiesta esplicitamente («ricordami
  di...») — una conferma a tastiera in mezzo a una frase vocale sarebbe solo attrito.
"""

import re
from datetime import datetime

from safety import SAFE
# Riusiamo il ponte osascript della famiglia Mac (stesso guardiano, stesso timeout base).
from .mac import _osascript, _stringa_as

# Interrogare Calendar può essere lento: timeout dedicato, più largo di quello dei
# comandi istantanei (volume, notifiche).
_TIMEOUT_CALENDARIO = 60


def _osascript_lento(script: str) -> str:
    """Come _osascript ma con timeout largo (le query a Calendario sono lente)."""
    import shutil
    import subprocess
    if shutil.which("osascript") is None:
        raise RuntimeError(
            "osascript non trovato: i tool del calendario funzionano solo su macOS."
        )
    proc = subprocess.run(["osascript", "-e", script],
                          capture_output=True, timeout=_TIMEOUT_CALENDARIO)
    if proc.returncode != 0:
        dettaglio = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"osascript è fallito: {dettaglio or '(nessun dettaglio)'}")
    return proc.stdout.decode("utf-8", errors="replace").strip()


# --- impegni ------------------------------------------------------------------
_FINESTRE = {"oggi": (0, 1), "domani": (1, 1), "settimana": (0, 7)}


def impegni(quando: str = "oggi") -> str:
    """Elenca gli impegni del Calendario nella finestra chiesta (oggi/domani/settimana)."""
    quando = (quando or "oggi").strip().lower()
    if quando not in _FINESTRE:
        raise ValueError(f"Finestra sconosciuta: {quando!r} (oggi/domani/settimana).")
    scarto, durata = _FINESTRE[quando]
    # Date calcolate DENTRO AppleScript con soli numeri (niente parsing di date da
    # stringa, che dipende dalla lingua del sistema).
    script = f"""
set d1 to current date
set time of d1 to 0
set d1 to d1 + ({scarto} * days)
set d2 to d1 + ({durata} * days)
set fuori to ""
tell application "Calendar"
  repeat with cal in calendars
    repeat with ev in (every event of cal whose start date ≥ d1 and start date < d2)
      set fuori to fuori & (start date of ev as string) & " | " & (summary of ev) & linefeed
    end repeat
  end repeat
end tell
return fuori
"""
    fuori = _osascript_lento(script)
    if not fuori.strip():
        return f"Nessun impegno in calendario per: {quando}."
    return f"Impegni ({quando}):\n{fuori.strip()}"


# --- promemoria ---------------------------------------------------------------
def promemoria_lista() -> str:
    """Elenca i promemoria NON completati della lista predefinita."""
    script = """
set fuori to ""
tell application "Reminders"
  repeat with r in (reminders of default list whose completed is false)
    set fuori to fuori & "- " & (name of r) & linefeed
  end repeat
end tell
return fuori
"""
    fuori = _osascript_lento(script)
    if not fuori.strip():
        return "Nessun promemoria in sospeso."
    return "Promemoria in sospeso:\n" + fuori.strip()


def promemoria_aggiungi(testo: str, scadenza: str | None = None) -> str:
    """
    Aggiunge un promemoria alla lista predefinita, con scadenza opzionale
    ("YYYY-MM-DD HH:MM"). La data è costruita per COMPONENTI numerici in AppleScript,
    mai da stringa: il parsing di date testuali cambia con la lingua del sistema.
    """
    testo = (testo or "").strip()
    if not testo:
        raise ValueError("Testo del promemoria mancante.")
    proprieta = f"{{name:{_stringa_as(testo)}}}"
    prologo = ""
    if scadenza:
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", scadenza.strip())
        if not m:
            raise ValueError(
                f"Scadenza non valida: {scadenza!r}. Formato: 'YYYY-MM-DD HH:MM'."
            )
        anno, mese, giorno, ora, minuto = (int(x) for x in m.groups())
        # Validazione onesta: datetime solleva su date impossibili (13° mese, 31 aprile…).
        datetime(anno, mese, giorno, ora, minuto)
        prologo = f"""
set dd to current date
set year of dd to {anno}
set month of dd to {mese}
set day of dd to {giorno}
set time of dd to ({ora} * hours + {minuto} * minutes)
"""
        proprieta = f"{{name:{_stringa_as(testo)}, due date:dd}}"
    script = f"""{prologo}
tell application "Reminders"
  tell default list to make new reminder with properties {proprieta}
end tell
return "ok"
"""
    _osascript_lento(script)
    coda = f" (scadenza {scadenza})" if scadenza else ""
    return f"Promemoria aggiunto: {testo}{coda}."


# --- Schemi per il modello ---------------------------------------------------
IMPEGNI = {
    "name": "impegni",
    "description": (
        "Legge gli IMPEGNI dal Calendario del Mac: 'oggi', 'domani' o 'settimana' "
        "(prossimi 7 giorni). Usalo per «che impegni ho domani?», «com'è la mia "
        "settimana?». Può richiedere qualche secondo su calendari molto pieni."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "quando": {"type": "string", "enum": ["oggi", "domani", "settimana"]},
        },
        "required": [],
    },
}

PROMEMORIA_LISTA = {
    "name": "promemoria_lista",
    "description": (
        "Elenca i PROMEMORIA non completati (app Promemoria del Mac, lista predefinita). "
        "Usalo per «cosa devo fare?», «leggimi i promemoria»."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

PROMEMORIA_AGGIUNGI = {
    "name": "promemoria_aggiungi",
    "description": (
        "Aggiunge un PROMEMORIA all'app Promemoria del Mac (lista predefinita), con "
        "scadenza opzionale nel formato 'YYYY-MM-DD HH:MM'. Usalo per «ricordami di "
        "comprare il latte», «promemoria per domani alle 9: chiamare Luca» — se serve "
        "l'ora/data di oggi per calcolare la scadenza, usa prima il tool data_ora. "
        "Diverso da 'ricorda' (memoria interna di Jarvis) e da 'timer' (che vive solo "
        "finché Jarvis è aperto): questo resta nell'app Promemoria e suona anche a "
        "Jarvis spento."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "testo": {"type": "string", "description": "Cosa ricordare."},
            "scadenza": {"type": "string",
                         "description": "Opzionale: 'YYYY-MM-DD HH:MM' (usa data_ora per l'oggi)."},
        },
        "required": ["testo"],
    },
}

TOOLS = [
    (IMPEGNI, impegni, SAFE),
    (PROMEMORIA_LISTA, promemoria_lista, SAFE),
    (PROMEMORIA_AGGIUNGI, promemoria_aggiungi, SAFE),
]
