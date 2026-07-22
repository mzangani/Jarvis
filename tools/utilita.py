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
# Servizio PRIMARIO: Open-Meteo (open-meteo.com) — gratuito, senza chiave, JSON,
# molto più affidabile di wttr.in (che resta come RIPIEGO: due servizi indipendenti
# reggono meglio di uno). Host FISSI: non sono URL scelti dal modello.
_GEOCODING = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST = "https://api.open-meteo.com/v1/forecast"
_WTTR_HOST = "https://wttr.in"
_FORMATO_WTTR = "%l:+%C,+temperatura+%t+(percepita+%f),+umidita+%h,+vento+%w"
_TIMEOUT = 15
_USER_AGENT = "curl/8 (Jarvis assistente locale)"
_MAX = 2000

# Codici meteo WMO (usati da Open-Meteo) -> descrizione in italiano. Funzione di
# lookup PURA, testata a secco.
_WMO = {
    0: "sereno", 1: "prevalentemente sereno", 2: "parzialmente nuvoloso", 3: "coperto",
    45: "nebbia", 48: "nebbia con brina",
    51: "pioviggine leggera", 53: "pioviggine", 55: "pioviggine intensa",
    56: "pioviggine gelata", 57: "pioviggine gelata intensa",
    61: "pioggia leggera", 63: "pioggia", 65: "pioggia forte",
    66: "pioggia gelata", 67: "pioggia gelata forte",
    71: "neve leggera", 73: "neve", 75: "neve forte", 77: "nevischio",
    80: "rovesci leggeri", 81: "rovesci", 82: "rovesci violenti",
    85: "rovesci di neve", 86: "rovesci di neve forti",
    95: "temporale", 96: "temporale con grandine", 99: "temporale con grandine forte",
}


def descrizione_wmo(codice: int) -> str:
    """Descrizione italiana di un codice meteo WMO (onesta sul non mappato)."""
    return _WMO.get(codice, f"condizione {codice}")


def formatta_corrente(nome: str, corrente: dict) -> str:
    """Frase leggibile dalle condizioni CORRENTI di Open-Meteo. PURA, testabile."""
    return (
        f"{nome}: {descrizione_wmo(corrente['weather_code'])}, "
        f"{round(corrente['temperature_2m'])}°C "
        f"(percepiti {round(corrente['apparent_temperature'])}°C), "
        f"umidità {corrente['relative_humidity_2m']}%, "
        f"vento {round(corrente['wind_speed_10m'])} km/h."
    )


def formatta_domani(nome: str, daily: dict) -> str:
    """Frase leggibile dalla previsione di DOMANI (indice 1 del blocco daily). PURA."""
    return (
        f"Domani a {nome}: {descrizione_wmo(daily['weather_code'][1])}, "
        f"minima {round(daily['temperature_2m_min'][1])}°C, "
        f"massima {round(daily['temperature_2m_max'][1])}°C, "
        f"probabilità di precipitazioni {daily['precipitation_probability_max'][1]}%."
    )


def _json_http(url: str) -> dict:
    """GET + parse JSON con timeout, verso i soli host fissi di questo modulo."""
    import json
    richiesta = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(richiesta, timeout=_TIMEOUT) as risposta:
        return json.loads(risposta.read(200_000).decode("utf-8"))


def _meteo_wttr(luogo: str) -> str:
    """RIPIEGO: il vecchio percorso via wttr.in (una riga di testo già pronta)."""
    url = f"{_WTTR_HOST}/{quote(luogo)}?format={_FORMATO_WTTR}&lang=it&m"
    richiesta = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(richiesta, timeout=_TIMEOUT) as risposta:
        testo = risposta.read(_MAX + 1).decode("utf-8", errors="replace").strip()
    if not testo:
        raise RuntimeError("risposta vuota da wttr.in")
    return testo[:_MAX]


def meteo(luogo: str, quando: str = "oggi") -> str:
    """Il tempo nel luogo indicato: 'oggi' (condizioni attuali) o 'domani' (previsione)."""
    luogo = (luogo or "").strip()
    if not luogo:
        raise ValueError("Luogo mancante: dimmi di che città/posto vuoi il meteo.")
    quando = (quando or "oggi").strip().lower()
    if quando not in ("oggi", "domani"):
        raise ValueError(f"'quando' non valido: {quando!r} (oggi/domani).")

    try:
        # 1) Geocoding: dal nome del luogo a coordinate (e nome "ufficiale").
        geo = _json_http(f"{_GEOCODING}?name={quote(luogo)}&count=1&language=it")
        risultati = geo.get("results") or []
        if not risultati:
            # Luogo ignoto: è una risposta VALIDA del servizio, non un guasto — niente
            # ripiego, diciamolo com'è.
            return f"Non trovo nessun luogo chiamato «{luogo}»: controlla il nome."
        posto = risultati[0]
        nome = posto.get("name", luogo)
        # 2) Previsioni: correnti + blocco giornaliero (per 'domani').
        dati = _json_http(
            f"{_FORECAST}?latitude={posto['latitude']}&longitude={posto['longitude']}"
            "&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
            "weather_code,wind_speed_10m"
            "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max"
            "&timezone=auto&forecast_days=2"
        )
        if quando == "domani":
            return formatta_domani(nome, dati["daily"])
        return formatta_corrente(nome, dati["current"])
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError, OSError) as e:
        # Open-Meteo giù o risposta inattesa: proviamo il ripiego (solo per l'oggi:
        # wttr.in nel nostro formato dà le condizioni correnti).
        if quando == "oggi":
            try:
                return _meteo_wttr(luogo)
            except Exception:
                pass  # anche il ripiego è andato male: riportiamo il guasto originale
        raise RuntimeError(
            f"Il servizio meteo non risponde al momento ({e}). Riprova tra poco."
        ) from e


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
        "Restituisce il METEO in un luogo (città o località): condizioni ATTUALI "
        "(quando='oggi') o la PREVISIONE di domani (quando='domani'), via il servizio "
        "pubblico Open-Meteo (con ripiego automatico su wttr.in). Fornisce condizione, "
        "temperature, umidità, vento; per domani anche la probabilità di pioggia. Se il "
        "luogo non esiste lo dice: riferiscilo invece di inventare. Manda in rete solo "
        "il nome del luogo; è gratis e non richiede conferma."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "luogo": {
                "type": "string",
                "description": "La città o località, es. 'Milano' o 'Roma'.",
            },
            "quando": {"type": "string", "enum": ["oggi", "domani"]},
        },
        "required": ["luogo"],
    },
}


# Terne (schema, funzione, rischio). Entrambi SAFE.
TOOLS = [
    (DATA_ORA, data_ora, SAFE),
    (METEO, meteo, SAFE),
]
