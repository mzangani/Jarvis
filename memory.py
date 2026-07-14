"""
memory.py — La memoria LUNGA (persistente) di Jarvis (Fase 5a).

Sta qui, in cima al progetto, perché è INFRASTRUTTURA come safety.py: le famiglie
di tool (tools/memory.py) ci si appoggiano, ma questo file non conosce l'API
Anthropic né i tool. Sa solo di SQLite.

Concetto chiave — due tipi di memoria:
  - memoria NEL CONTESTO: la cronologia (brain.Agent.messages). Sempre presente ma
    costosa (pesa in token a ogni chiamata) e limitata (finestra di contesto), e
    sparisce a fine sessione.
  - memoria RECUPERATA (questo file): fatti su DISCO. Illimitata e persistente, ma
    NON è nel contesto finché non la tiriamo dentro: o cercandola (richiama) o
    iniettandone un estratto nel system prompt all'avvio. È la versione minima del
    pattern RAG (Retrieval-Augmented Generation): tieni la conoscenza fuori e recuperi
    solo il pezzo pertinente.

Onestà sul recupero: nessuna dipendenza nuova => niente embeddings. Usiamo FTS5, il
full-text integrato in SQLite: è KEYWORD-matching (trova le parole), NON semantico
(non capisce i significati). Se il SQLite di sistema è compilato senza FTS5, ripieghiamo
su una ricerca LIKE più rozza e lo DICHIARIAMO apertamente (niente finto successo).

Perché un SINGLETON di modulo: brain.dispatch chiama funzione(**input) e non può
passare oggetti come l'handle del DB. Quindi la connessione vive qui, creata pigramente
alla prima chiamata, e le funzioni la recuperano da sole.
"""

import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from rich.console import Console

console = Console()


# --- Parametri ---------------------------------------------------------------
# Quanti fatti (i più recenti) iniettiamo nel system prompt all'avvio: un tetto per
# non riempire il contesto. Il resto resta recuperabile on-demand con 'richiama'.
_LIMITE_INIEZIONE = 30
# Quanti risultati al massimo restituisce 'richiama' per una ricerca.
_LIMITE_RICHIAMO = 10


# --- Stato del modulo (il SINGLETON) -----------------------------------------
# La connessione è unica per tutto il processo: la creiamo alla prima chiamata di
# _conn() e la riusiamo. _usa_fts5 registra quale motore di ricerca abbiamo (FTS5
# vero o ripiego LIKE), deciso una volta sola in fase di inizializzazione.
_conn_singleton: sqlite3.Connection | None = None
_usa_fts5: bool = False


def _percorso_db() -> Path:
    """
    Percorso del file SQLite della memoria.

    Configurabile con la variabile d'ambiente JARVIS_MEMORY (stesso stile di
    JARVIS_SANDBOX in safety.py); default: ~/Jarvis-Sandbox/jarvis_memory.db.
    """
    default = Path.home() / "Jarvis-Sandbox" / "jarvis_memory.db"
    return Path(os.environ.get("JARVIS_MEMORY", default)).expanduser()


def _fts5_disponibile(conn: sqlite3.Connection) -> bool:
    """
    True se questo SQLite sa creare tabelle FTS5.

    Lo verifichiamo creando e cancellando una tabella-sonda nello schema 'temp'
    (in memoria, non tocca il file del DB): se il modulo fts5 manca, SQLite solleva
    OperationalError ('no such module: fts5') e noi ripieghiamo su LIKE.
    """
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._jarvis_fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp._jarvis_fts5_probe")
        return True
    except sqlite3.OperationalError:
        # Solo questo errore significa "FTS5 assente": lo distinguiamo da altri guasti,
        # che invece devono risalire LOUD.
        return False


def _apri_e_inizializza() -> sqlite3.Connection:
    """
    Apre (creando cartella e file se mancano) la connessione al DB e prepara lo
    schema. Sceglie qui, una volta sola, FTS5 vero o ripiego LIKE, aggiornando il
    flag di modulo _usa_fts5.

    Fail loud: se la cartella non è scrivibile o il DB non si apre, l'eccezione
    (OSError / sqlite3.Error) risale al chiamante. NON aggiorniamo lo stato globale
    finché l'inizializzazione non è andata a buon fine.
    """
    global _usa_fts5

    percorso = _percorso_db()
    # Creiamo la cartella contenitore se manca (può sollevare OSError: LOUD).
    percorso.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(percorso)  # REPL a thread singolo: check_same_thread di default va bene

    if _fts5_disponibile(conn):
        # UNA sola tabella virtuale FTS5 fa sia da archivio sia da indice di ricerca:
        # niente trigger da sincronizzare. rowid è l'id implicito; creato_il è UNINDEXED
        # (lo conserviamo ma non serve cercarlo a testo).
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fatti "
            "USING fts5(testo, creato_il UNINDEXED)"
        )
        _usa_fts5 = True
    else:
        # Ripiego onesto: tabella normale + ricerca LIKE. rowid è alias di id, così il
        # resto del codice (dedup, ordinamenti) funziona identico nei due casi.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fatti ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  testo TEXT NOT NULL,"
            "  creato_il TEXT NOT NULL"
            ")"
        )
        _usa_fts5 = False
        # Avviso una tantum: l'utente ha diritto di sapere che il richiamo è più rozzo.
        console.print(
            "[yellow]⚠  Memoria: il tuo SQLite non ha FTS5; uso una ricerca LIKE "
            "(più rozza) per 'richiama'. I fatti si salvano comunque.[/]"
        )

    conn.commit()
    return conn


def _conn() -> sqlite3.Connection:
    """
    Restituisce la connessione singleton, creandola alla prima chiamata.

    Se l'inizializzazione fallisce, _conn_singleton resta None e la prossima chiamata
    riproverà (ri-sollevando l'errore): non "congeliamo" mai uno stato rotto.
    """
    global _conn_singleton
    if _conn_singleton is None:
        _conn_singleton = _apri_e_inizializza()
    return _conn_singleton


# --- API pubblica (usata da tools/memory.py e da brain.py) -------------------

def ricorda_fatto(testo: str) -> str:
    """
    Salva un fatto persistente. Ignora i duplicati esatti (stesso testo già presente)
    per non sporcare il DB. Fail loud su testo vuoto o errori del DB.
    """
    testo = (testo or "").strip()
    if not testo:
        # Niente da ricordare: errore esplicito, non un finto "ok".
        raise ValueError("Fatto vuoto: non c'è niente da ricordare.")

    conn = _conn()

    # Dedup esatto: 'testo = ?' è un confronto letterale (in FTS5 SQLite applica il
    # filtro con una scansione, corretto anche se il vtab non lo "consuma"). rowid
    # funziona sia in FTS5 (implicito) sia nella tabella normale (alias di id).
    gia_presente = conn.execute(
        "SELECT rowid FROM fatti WHERE testo = ? LIMIT 1", (testo,)
    ).fetchone()
    if gia_presente is not None:
        return f"Lo sapevo già: «{testo}». Non l'ho duplicato."

    creato_il = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO fatti(testo, creato_il) VALUES (?, ?)", (testo, creato_il)
    )
    conn.commit()  # sqlite3 non è in autocommit: senza commit il fatto non è persistito
    return f"Memorizzato: «{testo}»."


def _prepara_query_fts5(query: str) -> str | None:
    """
    Trasforma una domanda in linguaggio naturale in una query FTS5 sicura.

    Estraiamo le PAROLE (anche accentate) e le mettiamo in OR come termini letterali
    tra virgolette. Due motivi:
      - OR (non AND): "dove sta il mio progetto?" deve trovare "il mio progetto sta
        in D:\\dev" anche se non contiene "dove". Con l'AND implicito di FTS5 fallirebbe.
      - virgolette: neutralizzano i caratteri speciali di FTS5 (", *, -, :, parentesi,
        NEAR...) che altrimenti darebbero un errore di sintassi.

    Ritorna None se non c'è nessuna parola utile (query di sola punteggiatura).
    """
    parole = re.findall(r"\w+", query, flags=re.UNICODE)
    if not parole:
        return None
    return " OR ".join(f'"{p}"' for p in parole)


def richiama_fatti(query: str, limite: int = _LIMITE_RICHIAMO) -> str:
    """
    Cerca nella memoria i fatti che corrispondono a `query` (keyword match) e li
    restituisce come testo leggibile. Fail loud su query vuota o errori del DB.

    Con FTS5 ordiniamo per pertinenza (rank bm25); nel ripiego LIKE, per data
    (più recenti prima), perché LIKE non dà un punteggio di rilevanza.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("Query vuota: dimmi cosa cercare nella memoria.")

    conn = _conn()

    if _usa_fts5:
        match = _prepara_query_fts5(query)
        if match is None:
            return f"Nessun fatto memorizzato corrisponde a «{query}»."
        righe = conn.execute(
            "SELECT testo, creato_il FROM fatti WHERE fatti MATCH ? "
            "ORDER BY rank LIMIT ?",
            (match, limite),
        ).fetchall()
    else:
        # Ripiego: sottostringa case-insensitive. Più rozzo (nessun ranking), onesto.
        righe = conn.execute(
            "SELECT testo, creato_il FROM fatti WHERE testo LIKE ? "
            "ORDER BY rowid DESC LIMIT ?",
            (f"%{query}%", limite),
        ).fetchall()

    if not righe:
        return f"Nessun fatto memorizzato corrisponde a «{query}»."
    elenco = "\n".join(f"- {testo}  (memorizzato il {creato})" for testo, creato in righe)
    return f"Fatti trovati per «{query}»:\n{elenco}"


def fatti_recenti(limite: int = _LIMITE_INIEZIONE) -> list[str]:
    """
    Restituisce i `limite` fatti più recenti, in ordine cronologico (dal più vecchio
    al più recente tra quelli selezionati). Serve a brain.py per iniettarli nel
    system prompt all'avvio, così Jarvis parte già "sapendo".
    """
    conn = _conn()
    righe = conn.execute(
        "SELECT testo FROM fatti ORDER BY rowid DESC LIMIT ?", (limite,)
    ).fetchall()
    # Prendiamo i più recenti (rowid DESC), poi invertiamo per leggerli in ordine
    # cronologico nel blocco del prompt.
    return [r[0] for r in reversed(righe)]
