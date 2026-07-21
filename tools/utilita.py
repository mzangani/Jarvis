"""
tools/utilita.py — Famiglia di tool "Utilità" (data/ora e meteo).

Due piccoli strumenti di uso quotidiano:

  - data_ora(): la data e l'ora CORRENTI, in italiano leggibile. Sembra banale, ma il
    modello NON conosce l'ora reale (il suo "adesso" è vago): questo tool gliela dà
    davvero, dall'orologio del computer. Solo libreria standard (datetime).

  - meteo(luogo): il tempo attuale in un luogo, via il servizio pubblico wttr.in. È una
    GET in sola lettura verso un host FISSO e fidato (a differenza di leggi_pagina, che
    apre URL arbitrari): niente chiave API, niente costo. Solo urllib (stdlib).

Rischi:
  - data_ora -> SAFE (lettura dell'orologio locale, nessun effetto).
  - meteo    -> SAFE. È sola lettura e colpisce un host fisso; manda in rete solo il nome
    del luogo. Non apre URL scelti dal modello, quindi non ha il rischio SSRF di
    leggi_pagina (che infatti è CAUTION).
"""

import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import quote

from safety import SAFE

# --- data/ora ----------------------------------------------------------------
_GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
_MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
         "agosto", "settembre", "ottobre", "novembre", "dicembre"]


def data_ora() -> str:
    """Restituisce data e ora correnti in italiano (dall'orologio del computer)."""
    # Traduciamo noi giorno e mese: locale='it_IT' potrebbe non essere installato sul
    # sistema, e non vogliamo dipendere da quello. datetime.now() è l'ora LOCALE.
    ora = datetime.now()
    giorno = _GIORNI[ora.weekday()]
    mese = _MESI[ora.month - 1]
    return f"Oggi è {giorno} {ora.day} {mese} {ora.year}, sono le {ora:%H:%M}."


# --- meteo -------------------------------------------------------------------
# Host fisso: NON è un URL scelto dal modello. Il luogo va nel path (codificato).
_WTTR_HOST = "https://wttr.in"
# Formato di wttr.in: %l luogo, %C condizione (testo), %t temperatura, %f percepita,
# %h umidità, %w vento. Gli spazi si mandano come '+', i '%' restano letterali (sono i
# segnaposto di wttr): quindi questo NON va url-encodato, e teniamo le etichette senza
# accenti per non dover codificare caratteri non-ASCII.
_FORMATO = "%l:+%C,+temperatura+%t+(percepita+%f),+umidita+%h,+vento+%w"
_TIMEOUT = 15
# wttr.in serve testo semplice ai client "curl-like" e HTML ai browser: ci presentiamo
# come curl per ottenere la riga di testo pulita.
_USER_AGENT = "curl/8 (Jarvis assistente locale)"
_MAX = 2000  # la risposta è una riga: un tetto basso basta e avanza


def meteo(luogo: str) -> str:
    """Restituisce il tempo attuale nel `luogo` indicato (via wttr.in)."""
    luogo = (luogo or "").strip()
    if not luogo:
        raise ValueError("Luogo mancante: dimmi di che città/posto vuoi il meteo.")
    # quote() codifica il luogo per il path in modo sicuro (spazi, accenti, ecc.).
    # '&m' = unità metriche (°C), '&lang=it' = condizione in italiano.
    url = f"{_WTTR_HOST}/{quote(luogo)}?format={_FORMATO}&lang=it&m"
    richiesta = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(richiesta, timeout=_TIMEOUT) as risposta:
            grezzo = risposta.read(_MAX + 1)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Il servizio meteo ha risposto con un errore HTTP {e.code} ({e.reason}).")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Non riesco a contattare il servizio meteo: {e.reason}")

    testo = grezzo.decode("utf-8", errors="replace").strip()
    # wttr.in risponde a volte con "Unknown location; ..." (con codice 200): giriamolo
    # all'utente com'è, senza fingere un meteo che non ha.
    if not testo:
        raise RuntimeError("Il servizio meteo ha restituito una risposta vuota.")
    return testo[:_MAX]


# --- Schemi per il modello ---------------------------------------------------
DATA_ORA = {
    "name": "data_ora",
    "description": (
        "Restituisce la DATA e l'ORA correnti (dall'orologio del computer dell'utente). "
        "Usalo SEMPRE che serve sapere che giorno/ora è davvero — per esempio 'che ore "
        "sono?', 'che giorno è oggi?', o quando devi calcolare qualcosa basato su 'adesso' "
        "(scadenze, 'tra 3 giorni', l'età). Non tirare a indovinare l'ora: chiamalo."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

METEO = {
    "name": "meteo",
    "description": (
        "Restituisce il tempo meteorologico ATTUALE in un luogo (città o località), via il "
        "servizio pubblico wttr.in. Usalo per domande come 'che tempo fa a Milano?'. "
        "Fornisce condizione, temperatura (anche percepita), umidità e vento. Se il luogo "
        "non è riconosciuto il servizio lo dice: riferiscilo all'utente invece di inventare. "
        "Manda in rete solo il nome del luogo; è gratis e non richiede conferma."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "luogo": {
                "type": "string",
                "description": "La città o località di cui vuoi il meteo, es. 'Milano' o 'Roma, Italia'.",
            }
        },
        "required": ["luogo"],
    },
}


# Terne (schema, funzione, rischio). Entrambi SAFE.
TOOLS = [
    (DATA_ORA, data_ora, SAFE),
    (METEO, meteo, SAFE),
]
