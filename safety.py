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

import ipaddress
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

from rich.console import Console

console = Console()


# --- Livelli di rischio ------------------------------------------------------
# Classifichiamo ogni tool in uno di questi tre livelli.
SAFE = "SAFE"            # sola lettura locale, nessun effetto collaterale (es. get_system_info)
CAUTION = "CAUTION"      # effetto contenuto/reversibile: scrive file, apre app, legge una pagina web (GET con guardiano SSRF)
DANGEROUS = "DANGEROUS"  # cancella, esegue comandi shell arbitrari, termina processi
# Nota sulla RETE: una GET in sola lettura passata da ensure_url_sicuro() è CAUTION
# (nessun effetto distruttivo, host interni bloccati). Resterebbe DANGEROUS solo una
# rete che SCRIVE/scarica su disco: non c'è ancora, la aggiungeremo con quel rischio.


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


# --- GUARDIANO DI RETE (anti-SSRF) -------------------------------------------
# La rete è una superficie nuova e insidiosa. Un URL apparentemente innocuo può
# puntare a servizi INTERNI alla macchina o alla rete locale (localhost, un
# database, il router) o all'endpoint metadata dei cloud (169.254.169.254), che
# spesso espone credenziali. Sfruttare l'agente per raggiungerli si chiama SSRF.
#
# `ensure_url_sicuro` è il guardiano che i tool web chiamano come PRIMA riga (ed è
# esposto anche come pre-check, così scatta prima della conferma), esattamente
# come `ensure_in_sandbox` fa per i file.

# Schemi ammessi: solo il web "vero". Niente file://, ftp://, gopher://, data://...
_SCHEMI_WEB = {"http", "https"}


def _ip_e_vietato(ip: str) -> bool:
    """
    True se `ip` NON è un indirizzo pubblico instradabile: loopback, rete privata,
    link-local (incluso l'endpoint metadata 169.254.169.254), riservato, ecc.

    Ragioniamo sull'IP, non sul nome host, perché il nome è ingannevole:
    'localhost', '127.0.0.1', '2130706433' (127.0.0.1 in decimale) e un dominio
    che PUNTA a un IP privato sono tutti modi per colpire la stessa rete interna.
    """
    try:
        # split('%'): togliamo l'eventuale scope-id degli IPv6 link-local (fe80::1%eth0).
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        # Fail closed: un indirizzo che non sappiamo nemmeno interpretare lo blocchiamo.
        return True

    # Un IPv6 può "mappare" un IPv4 (es. ::ffff:127.0.0.1): controlliamo l'IPv4
    # sottostante, perché non tutte le versioni di Python propagano is_loopback &
    # co. all'indirizzo mappato — sarebbe una scorciatoia per aggirarci.
    mappato = getattr(addr, "ipv4_mapped", None)
    if mappato is not None:
        addr = mappato

    return (
        addr.is_loopback        # 127.0.0.0/8, ::1
        or addr.is_private      # 10/8, 172.16/12, 192.168/16, fc00::/7 ...
        or addr.is_link_local   # 169.254.0.0/16 (incl. metadata), fe80::/10
        or addr.is_reserved     # range IETF riservati
        or addr.is_unspecified  # 0.0.0.0, ::
        or addr.is_multicast    # 224.0.0.0/4, ff00::/8
    )


def ensure_url_sicuro(url: str) -> str:
    """
    Verifica che `url` sia sicuro da recuperare e ne restituisce la versione pulita.

    - Blocca (PermissionError) gli schemi diversi da http/https e gli host locali
      o di rete privata (anti-SSRF).
    - Solleva RuntimeError se l'host non risolve: niente finto successo.

    Da chiamare come PRIMA riga dei tool web e come pre-check nel loop (così un URL
    vietato è respinto senza nemmeno mostrare la conferma).

    Nota onesta (TOCTOU): risolviamo il nome QUI, e urllib lo ri-risolverà al
    momento della richiesta; in teoria un DNS malevolo potrebbe rispondere in modo
    diverso tra i due istanti (DNS rebinding). Blindarlo del tutto richiederebbe di
    "inchiodare" l'IP dentro la connessione: fuori scopo qui. Il controllo copre
    comunque tutti i casi concreti (schemi, IP letterali, nomi che puntano a reti interne).
    """
    if not url or not url.strip():
        raise ValueError("URL vuoto: non c'è niente da recuperare.")
    url = url.strip()

    parsed = urlparse(url)

    # 1. Schema: solo http/https.
    if parsed.scheme.lower() not in _SCHEMI_WEB:
        raise PermissionError(
            f"Schema URL non consentito: '{parsed.scheme or '(nessuno)'}'. "
            "Sono ammessi solo http e https (rifiutati file, ftp, gopher, data, ...)."
        )

    # 2. Deve esserci un host.
    host = parsed.hostname  # già senza porta né eventuale 'utente:password@'
    if not host:
        raise PermissionError(f"URL senza host valido: {url!r}")

    # 3. Risolviamo l'host a TUTTI i suoi IP e controlliamo OGNUNO: un solo IP
    #    privato basta a rifiutare, così un nome con più record non può "nascondere"
    #    un indirizzo interno dietro uno pubblico.
    try:
        info = socket.getaddrinfo(host, parsed.port or None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        # Host che non risolve (o DNS non disponibile): fail loud, non silenzioso.
        raise RuntimeError(f"Impossibile risolvere l'host '{host}': {e}")

    for record in info:
        ip = record[4][0]  # sockaddr: (ip, porta[, flowinfo, scope_id])
        if _ip_e_vietato(ip):
            raise PermissionError(
                f"Host non consentito: '{host}' risolve a un indirizzo privato o "
                f"locale ({ip}). Per prevenire attacchi SSRF sono bloccati localhost, "
                "le reti private e l'endpoint metadata (169.254.169.254)."
            )

    return url


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
