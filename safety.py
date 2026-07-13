"""
safety.py — Il livello di sicurezza di Jarvis (Fase 3).

Questo modulo è scritto PRIMA dei tool potenti (file, shell, rete): è il
"guardiano" attraverso cui ogni azione rischiosa deve passare. Fornisce:

  - i tre livelli di rischio (SAFE / CAUTION / DANGEROUS)
  - una SANDBOX_ROOT: le operazioni su file restano confinate a una cartella
  - una blacklist di pattern SEMPRE rifiutati (rm -rf /, sudo, mkfs, ...)
  - confirm(): mostra ESATTAMENTE cosa sta per accadere e chiede conferma

Nota progettuale: qui è lecito che il codice parli con l'utente (confirm),
perché una conferma di sicurezza è un cancello, non "interfaccia" dell'app.
"""

import os
import re
from pathlib import Path

from rich.console import Console

console = Console()


# --- Livelli di rischio ------------------------------------------------------
# Classifichiamo ogni tool in uno di questi tre livelli.
SAFE = "SAFE"            # sola lettura, nessun effetto collaterale (es. get_system_info)
CAUTION = "CAUTION"      # modifica qualcosa: scrive file, apre applicazioni
DANGEROUS = "DANGEROUS"  # cancella, esegue comandi shell arbitrari, usa la rete


# --- SANDBOX -----------------------------------------------------------------
# Le operazioni su file sono confinate a questa cartella, salvo autorizzazione
# esplicita. È configurabile con la variabile d'ambiente JARVIS_SANDBOX.
SANDBOX_ROOT = Path(os.environ.get("JARVIS_SANDBOX", Path.home() / "Jarvis-Sandbox"))


def sandbox_root() -> Path:
    """Restituisce la cartella sandbox, creandola se non esiste ancora."""
    SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
    return SANDBOX_ROOT


def ensure_in_sandbox(path: str) -> Path:
    """
    Risolve `path` e verifica che stia DENTRO la sandbox.

    - Se il percorso è relativo, lo interpretiamo rispetto alla sandbox.
    - Usiamo .resolve() per sciogliere '..' e i collegamenti (symlink):
      è così che impediamo le evasioni tipo '../../etc/passwd'.

    Solleva PermissionError se il percorso finale esce dalla sandbox.
    Da usare nei tool sui file (Fase 4).
    """
    root = sandbox_root().resolve()
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = root / p
    p = p.resolve()  # <- scioglie '..' e symlink: niente scorciatoie fuori dalla sandbox

    if not p.is_relative_to(root):
        raise PermissionError(
            f"Percorso fuori dalla sandbox: {p}\nLa sandbox consentita è: {root}"
        )
    return p


# --- BLACKLIST ---------------------------------------------------------------
# Pattern SEMPRE rifiutati, a prescindere da qualunque conferma: sono azioni
# catastrofiche o palesemente pericolose che non vogliamo permettere mai.
_BLACKLIST = [
    (r"\brm\s+-[rf]{1,2}\s+/(?:\s|$)", "cancellazione della radice del filesystem"),
    (r"\brm\s+-[rf]{1,2}\s+~", "cancellazione della cartella utente"),
    (r"\bmkfs\b", "formattazione di un disco"),
    (r"\bformat\s+[a-z]:", "formattazione di un disco (Windows)"),
    (r"\bdiskpart\b", "gestione partizioni del disco"),
    (r"\bdd\b.*\bof=/dev/", "scrittura diretta su disco (dd)"),
    (r">\s*/dev/sd[a-z]", "scrittura diretta su disco"),
    (r"\bsudo\b", "esecuzione con privilegi di amministratore (sudo)"),
    (r"(^|\s)su\s", "cambio utente (su)"),
    (r"\breg\s+(add|delete)\b", "modifica del registro di sistema (Windows)"),
    (r"\bregedit\b", "editor del registro di sistema (Windows)"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "spegnimento o riavvio del sistema"),
]
# Compiliamo le espressioni regolari una sola volta (case-insensitive).
_BLACKLIST = [(re.compile(pattern, re.IGNORECASE), desc) for pattern, desc in _BLACKLIST]


def viola_blacklist(testo: str) -> str | None:
    """Se `testo` contiene un pattern proibito ne restituisce la descrizione, altrimenti None."""
    for regex, descrizione in _BLACKLIST:
        if regex.search(testo):
            return descrizione
    return None


def check_blacklist(testo: str) -> None:
    """
    Solleva PermissionError se `testo` viola la blacklist.
    Da chiamare dentro i tool pericolosi (es. shell) sull'input del modello.
    """
    motivo = viola_blacklist(testo)
    if motivo is not None:
        raise PermissionError(f"Azione vietata dalla blacklist: {motivo}")


# --- CONFERMA ----------------------------------------------------------------
def confirm(nome_tool: str, tool_input: dict, rischio: str) -> bool:
    """
    Mostra ESATTAMENTE cosa sta per fare Jarvis e chiede conferma esplicita.

    Ritorna True solo se l'utente acconsente. Il default è NO: un semplice invio
    (risposta vuota) equivale a rifiutare. Meglio negare per errore che eseguire
    per errore.
    """
    colore = "yellow" if rischio == CAUTION else "red"

    console.print(f"\n[{colore}]⚠  Jarvis vuole eseguire un'azione [{rischio}][/]")
    console.print(f"   tool: [bold]{nome_tool}[/]")
    # Stampiamo ogni argomento (percorso, comando, ...) così l'utente vede
    # esattamente cosa succederà, senza sorprese.
    for chiave, valore in tool_input.items():
        console.print(f"   {chiave}: [bold]{valore}[/]")

    risposta = console.input(f"[{colore}]Confermi? [s/N] [/]").strip().lower()
    return risposta in {"s", "si", "sì", "y", "yes"}
