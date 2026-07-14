"""
logger.py — Il tracciamento (logging) delle tool call di Jarvis (Fase 7a).

Sta qui, in cima al progetto, perché è INFRASTRUTTURA come safety.py / memory.py /
history.py: brain.py ci si appoggia, ma questo file non conosce l'API Anthropic né i
tool. Sa solo scrivere una riga su un file.

COSA FA: registra OGNI chiamata dei NOSTRI tool (quelli che passano dal dispatch) su un
file JSONL — una riga JSON per chiamata — con input, output (troncato), durata, esito e
rischio. Serve per OSSERVABILITÀ: poter vedere DOPO cosa ha fatto Jarvis, quanto ci ha
messo e cosa è andato storto.

Perché JSONL (una riga = un evento, in APPEND) e non un log testuale né un unico grande
JSON: ogni riga è un oggetto strutturato, filtrabile e parsabile da sola; si aggiunge in
append (nessuna riscrittura dell'intero file) e un crash a metà rovina al più l'ultima
riga, non tutte. Un array JSON `[ {...}, ... ]` andrebbe invece riscritto per intero e
resterebbe corrotto/non parsabile se il processo muore a metà scrittura.

LOGGING != CANCELLI DI SICUREZZA: confirm()/precheck() stanno PRIMA dell'azione e possono
BLOCCARLA; il logging sta ATTORNO all'azione, non decide nulla e NON blocca mai. Per
questo un errore del logger si ASSORBE (avviso una volta, poi si prosegue), mentre un
errore di un cancello deve fermare l'azione.
"""

import json
import os
from datetime import datetime
from pathlib import Path

from rich.console import Console

console = Console()


# --- Parametri ---------------------------------------------------------------
# Tetto ai caratteri di un singolo output loggato: un tool_result può essere enorme (una
# pagina web, un elenco di file) e non vogliamo gonfiare il file di log. Più generoso dei
# 600 char del riassunto (history.py) perché qui lo scopo è il debug, non il contesto.
_MAX_CHAR_OUTPUT = 2000

# Stesso tetto per i singoli valori-stringa dentro l'input (es. il contenuto passato a
# scrivi_file può essere lungo): teniamo l'input strutturato ma limitato.
_MAX_CHAR_INPUT_VALUE = 2000


# --- Stato del modulo --------------------------------------------------------
# Se la scrittura del log fallisce (file non scrivibile, disco pieno, ...) avvisiamo UNA
# sola volta e poi restiamo silenziosi: il logging non deve mai far cadere Jarvis (è
# osservabilità, non un cancello). Questo flag ricorda che l'avviso è già stato dato, per
# non ripeterlo a ogni tool call. NB: continuiamo comunque a RIPROVARE a scrivere a ogni
# chiamata, così se il file torna scrivibile il logging riparte da solo.
_avviso_dato = False


def _percorso_log() -> Path:
    """
    Percorso del file JSONL di log.

    Configurabile con la variabile d'ambiente JARVIS_LOG (stesso stile di JARVIS_MEMORY
    in memory.py); default: ~/Jarvis-Sandbox/jarvis.jsonl.
    """
    default = Path.home() / "Jarvis-Sandbox" / "jarvis.jsonl"
    return Path(os.environ.get("JARVIS_LOG", default)).expanduser()


def _tronca(testo: str, limite: int) -> str:
    """Accorcia `testo` a `limite` caratteri, segnalando il taglio con un marcatore."""
    if len(testo) <= limite:
        return testo
    return testo[:limite] + " […]"


def _input_troncato(tool_input: dict) -> dict:
    """
    Restituisce una copia dell'input con i valori-stringa lunghi accorciati.

    Manteniamo l'input come OGGETTO JSON (chiavi/valori), così nel log resta
    interrogabile; tronchiamo solo le singole stringhe lunghe (es. il contenuto di
    scrivi_file). I valori non-stringa (numeri, bool, liste) restano come sono: di norma
    sono piccoli.
    """
    if not isinstance(tool_input, dict):
        # Difensivo: l'input dei tool è sempre un dict, ma non vogliamo esplodere qui.
        return {"valore": _tronca(str(tool_input), _MAX_CHAR_INPUT_VALUE)}
    ridotto = {}
    for chiave, valore in tool_input.items():
        ridotto[chiave] = _tronca(valore, _MAX_CHAR_INPUT_VALUE) if isinstance(valore, str) else valore
    return ridotto


def log_tool_call(
    *,
    tool: str,
    tool_input: dict,
    output: str,
    esito: str,
    is_error: bool,
    durata_ms: float,
    rischio: str,
) -> None:
    """
    Aggiunge UNA riga JSON al file di log per una chiamata di tool.

    Argomenti solo-nominali (keyword-only) per rendere leggibile il sito di chiamata in
    brain.py. `esito` è uno tra "ok" / "errore" / "rifiutato". `durata_ms` è il tempo di
    ESECUZIONE del tool (0 per i rifiuti: il tool non è mai partito).

    ROBUSTEZZA (decisione 4): questa funzione non deve MAI far cadere Jarvis. Se il file
    non è scrivibile, avvisiamo LOUD una sola volta e proseguiamo senza loggare.
    """
    global _avviso_dato

    riga = {
        "quando": datetime.now().isoformat(timespec="seconds"),  # ISO, come i timestamp di memory.py
        "tool": tool,
        "input": _input_troncato(tool_input),
        "output": _tronca(output or "", _MAX_CHAR_OUTPUT),
        "is_error": is_error,
        "esito": esito,
        "durata_ms": round(durata_ms, 1),
        "rischio": rischio,
    }

    try:
        percorso = _percorso_log()
        # Creiamo la cartella contenitore se manca (può sollevare OSError: gestito sotto).
        percorso.parent.mkdir(parents=True, exist_ok=True)
        # Apertura in APPEND ("a"): ogni chiamata aggiunge una riga in coda, senza
        # riscrivere il resto (è il senso del JSONL). ensure_ascii=False conserva gli
        # accenti; default=str è una cintura: se un valore imprevisto non fosse
        # serializzabile, lo convertiamo a stringa invece di sollevare TypeError.
        with open(percorso, "a", encoding="utf-8") as f:
            f.write(json.dumps(riga, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        # except MIRATO e motivato: il logging è osservabilità, non un cancello. Se il
        # file non è scrivibile NON facciamo crashare l'assistente: avvisiamo una sola
        # volta (loud) e proseguiamo senza loggare. Ripetere l'avviso a ogni tool call
        # sarebbe solo rumore; il prossimo tentativo riparte comunque dal try qui sopra.
        if not _avviso_dato:
            console.print(
                f"[yellow]⚠  Log non scrivibile ({e}); proseguo senza tracciare le "
                "tool call su file.[/]"
            )
            _avviso_dato = True
