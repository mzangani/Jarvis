"""
tools/ — Raccolta degli strumenti (tool) di Jarvis.

Ogni file di questa cartella è una FAMIGLIA di tool e mette a disposizione una
lista `TOOLS` di coppie (schema, funzione). Questo file aggrega tutte le famiglie
in due oggetti comodi per brain.py:

  - SCHEMAS: la lista di schemi da passare all'API (dice al modello quali tool esistono)
  - dispatch(name, input): esegue il tool giusto dato il nome scelto dal modello
"""

from . import system

# Elenco dei moduli-famiglia.
# Nelle prossime fasi aggiungeremo qui: files, shell, web.
_MODULI = [system]

# Costruiamo il "registro": schema per l'API + mappa nome -> funzione.
SCHEMAS: list[dict] = []
_IMPL: dict = {}

for _mod in _MODULI:
    for _schema, _funzione in _mod.TOOLS:
        SCHEMAS.append(_schema)
        _IMPL[_schema["name"]] = _funzione


def dispatch(name: str, tool_input: dict) -> str:
    """Esegue il tool con il nome dato, usando gli argomenti scelti dal modello."""
    if name not in _IMPL:
        # Fail loud: nome sconosciuto -> solleviamo un errore, non lo ingoiamo.
        raise ValueError(f"Tool sconosciuto richiesto dal modello: {name}")
    funzione = _IMPL[name]
    # tool_input è un dict {nome_argomento: valore}; lo espandiamo come argomenti nominati.
    return funzione(**tool_input)
