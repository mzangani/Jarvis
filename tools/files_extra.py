"""
tools/files_extra.py — Famiglia "File avanzati": archivi zip, info e checksum.

Estende la famiglia File con operazioni comode ma un po' più specialistiche. Vale la
STESSA regola non negoziabile: OGNI funzione che tocca un percorso chiama
`safety.ensure_in_sandbox()` come prima cosa, così nulla esce dalla cartella consentita.

  - info_file(percorso)          -> dimensione, tipo, ultima modifica di un file/cartella.
  - hash_file(percorso, algo)    -> checksum (sha256 di default) per verificare un file.
  - comprimi_zip(sorgenti, dest) -> crea un archivio .zip da file/cartelle della sandbox.
  - estrai_zip(archivio, dest)   -> estrae un .zip DENTRO la sandbox.

Rischi:
  - info_file, hash_file  -> SAFE (sola lettura).
  - comprimi_zip, estrai_zip -> CAUTION (scrivono file): la conferma è del loop in brain.py.

Sicurezza degli archivi — "zip-slip": un .zip malevolo può contenere voci con percorsi
tipo '../../fuori.txt' per scrivere FUORI dalla cartella di destinazione. Prima di
estrarre, verifichiamo che OGNI voce resti dentro la destinazione (che è già in sandbox);
se una sola evade, rifiutiamo l'intero archivio (fail loud), senza estrarre nulla.
"""

import hashlib
import zipfile
from datetime import datetime

import safety
from safety import SAFE, CAUTION

# Algoritmi di hash ammessi: i più comuni. md5/sha1 sono deboli per la crittografia ma
# validissimi per un checksum "è lo stesso file?"; li teniamo, dichiarandolo.
_ALGO_AMMESSI = {"sha256", "sha1", "md5", "sha512"}
# Leggiamo i file a blocchi per non caricare in memoria file enormi mentre calcoliamo l'hash.
_BLOCCO = 1024 * 1024  # 1 MB


def _dimensione_leggibile(byte: int) -> str:
    """Da byte a una forma umana (KB/MB/GB), accanto al valore esatto."""
    valore = float(byte)
    for unita in ("byte", "KB", "MB", "GB", "TB"):
        if valore < 1024 or unita == "TB":
            intero = f"{int(valore)}" if unita == "byte" else f"{valore:.1f}"
            return f"{intero} {unita}"
        valore /= 1024
    return f"{byte} byte"


def info_file(percorso: str) -> str:
    """Restituisce dimensione, tipo e data di ultima modifica di un file o cartella."""
    p = safety.ensure_in_sandbox(percorso)
    if not p.exists():
        raise FileNotFoundError(f"Non esiste: {p}")
    st = p.stat()
    quando = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
    if p.is_dir():
        # Per una cartella la "dimensione" utile è quanti elementi contiene.
        n = sum(1 for _ in p.iterdir())
        return f"{p}\n  tipo: cartella\n  elementi: {n}\n  ultima modifica: {quando}"
    return (
        f"{p}\n"
        f"  tipo: file\n"
        f"  dimensione: {_dimensione_leggibile(st.st_size)} ({st.st_size} byte)\n"
        f"  ultima modifica: {quando}"
    )


def hash_file(percorso: str, algoritmo: str = "sha256") -> str:
    """Calcola il checksum di un file (per verificarne l'integrità o l'identità)."""
    algoritmo = (algoritmo or "sha256").lower().strip()
    if algoritmo not in _ALGO_AMMESSI:
        raise ValueError(
            f"Algoritmo non supportato: '{algoritmo}'. Ammessi: {', '.join(sorted(_ALGO_AMMESSI))}."
        )
    p = safety.ensure_in_sandbox(percorso)
    if not p.exists():
        raise FileNotFoundError(f"Il file non esiste: {p}")
    if p.is_dir():
        raise IsADirectoryError(f"È una cartella, non un file: {p}.")
    h = hashlib.new(algoritmo)
    with p.open("rb") as f:
        for blocco in iter(lambda: f.read(_BLOCCO), b""):
            h.update(blocco)
    return f"{algoritmo}({p.name}) = {h.hexdigest()}"


def comprimi_zip(sorgenti, destinazione: str) -> str:
    """Crea un archivio .zip dalle sorgenti indicate (file e/o cartelle della sandbox)."""
    # Il modello può passare una lista o, per comodità, un singolo percorso: normalizziamo.
    if isinstance(sorgenti, str):
        sorgenti = [sorgenti]
    if not sorgenti:
        raise ValueError("Nessuna sorgente da comprimere.")
    dest = safety.ensure_in_sandbox(destinazione)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Validiamo TUTTE le sorgenti (dentro sandbox + esistenti) prima di iniziare a scrivere.
    percorsi = []
    for s in sorgenti:
        p = safety.ensure_in_sandbox(s)
        if not p.exists():
            raise FileNotFoundError(f"Sorgente inesistente: {p}")
        percorsi.append(p)

    n = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in percorsi:
            if p.is_dir():
                # Includiamo la cartella stessa nel nome: arcname relativo al suo genitore.
                for f in sorted(p.rglob("*")):
                    if f.is_file():
                        z.write(f, arcname=str(f.relative_to(p.parent)))
                        n += 1
            else:
                z.write(p, arcname=p.name)
                n += 1
    return f"Creato l'archivio {dest} con {n} file."


def estrai_zip(archivio: str, destinazione: str = ".") -> str:
    """Estrae un archivio .zip in una cartella della sandbox (con protezione anti zip-slip)."""
    arch = safety.ensure_in_sandbox(archivio)
    if not arch.exists():
        raise FileNotFoundError(f"L'archivio non esiste: {arch}")
    if not zipfile.is_zipfile(arch):
        raise ValueError(f"Non è un archivio zip valido: {arch}")
    dest = safety.ensure_in_sandbox(destinazione)
    dest.mkdir(parents=True, exist_ok=True)
    radice = dest.resolve()

    with zipfile.ZipFile(arch) as z:
        # CANCELLO anti zip-slip: nessuna voce può finire fuori dalla destinazione.
        for membro in z.namelist():
            bersaglio = (dest / membro).resolve()
            if bersaglio != radice and not bersaglio.is_relative_to(radice):
                raise PermissionError(
                    f"Archivio rifiutato: la voce '{membro}' uscirebbe dalla cartella di "
                    f"destinazione ({radice}). Possibile tentativo di zip-slip."
                )
        nomi = z.namelist()
        z.extractall(dest)
    return f"Estratti {len(nomi)} elementi da {arch.name} in {dest}."


# --- Schemi per il modello ---------------------------------------------------
_NOTA_PERCORSO = (
    "Il percorso è relativo alla sandbox di Jarvis (es. 'archivi/foto.zip'); "
    "i percorsi fuori dalla sandbox vengono rifiutati."
)

INFO_FILE = {
    "name": "info_file",
    "description": (
        "Restituisce informazioni su un file o una cartella: dimensione (per i file), "
        "tipo, numero di elementi (per le cartelle) e data di ultima modifica. Usalo per "
        "'quanto è grande questo file?', 'quando l'ho modificato?'. NON legge il contenuto "
        "(per quello usa leggi_file). " + _NOTA_PERCORSO
    ),
    "input_schema": {
        "type": "object",
        "properties": {"percorso": {"type": "string", "description": _NOTA_PERCORSO}},
        "required": ["percorso"],
    },
}

HASH_FILE = {
    "name": "hash_file",
    "description": (
        "Calcola il checksum (impronta) di un file, per verificarne l'integrità o "
        "controllare se due file sono identici. Algoritmo predefinito sha256; ammessi anche "
        "sha1, md5, sha512. Usalo per 'che hash ha questo file?' o 'questi due file sono "
        "uguali?' (confrontando gli hash). " + _NOTA_PERCORSO
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "percorso": {"type": "string", "description": _NOTA_PERCORSO},
            "algoritmo": {
                "type": "string",
                "description": "Algoritmo: sha256 (default), sha1, md5 o sha512.",
            },
        },
        "required": ["percorso"],
    },
}

COMPRIMI_ZIP = {
    "name": "comprimi_zip",
    "description": (
        "Crea un archivio .zip a partire da uno o più file/cartelle della sandbox. Usalo "
        "per 'comprimi questi file', 'fai uno zip della cartella X'. Le cartelle vengono "
        "incluse ricorsivamente. Sorgenti e destinazione stanno nella sandbox."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sorgenti": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Elenco di file/cartelle da comprimere (percorsi nella sandbox).",
            },
            "destinazione": {
                "type": "string",
                "description": "Percorso del file .zip da creare, nella sandbox (es. 'archivio.zip').",
            },
        },
        "required": ["sorgenti", "destinazione"],
    },
}

ESTRAI_ZIP = {
    "name": "estrai_zip",
    "description": (
        "Estrae il contenuto di un archivio .zip in una cartella della sandbox. Usalo per "
        "'estrai questo zip', 'scompatta l'archivio'. Se ometti la destinazione, estrae "
        "nella radice della sandbox. Per sicurezza, un archivio le cui voci uscirebbero "
        "dalla cartella di destinazione (zip-slip) viene rifiutato."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "archivio": {"type": "string", "description": "Percorso del file .zip da estrarre (nella sandbox)."},
            "destinazione": {
                "type": "string",
                "description": "Cartella di destinazione, nella sandbox. Default: radice della sandbox.",
            },
        },
        "required": ["archivio"],
    },
}


# Terne (schema, funzione, rischio). info/hash SAFE; zip CAUTION (scrivono file).
TOOLS = [
    (INFO_FILE, info_file, SAFE),
    (HASH_FILE, hash_file, SAFE),
    (COMPRIMI_ZIP, comprimi_zip, CAUTION),
    (ESTRAI_ZIP, estrai_zip, CAUTION),
]
