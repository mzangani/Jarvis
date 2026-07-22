"""
tools/mac.py — Famiglia "Mac": controllo del computer via osascript (AppleScript).

Volume, musica, notifiche a schermo e timer: i comandi "da maggiordomo" perfetti per
la modalità vocale («abbassa il volume», «metti in pausa», «timer di dieci minuti»).

Tutto passa da `osascript`, il ponte da riga di comando verso AppleScript, PRESENTE DI
SERIE su macOS: zero dipendenze. Su altri sistemi i tool falliscono LOUD con un
messaggio chiaro (stesso pattern della clipboard). Al primo uso macOS può chiedere il
permesso "Automazione" (una tantum): è normale.

Rischi: tutti SAFE — azioni locali, reversibili e senza effetti su file o dati
(alzare il volume, mettere in pausa, mostrare una notifica, avviare un timer).
"""

import shutil
import subprocess
import threading
from datetime import datetime, timedelta

from safety import SAFE

_TIMEOUT = 15  # osascript non deve mai bloccare l'agente


def _osascript(script: str) -> str:
    """Esegue uno script AppleScript e ritorna stdout. Fail loud se non siamo su macOS."""
    if shutil.which("osascript") is None:
        raise RuntimeError(
            "osascript non trovato: i tool della famiglia Mac funzionano solo su macOS."
        )
    proc = subprocess.run(["osascript", "-e", script],
                          capture_output=True, timeout=_TIMEOUT)
    if proc.returncode != 0:
        dettaglio = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"osascript è fallito: {dettaglio or '(nessun dettaglio)'}")
    return proc.stdout.decode("utf-8", errors="replace").strip()


def _stringa_as(testo: str) -> str:
    """Codifica un testo come stringa AppleScript (escape di backslash e doppi apici)."""
    return '"' + (testo or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


# --- volume -------------------------------------------------------------------
def volume(azione: str, livello: int | None = None) -> str:
    """Regola il volume di sistema: imposta/su/giu/muto/smuto/leggi."""
    azione = (azione or "").strip().lower()
    if azione == "leggi":
        v = _osascript("output volume of (get volume settings)")
        return f"Il volume è al {v}%."
    if azione == "muto":
        _osascript("set volume output muted true")
        return "Audio silenziato."
    if azione == "smuto":
        _osascript("set volume output muted false")
        return "Audio riattivato."
    if azione == "imposta":
        if livello is None or not (0 <= int(livello) <= 100):
            raise ValueError("Per 'imposta' serve un livello tra 0 e 100.")
        _osascript(f"set volume output volume {int(livello)}")
        return f"Volume impostato al {int(livello)}%."
    if azione in ("su", "giu"):
        attuale = int(_osascript("output volume of (get volume settings)"))
        passo = 10 if livello is None else max(1, min(100, int(livello)))
        nuovo = max(0, min(100, attuale + passo if azione == "su" else attuale - passo))
        _osascript(f"set volume output volume {nuovo}")
        return f"Volume: {attuale}% → {nuovo}%."
    raise ValueError(f"Azione volume sconosciuta: {azione!r} (imposta/su/giu/muto/smuto/leggi).")


# --- musica -------------------------------------------------------------------
def musica(azione: str) -> str:
    """Controlla l'app Musica: riproduci/pausa/successivo/precedente/brano."""
    azione = (azione or "").strip().lower()
    comandi = {
        "riproduci": "play", "pausa": "pause",
        "successivo": "next track", "precedente": "previous track",
    }
    if azione in comandi:
        _osascript(f'tell application "Music" to {comandi[azione]}')
        return f"Musica: {azione}."
    if azione == "brano":
        try:
            fuori = _osascript(
                'tell application "Music" to name of current track & " — " & '
                "artist of current track"
            )
        except RuntimeError:
            return "Nessun brano in riproduzione."
        return f"In riproduzione: {fuori}"
    raise ValueError(
        f"Azione musica sconosciuta: {azione!r} (riproduci/pausa/successivo/precedente/brano)."
    )


# --- notifiche ----------------------------------------------------------------
def notifica(testo: str, titolo: str = "Jarvis") -> str:
    """Mostra una notifica di sistema sul Mac."""
    testo = (testo or "").strip()
    if not testo:
        raise ValueError("Testo della notifica mancante.")
    _osascript(
        f"display notification {_stringa_as(testo)} "
        f"with title {_stringa_as(titolo)} sound name \"Glass\""
    )
    return "Notifica mostrata."


# --- timer --------------------------------------------------------------------
# I timer vivono nel processo di Jarvis: se chiudi Jarvis, i timer si perdono
# (thread daemon). È un limite onesto, dichiarato nella description del tool.
_timer_attivi: list[dict] = []


def timer(minuti: float, messaggio: str = "Timer scaduto") -> str:
    """Avvia un timer: allo scadere mostra una notifica (con suono) sul Mac."""
    if shutil.which("osascript") is None:
        # Controllo SUBITO: scoprirlo allo scadere (in un thread) sarebbe silenzioso.
        raise RuntimeError(
            "osascript non trovato: i timer con notifica funzionano solo su macOS."
        )
    minuti = float(minuti)
    if not (0 < minuti <= 24 * 60):
        raise ValueError("I minuti devono essere tra 0 (escluso) e 1440 (24 ore).")
    scadenza = datetime.now() + timedelta(minutes=minuti)
    voce = {"scade": scadenza, "messaggio": messaggio}

    def _scatta() -> None:
        try:
            notifica(messaggio, titolo="⏱ Timer di Jarvis")
        except RuntimeError:
            pass  # niente notifica possibile: il timer muore in silenzio, già dichiarato
        if voce in _timer_attivi:
            _timer_attivi.remove(voce)

    t = threading.Timer(minuti * 60, _scatta)
    t.daemon = True  # non tiene in vita il processo: chiuso Jarvis, timer perso (documentato)
    t.start()
    _timer_attivi.append(voce)
    return f"Timer di {minuti:g} minuti avviato (scade alle {scadenza:%H:%M})."


def timer_attivi() -> str:
    """Elenca i timer in corso."""
    if not _timer_attivi:
        return "Nessun timer attivo."
    righe = [f"- alle {v['scade']:%H:%M}: {v['messaggio']}" for v in _timer_attivi]
    return "Timer attivi:\n" + "\n".join(righe)


# --- Schemi per il modello ---------------------------------------------------
VOLUME = {
    "name": "volume",
    "description": (
        "Regola il VOLUME di sistema del Mac. Azioni: 'imposta' (con livello 0-100), "
        "'su'/'giu' (di 10, o del livello indicato), 'muto', 'smuto', 'leggi'. Usalo per "
        "«abbassa il volume», «volume al 30%», «silenzia»."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "azione": {"type": "string", "enum": ["imposta", "su", "giu", "muto", "smuto", "leggi"]},
            "livello": {"type": "integer", "description": "0-100 per 'imposta'; passo per su/giu."},
        },
        "required": ["azione"],
    },
}

MUSICA = {
    "name": "musica",
    "description": (
        "Controlla l'app Musica del Mac: 'riproduci', 'pausa', 'successivo', 'precedente', "
        "'brano' (dice cosa è in riproduzione). Usalo per «metti la musica», «pausa», "
        "«salta questa canzone», «che canzone è?»."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "azione": {"type": "string",
                       "enum": ["riproduci", "pausa", "successivo", "precedente", "brano"]},
        },
        "required": ["azione"],
    },
}

NOTIFICA = {
    "name": "notifica",
    "description": (
        "Mostra una NOTIFICA di sistema sul Mac (banner con suono). Usala per avvisi "
        "visivi immediati richiesti dall'utente."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "testo": {"type": "string", "description": "Il testo della notifica."},
            "titolo": {"type": "string", "description": "Titolo (default: Jarvis)."},
        },
        "required": ["testo"],
    },
}

TIMER = {
    "name": "timer",
    "description": (
        "Avvia un TIMER: allo scadere mostra una notifica con suono sul Mac. Usalo per "
        "«timer di 10 minuti», «avvisami tra un quarto d'ora». Limite onesto: il timer "
        "vive finché Jarvis è aperto (se chiudi Jarvis, il timer si perde). Per promemoria "
        "che devono sopravvivere usa promemoria_aggiungi."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "minuti": {"type": "number", "description": "Durata in minuti (anche frazioni)."},
            "messaggio": {"type": "string", "description": "Cosa dire allo scadere."},
        },
        "required": ["minuti"],
    },
}

TIMER_ATTIVI = {
    "name": "timer_attivi",
    "description": "Elenca i timer di Jarvis attualmente in corso, con l'ora di scadenza.",
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

TOOLS = [
    (VOLUME, volume, SAFE),
    (MUSICA, musica, SAFE),
    (NOTIFICA, notifica, SAFE),
    (TIMER, timer, SAFE),
    (TIMER_ATTIVI, timer_attivi, SAFE),
]
