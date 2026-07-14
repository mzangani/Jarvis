"""
tools/memory.py — Famiglia di tool "Memoria".

È il "ponte" tra il modello e l'infrastruttura memory.py: espone due tool che il
modello può decidere di usare, e ciascuno si limita a chiamare la funzione giusta
in memory.py (dove vive tutta la logica SQLite e la connessione singleton).

  - ricorda(fatto):  salva un'informazione durevole sull'utente sul nostro DB locale.
  - richiama(query): cerca tra i fatti salvati (keyword match, non semantico).

Rischio: entrambi SAFE.
  - richiama è sola lettura.
  - ricorda scrive SOLO sul nostro DB locale (nessun effetto su file dell'utente,
    sistema o rete): niente conferma, altrimenti spammeremmo l'utente ad ogni fatto.
Essendo SAFE, il loop in brain.py li esegue senza passare da safety.confirm().

Nota: NON prendiamo qui l'handle del DB. brain.dispatch chiama funzione(**input) e
non può passarci oggetti; per questo la connessione è un singleton dentro memory.py
e le funzioni la recuperano da sole.
"""

import memory
from safety import SAFE


# --- Implementazioni (sottili: la logica vera è in memory.py) ----------------

def ricorda(fatto: str) -> str:
    """Salva un fatto persistente sull'utente. Ritorna una conferma testuale."""
    return memory.ricorda_fatto(fatto)


def richiama(query: str) -> str:
    """Cerca nei fatti salvati quelli che corrispondono a `query`."""
    return memory.richiama_fatti(query)


# --- Schemi per il modello ---------------------------------------------------
# Description scritte PER IL MODELLO: cosa fa il tool, QUANDO usarlo, e i limiti onesti.
RICORDA = {
    "name": "ricorda",
    "description": (
        "Salva in modo PERMANENTE un fatto o una preferenza durevole sull'utente, "
        "così resterà disponibile anche nelle sessioni future. Usalo quando l'utente "
        "ti comunica un'informazione stabile che vale la pena ricordare (es. 'il mio "
        "progetto sta in D:\\dev', 'preferisco risposte brevi', 'mi chiamo Matteo', "
        "'uso Python 3.12'). NON usarlo per dettagli effimeri di questa sola "
        "conversazione. Salva un fatto per chiamata, formulato in modo chiaro e "
        "autosufficiente (che si capisca anche fuori contesto). È un'azione locale e "
        "sicura: non richiede conferma."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "fatto": {
                "type": "string",
                "description": (
                    "Il fatto da memorizzare, in una frase chiara e autosufficiente, "
                    "es. 'Il progetto dell'utente si trova in D:\\dev'."
                ),
            }
        },
        "required": ["fatto"],
    },
}

RICHIAMA = {
    "name": "richiama",
    "description": (
        "Cerca tra i fatti che hai memorizzato sull'utente quelli che corrispondono a "
        "una query. Usalo quando ti serve un'informazione durevole sull'utente che NON "
        "è già elencata nel blocco 'Cose che ricordi sull'utente' del tuo prompt (i "
        "fatti più recenti sono già lì; usa 'richiama' per il resto o per cercare un "
        "argomento specifico). La ricerca è per PAROLE CHIAVE, non semantica: cerca "
        "con le parole probabilmente presenti nel fatto (es. 'progetto', 'cartella'). "
        "Se non trova nulla, dillo all'utente invece di inventare."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Le parole chiave da cercare tra i fatti memorizzati, "
                    "es. 'dove progetto cartella'."
                ),
            }
        },
        "required": ["query"],
    },
}


# Terne (schema, funzione, rischio). Entrambi SAFE: nessuna conferma nel loop.
TOOLS = [
    (RICORDA, ricorda, SAFE),
    (RICHIAMA, richiama, SAFE),
]
