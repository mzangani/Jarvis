"""
tools/ — Raccolta degli strumenti (tool) di Jarvis.

Ogni file di questa cartella è una FAMIGLIA di tool e mette a disposizione una
lista `TOOLS` di coppie (schema, funzione). Questo file aggrega tutte le famiglie
in due oggetti comodi per brain.py:

  - SCHEMAS: la lista di schemi da passare all'API (dice al modello quali tool esistono)
  - SERVER_TOOLS: i tool "server-side" (eseguiti da Anthropic, es. web_search)
  - dispatch(name, input): esegue il tool giusto dato il nome scelto dal modello
"""

from . import files, system, shell, web, memory, appunti, utilita, files_extra

# Elenco dei moduli-famiglia. Ogni famiglia espone TOOLS (e opzionalmente PRECHECKS).
_MODULI = [system, files, shell, web, memory, appunti, utilita, files_extra]

# Costruiamo il "registro": schema per l'API + mappa nome -> funzione + mappa
# nome -> rischio + mappa nome -> pre-check (validazione categorica prima della conferma).
SCHEMAS: list[dict] = []
SERVER_TOOLS: list[dict] = []
_IMPL: dict = {}
_RISK: dict = {}
_PRECHECK: dict = {}

for _mod in _MODULI:
    for _schema, _funzione, _rischio in _mod.TOOLS:
        SCHEMAS.append(_schema)
        _IMPL[_schema["name"]] = _funzione
        _RISK[_schema["name"]] = _rischio
    # Le famiglie possono opzionalmente dichiarare PRECHECKS (nome -> validatore).
    # getattr con default {}: chi non lo dichiara non ha pre-check, ed è normale.
    _PRECHECK.update(getattr(_mod, "PRECHECKS", {}))
    # ...e possono dichiarare SERVER_TOOLS: tool eseguiti da Anthropic (non da noi),
    # come il web search. Li raccogliamo a parte e li concateneremo agli SCHEMAS solo
    # al momento della chiamata API. NON entrano in _IMPL/_RISK/_PRECHECK: dispatch,
    # risk_of e precheck non li vedono nemmeno, perché non li eseguiamo noi.
    SERVER_TOOLS.extend(getattr(_mod, "SERVER_TOOLS", []))


def dispatch(name: str, tool_input: dict) -> str:
    """Esegue il tool con il nome dato, usando gli argomenti scelti dal modello."""
    if name not in _IMPL:
        # Fail loud: nome sconosciuto -> solleviamo un errore, non lo ingoiamo.
        raise ValueError(f"Tool sconosciuto richiesto dal modello: {name}")
    funzione = _IMPL[name]
    # tool_input è un dict {nome_argomento: valore}; lo espandiamo come argomenti nominati.
    return funzione(**tool_input)


def risk_of(name: str) -> str:
    """Livello di rischio di un tool. Un nome sconosciuto è trattato come DANGEROUS (fail closed)."""
    from safety import DANGEROUS
    return _RISK.get(name, DANGEROUS)


def precheck(name: str, tool_input: dict) -> None:
    """
    Esegue l'eventuale validazione categorica del tool PRIMA della conferma.

    Serve a respingere subito ciò che è sempre vietato (es. la blacklist della
    shell) senza nemmeno chiedere conferma all'utente. Se il tool non dichiara un
    pre-check è un no-op: la maggioranza dei tool non ne ha bisogno. Solleva
    l'eccezione del validatore (es. PermissionError) se l'azione è vietata.
    """
    validatore = _PRECHECK.get(name)
    if validatore is not None:
        validatore(tool_input)
