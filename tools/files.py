"""
tools/files.py — Famiglia di tool "File".

Sono i primi strumenti con effetti REALI sul disco. La regola di sicurezza è
una sola, ma non negoziabile:

  OGNI funzione che tocca un percorso chiama `safety.ensure_in_sandbox()` come
  PRIMA riga. Quella funzione risolve '..' e i symlink e solleva PermissionError
  se il percorso finale esce dalla sandbox. Così l'agente non può leggere o
  scrivere fuori dalla cartella consentita, nemmeno provandoci di proposito.

La CONFERMA per le azioni rischiose NON è qui: la gestisce il loop in brain.py
(chiama safety.confirm() prima di dispatch per ogni tool non-SAFE). Qui ci
limitiamo a dichiarare il rischio corretto nella terna (schema, funzione, rischio).

Rischi in questa famiglia:
  - leggi_file, lista_dir, cerca_per_nome, cerca_nel_contenuto -> SAFE (sola lettura)
  - scrivi_file, sposta, crea_cartella                         -> CAUTION (modificano)
  (La cancellazione, DANGEROUS, non è tra i tool richiesti in 4a: non la aggiungiamo.)
"""

import fnmatch
import shutil
from pathlib import Path

import safety
from safety import SAFE, CAUTION

# Quanti byte al massimo leggiamo/scandagliamo per file. Serve a non riversare
# file enormi (o binari) dentro il contesto del modello: sarebbe inutile e costoso.
_MAX_BYTES = 100_000


# --- Implementazioni ---------------------------------------------------------

def leggi_file(percorso: str) -> str:
    """Legge un file di testo dentro la sandbox e ne restituisce il contenuto."""
    p = safety.ensure_in_sandbox(percorso)  # <- SEMPRE la prima riga
    if not p.exists():
        # Fail loud: non fingiamo un file vuoto, diciamo che non c'è.
        raise FileNotFoundError(f"Il file non esiste: {p}")
    if p.is_dir():
        raise IsADirectoryError(f"È una cartella, non un file: {p}. Usa lista_dir.")

    dati = p.read_bytes()
    troncato = len(dati) > _MAX_BYTES
    # 'errors=replace': se incappiamo in un file non testuale non esplodiamo,
    # ma i caratteri illeggibili diventano '�' e sarà evidente al modello.
    testo = dati[:_MAX_BYTES].decode("utf-8", errors="replace")
    if troncato:
        testo += f"\n\n[...troncato: mostrati i primi {_MAX_BYTES} byte di {len(dati)}...]"
    return testo


def scrivi_file(percorso: str, contenuto: str) -> str:
    """Scrive (creando o sovrascrivendo) un file di testo dentro la sandbox."""
    p = safety.ensure_in_sandbox(percorso)
    if p.is_dir():
        raise IsADirectoryError(f"È una cartella, non un file: {p}.")
    # Creiamo le cartelle intermedie mancanti: scrivere 'a/b/c.txt' non deve
    # fallire solo perché 'a/b' non esiste ancora.
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(contenuto, encoding="utf-8")
    return f"Scritti {len(contenuto)} caratteri in {p}"


def lista_dir(percorso: str = ".") -> str:
    """Elenca i file e le sottocartelle di una cartella dentro la sandbox."""
    p = safety.ensure_in_sandbox(percorso)
    if not p.exists():
        raise FileNotFoundError(f"La cartella non esiste: {p}")
    if not p.is_dir():
        raise NotADirectoryError(f"Non è una cartella: {p}. Usa leggi_file.")

    voci = []
    # Ordiniamo per nome così l'output è stabile e prevedibile.
    for figlio in sorted(p.iterdir(), key=lambda x: x.name.lower()):
        if figlio.is_dir():
            voci.append(f"[dir]  {figlio.name}/")
        else:
            # Mostriamo anche la dimensione: al modello serve per decidere se
            # ha senso leggere il file o è troppo grande.
            voci.append(f"[file] {figlio.name} ({figlio.stat().st_size} byte)")

    if not voci:
        return f"La cartella {p} è vuota."
    return f"Contenuto di {p}:\n" + "\n".join(voci)


def cerca_per_nome(pattern: str) -> str:
    """Cerca ricorsivamente file/cartelle il cui NOME corrisponde a un pattern glob."""
    # La radice della ricerca è sempre la sandbox: passiamo "." a ensure_in_sandbox
    # per ottenere il percorso reale e verificato della radice.
    radice = safety.ensure_in_sandbox(".")
    trovati = []
    # rglob("*") scende ricorsivamente; poi filtriamo i NOMI con fnmatch, così
    # il pattern (es. '*.txt', 'appunti*') vale sul nome del file, non sul path.
    for elem in radice.rglob("*"):
        if fnmatch.fnmatch(elem.name, pattern):
            rel = elem.relative_to(radice)  # mostriamo percorsi relativi, più leggibili
            trovati.append(f"{rel}/" if elem.is_dir() else str(rel))

    if not trovati:
        return f"Nessun risultato per il pattern '{pattern}'."
    return f"Trovati {len(trovati)} risultati per '{pattern}':\n" + "\n".join(sorted(trovati))


def cerca_nel_contenuto(testo: str) -> str:
    """Cerca una stringa DENTRO il contenuto testuale dei file della sandbox."""
    radice = safety.ensure_in_sandbox(".")
    risultati = []
    for elem in radice.rglob("*"):
        if not elem.is_file():
            continue
        try:
            # Leggiamo solo i primi _MAX_BYTES: evita di caricare file enormi.
            dati = elem.read_bytes()[:_MAX_BYTES]
        except OSError:
            # File illeggibile (permessi, ecc.): lo saltiamo, non blocchiamo tutta
            # la ricerca. Non è un except vuoto: gestiamo un caso preciso e noto.
            continue
        contenuto = dati.decode("utf-8", errors="ignore")
        for num, riga in enumerate(contenuto.splitlines(), start=1):
            if testo in riga:
                rel = elem.relative_to(radice)
                risultati.append(f"{rel}:{num}: {riga.strip()}")

    if not risultati:
        return f"Nessuna riga contiene '{testo}'."
    # Limitiamo l'output a 100 righe per non intasare il contesto del modello.
    intestazione = f"Trovate {len(risultati)} righe con '{testo}':\n"
    return intestazione + "\n".join(risultati[:100])


def sposta(origine: str, destinazione: str) -> str:
    """Sposta o rinomina un file/cartella; ENTRAMBI i percorsi devono stare in sandbox."""
    # Attenzione: qui i percorsi da validare sono DUE. Verifichiamo entrambi,
    # altrimenti si potrebbe spostare un file FUORI dalla sandbox.
    orig = safety.ensure_in_sandbox(origine)
    dest = safety.ensure_in_sandbox(destinazione)
    if not orig.exists():
        raise FileNotFoundError(f"L'origine non esiste: {orig}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(orig), str(dest))
    return f"Spostato: {orig} -> {dest}"


def crea_cartella(percorso: str) -> str:
    """Crea una cartella (con le intermedie) dentro la sandbox."""
    p = safety.ensure_in_sandbox(percorso)
    # exist_ok=True: se esiste già non è un errore, l'obiettivo è comunque raggiunto.
    p.mkdir(parents=True, exist_ok=True)
    return f"Cartella pronta: {p}"


# --- Schemi per il modello ---------------------------------------------------
# Ogni description è scritta PER IL MODELLO: cosa fa, quando usarlo, quando NON
# usarlo (con rimando al tool giusto), e note sul formato degli argomenti.

_NOTA_PERCORSO = (
    "Il percorso è relativo alla sandbox di Jarvis (es. 'note/appunti.txt'); "
    "i percorsi assoluti fuori dalla sandbox vengono rifiutati."
)

LEGGI_FILE = {
    "name": "leggi_file",
    "description": (
        "Legge e restituisce il contenuto testuale di UN file. "
        "Usalo quando l'utente vuole vedere/riassumere/analizzare cosa c'è in un file. "
        "NON usarlo per elencare una cartella (usa lista_dir) né per cercare testo tra "
        "più file (usa cerca_nel_contenuto). " + _NOTA_PERCORSO
    ),
    "input_schema": {
        "type": "object",
        "properties": {"percorso": {"type": "string", "description": _NOTA_PERCORSO}},
        "required": ["percorso"],
    },
}

SCRIVI_FILE = {
    "name": "scrivi_file",
    "description": (
        "Crea un nuovo file o SOVRASCRIVE completamente uno esistente con il contenuto "
        "fornito. Usalo per salvare testo, appunti, codice. Attenzione: sovrascrive "
        "tutto il file, non aggiunge in coda. Le cartelle intermedie mancanti vengono "
        "create. " + _NOTA_PERCORSO
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "percorso": {"type": "string", "description": _NOTA_PERCORSO},
            "contenuto": {"type": "string", "description": "Il testo completo da scrivere nel file."},
        },
        "required": ["percorso", "contenuto"],
    },
}

LISTA_DIR = {
    "name": "lista_dir",
    "description": (
        "Elenca file e sottocartelle di una cartella, con la dimensione dei file. "
        "Usalo per esplorare cosa c'è nella sandbox o in una sua sottocartella. "
        "NON usarlo per leggere il contenuto di un file (usa leggi_file). "
        "Se ometti il percorso, elenca la radice della sandbox."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "percorso": {
                "type": "string",
                "description": "Cartella da elencare, relativa alla sandbox. Default: radice della sandbox.",
            }
        },
        "required": [],
    },
}

CERCA_PER_NOME = {
    "name": "cerca_per_nome",
    "description": (
        "Cerca ricorsivamente nella sandbox file/cartelle il cui NOME corrisponde a un "
        "pattern glob (es. '*.txt', 'appunti*', 'foto?.png'). Usalo quando conosci (anche "
        "in parte) il nome del file ma non dove si trova. NON usarlo per cercare del testo "
        "DENTRO i file: per quello usa cerca_nel_contenuto."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Pattern glob sul nome, es. '*.txt' o 'report*'.",
            }
        },
        "required": ["pattern"],
    },
}

CERCA_NEL_CONTENUTO = {
    "name": "cerca_nel_contenuto",
    "description": (
        "Cerca una stringa di testo DENTRO il contenuto dei file della sandbox e "
        "restituisce le righe che la contengono (con file e numero di riga). Usalo per "
        "domande tipo 'in quale file ho scritto X?'. NON usarlo per cercare per nome di "
        "file (usa cerca_per_nome). La ricerca è sensibile a maiuscole/minuscole."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "testo": {"type": "string", "description": "Il testo da cercare dentro i file."}
        },
        "required": ["testo"],
    },
}

SPOSTA = {
    "name": "sposta",
    "description": (
        "Sposta o rinomina un file o una cartella (origine -> destinazione). Rinominare "
        "significa spostare nella stessa cartella con un nome diverso. ENTRAMBI i percorsi "
        "devono stare nella sandbox. NON è una copia: l'origine dopo non esiste più."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "origine": {"type": "string", "description": "Percorso attuale, relativo alla sandbox."},
            "destinazione": {"type": "string", "description": "Nuovo percorso, relativo alla sandbox."},
        },
        "required": ["origine", "destinazione"],
    },
}

CREA_CARTELLA = {
    "name": "crea_cartella",
    "description": (
        "Crea una cartella (comprese le eventuali cartelle intermedie) dentro la sandbox. "
        "Usalo per organizzare i file. NON serve per creare file (usa scrivi_file, che crea "
        "da solo le cartelle mancanti). Se la cartella esiste già non è un errore. "
        + _NOTA_PERCORSO
    ),
    "input_schema": {
        "type": "object",
        "properties": {"percorso": {"type": "string", "description": _NOTA_PERCORSO}},
        "required": ["percorso"],
    },
}


# Terne (schema, funzione, rischio). La conferma per CAUTION è del loop, non nostra.
TOOLS = [
    (LEGGI_FILE, leggi_file, SAFE),
    (LISTA_DIR, lista_dir, SAFE),
    (CERCA_PER_NOME, cerca_per_nome, SAFE),
    (CERCA_NEL_CONTENUTO, cerca_nel_contenuto, SAFE),
    (SCRIVI_FILE, scrivi_file, CAUTION),
    (SPOSTA, sposta, CAUTION),
    (CREA_CARTELLA, crea_cartella, CAUTION),
]
