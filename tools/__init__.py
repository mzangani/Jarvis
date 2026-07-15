"""
tools/ — Raccolta degli strumenti (tool) di Jarvis.

Ogni file di questa cartella è una FAMIGLIA di tool e mette a disposizione una
lista `TOOLS` di coppie (schema, funzione). Questo file aggrega tutte le famiglie
in due oggetti comodi per brain.py:

  - SCHEMAS: la lista di schemi da passare all'API (dice al modello quali tool esistono)
  - dispatch(name, input): esegue il tool giusto dato il nome scelto dal modello
"""

from . import files, system

# Elenco dei moduli-famiglia.
# Nelle prossime fasi aggiungeremo qui: shell, web.
_MODULI = [system, files]

# Costruiamo il "registro": schema per l'API + mappa nome -> funzione + mappa nome -> rischio.
SCHEMAS: list[dict] = []
_IMPL: dict = {}
_RISK: dict = {}

for _mod in _MODULI:
    for _schema, _funzione, _rischio in _mod.TOOLS:
        SCHEMAS.append(_schema)
        _IMPL[_schema["name"]] = _funzione
        _RISK[_schema["name"]] = _rischio


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
