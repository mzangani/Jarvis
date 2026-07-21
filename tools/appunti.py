"""
tools/appunti.py — Famiglia di tool "Appunti" (clipboard di sistema + note veloci).

Due capacità vicine ma distinte:

  - CLIPBOARD di sistema: leggere e scrivere ciò che l'utente ha "copiato". È il ponte
    con il resto del sistema operativo ("copia questo", "cosa ho negli appunti?"). Non
    esiste una clipboard nella libreria standard: la raggiungiamo tramite i programmi
    del sistema (pbcopy/pbpaste su macOS, wl-copy/wl-paste o xclip/xsel su Linux), scelti
    a runtime. Se non ce n'è nessuno, falliamo LOUD con un messaggio chiaro.

  - NOTE veloci: un blocco-appunti append-only in un file di testo dentro la sandbox
    (come 'ricorda' scrive solo sul NOSTRO file, non tocca i file dell'utente). Serve a
    buttar giù al volo un pensiero ("segnati che...") e a rileggerlo dopo.

Rischi (discussi):
  - leggi_clipboard, elenca_note                -> SAFE (sola lettura).
  - scrivi_clipboard                            -> SAFE. Sovrascrive solo un buffer di
    appunti volatile, azione attesa quando l'utente dice "copia questo": chiedere conferma
    ogni volta (peggio ancora a voce) darebbe solo fastidio, senza un vero rischio.
  - aggiungi_nota                               -> SAFE. Scrive SOLO sul nostro file di
    note (append), come 'ricorda' col suo DB: nessun effetto sui file dell'utente.

Nota onesta (privacy): leggi_clipboard fa entrare nel contesto del modello ciò che hai
copiato (che potrebbe essere una password). È un'azione che fai su richiesta esplicita:
usala sapendolo.
"""

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import safety
from safety import SAFE

# Tetto ai caratteri restituiti (clipboard o note): non riversare tutto nel contesto.
_MAX = 100_000
# Timeout dei programmini di clipboard: se si impiantano non devono bloccare l'agente.
_TIMEOUT = 5


# --- CLIPBOARD ---------------------------------------------------------------
# Ogni voce: (nome_programma, argv_per_LEGGERE, argv_per_SCRIVERE). Proviamo in
# quest'ordine e usiamo il primo presente. pbpaste/pbcopy sono di macOS; gli altri
# coprono Linux (Wayland con wl-*, X11 con xclip/xsel).
_CLIP_LEGGI = [
    ("pbpaste", ["pbpaste"]),
    ("wl-paste", ["wl-paste", "--no-newline"]),
    ("xclip", ["xclip", "-selection", "clipboard", "-o"]),
    ("xsel", ["xsel", "--clipboard", "--output"]),
]
_CLIP_SCRIVI = [
    ("pbcopy", ["pbcopy"]),
    ("wl-copy", ["wl-copy"]),
    ("xclip", ["xclip", "-selection", "clipboard"]),
    ("xsel", ["xsel", "--clipboard", "--input"]),
]


def _scegli(tabella):
    """Ritorna l'argv del primo programma di clipboard disponibile, o None."""
    for nome, argv in tabella:
        if shutil.which(nome):
            return argv
    return None


def _err_no_clipboard(azione: str) -> RuntimeError:
    return RuntimeError(
        f"Non trovo un programma per {azione} la clipboard. Su macOS dovrebbe esserci "
        "pbcopy/pbpaste (di serie); su Linux installa 'xclip' o 'xsel' (X11) oppure "
        "'wl-clipboard' (Wayland)."
    )


def leggi_clipboard() -> str:
    """Restituisce il testo attualmente nella clipboard di sistema."""
    argv = _scegli(_CLIP_LEGGI)
    if argv is None:
        raise _err_no_clipboard("leggere")
    proc = subprocess.run(argv, capture_output=True, timeout=_TIMEOUT)
    if proc.returncode != 0:
        dettaglio = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Lettura della clipboard non riuscita: {dettaglio or '(errore sconosciuto)'}")
    testo = proc.stdout.decode("utf-8", errors="replace")
    if not testo.strip():
        return "La clipboard è vuota."
    if len(testo) > _MAX:
        return testo[:_MAX] + f"\n\n[...troncato ai primi {_MAX} caratteri...]"
    return testo


def scrivi_clipboard(testo: str) -> str:
    """Mette `testo` nella clipboard di sistema (l'utente potrà incollarlo altrove)."""
    argv = _scegli(_CLIP_SCRIVI)
    if argv is None:
        raise _err_no_clipboard("scrivere")
    proc = subprocess.run(argv, input=(testo or "").encode("utf-8"),
                          capture_output=True, timeout=_TIMEOUT)
    if proc.returncode != 0:
        dettaglio = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Scrittura nella clipboard non riuscita: {dettaglio or '(errore sconosciuto)'}")
    return f"Copiato nella clipboard ({len(testo or '')} caratteri)."


# --- NOTE veloci -------------------------------------------------------------
def _file_note() -> Path:
    """
    Percorso del file delle note. Default: 'note.txt' nella sandbox; configurabile con
    JARVIS_NOTES. Passa comunque da ensure_in_sandbox: le note restano confinate come
    ogni altro file, e un JARVIS_NOTES che punta fuori dalla sandbox viene rifiutato.
    """
    grezzo = os.environ.get("JARVIS_NOTES", "note.txt")
    return safety.ensure_in_sandbox(grezzo)


def aggiungi_nota(testo: str) -> str:
    """Aggiunge una nota (con data e ora) in coda al file delle note."""
    testo = (testo or "").strip()
    if not testo:
        raise ValueError("Nota vuota: non c'è niente da annotare.")
    p = _file_note()
    p.parent.mkdir(parents=True, exist_ok=True)
    quando = datetime.now().strftime("%Y-%m-%d %H:%M")
    with p.open("a", encoding="utf-8") as f:
        f.write(f"[{quando}] {testo}\n")
    return f"Nota aggiunta ({quando})."


def elenca_note() -> str:
    """Restituisce tutte le note salvate finora (le più recenti in fondo)."""
    p = _file_note()
    if not p.exists():
        return "Non hai ancora nessuna nota."
    testo = p.read_text(encoding="utf-8", errors="replace").strip()
    if not testo:
        return "Non hai ancora nessuna nota."
    if len(testo) > _MAX:
        # Teniamo la CODA (le note recenti sono in fondo), non la testa.
        testo = f"[...note più vecchie omesse...]\n" + testo[-_MAX:]
    return testo


# --- Schemi per il modello ---------------------------------------------------
LEGGI_CLIPBOARD = {
    "name": "leggi_clipboard",
    "description": (
        "Legge e restituisce il testo attualmente negli appunti di sistema (la clipboard, "
        "cioè l'ultima cosa che l'utente ha 'copiato'). Usalo per domande come 'cosa ho "
        "copiato?' o quando l'utente vuole che tu lavori sul testo che ha appena copiato. "
        "Restituisce solo TESTO. È un'azione locale di sola lettura."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

SCRIVI_CLIPBOARD = {
    "name": "scrivi_clipboard",
    "description": (
        "Mette del testo negli appunti di sistema (la clipboard), così l'utente potrà "
        "incollarlo dove vuole con Cmd/Ctrl+V. Usalo quando l'utente dice 'copia questo', "
        "'mettilo negli appunti' o simili. Sovrascrive il contenuto precedente della "
        "clipboard."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "testo": {"type": "string", "description": "Il testo da copiare nella clipboard."}
        },
        "required": ["testo"],
    },
}

AGGIUNGI_NOTA = {
    "name": "aggiungi_nota",
    "description": (
        "Aggiunge una nota veloce (con data e ora) a un blocco-appunti personale, salvato "
        "in modo persistente in un file. Usalo quando l'utente vuole 'segnarsi' o 'annotare' "
        "un pensiero, un'idea, una cosa da fare estemporanea. Diverso da 'ricorda': 'ricorda' "
        "è per FATTI durevoli sull'utente (iniettati nel contesto); 'aggiungi_nota' è un "
        "quaderno di appunti liberi che si consulta con 'elenca_note'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "testo": {"type": "string", "description": "Il testo della nota da annotare."}
        },
        "required": ["testo"],
    },
}

ELENCA_NOTE = {
    "name": "elenca_note",
    "description": (
        "Restituisce tutte le note veloci salvate finora con 'aggiungi_nota', in ordine "
        "cronologico (le più recenti in fondo). Usalo quando l'utente chiede 'cosa mi ero "
        "segnato?', 'leggimi le note', 'che appunti ho?'."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}


# Terne (schema, funzione, rischio). Tutte SAFE: nessuna passa da safety.confirm().
TOOLS = [
    (LEGGI_CLIPBOARD, leggi_clipboard, SAFE),
    (SCRIVI_CLIPBOARD, scrivi_clipboard, SAFE),
    (AGGIUNGI_NOTA, aggiungi_nota, SAFE),
    (ELENCA_NOTE, elenca_note, SAFE),
]
