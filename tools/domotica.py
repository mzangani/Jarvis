"""
tools/domotica.py — Famiglia "Domotica": impianti BTicino/Legrand SCS via OpenWebNet.

Gli impianti MyHome (bus SCS) espongono un gateway IP (MyHomeServer1, F454, MH202…)
che parla OPENWEBNET: un protocollo TESTUALE su TCP, porta 20000. I comandi sono
cornici tipo `*CHI*COSA*DOVE##`:

    *1*1*12##    → accendi la luce al punto 12      (CHI=1 luci, COSA=1 ON)
    *2*2*7##     → abbassa la tapparella al punto 7 (CHI=2 automazione, COSA=2 GIÙ)

Perché NATIVO e non MCP/Home Assistant: il protocollo è così semplice che basta un
socket di LIBRERIA STANDARD — zero dipendenze (il vincolo del progetto regge), zero
servizi in mezzo. E la logica di protocollo è in FUNZIONI PURE, testabili senza
impianto (nei test c'è anche un finto gateway su localhost).

Configurazione (in .env):
  JARVIS_SCS_HOST     = IP del gateway sulla tua rete (obbligatoria per usare i tool)
  JARVIS_SCS_PORT     = porta (default 20000)
  JARVIS_SCS_PASSWORD = password OPEN numerica, se il gateway la chiede (spesso basta
                        mettere l'IP del Mac tra gli "IP abilitati" del gateway)
  JARVIS_SCS_PUNTI    = mappa nomi→indirizzi SCS, es. "cucina=12, salotto=25, tapparella camera=7"

Sicurezza e rischi:
- L'host è FISSO dalla configurazione (non lo sceglie il modello) e gli indirizzi dei
  punti sono validati numerici: il modello non può farci aprire connessioni arbitrarie.
  (Il guardiano anti-SSRF del web non c'entra: qui la LAN è proprio la destinazione.)
- luce/tapparella sono SAFE: azioni fisiche ma benigne e REVERSIBILI, chieste a voce
  («accendi la luce») — una conferma a tastiera a ogni interruttore renderebbe la
  modalità vocale inutilizzabile. Scenari/allarmi NON sono esposti, deliberatamente.
- Autenticazione: nessuna password o password OPEN numerica. I gateway più recenti che
  impongono HMAC non sono ancora supportati: in quel caso il messaggio d'errore lo dice
  (soluzione tipica: abilitare l'IP del Mac nel gateway, che salta la password).
"""

import os
import re
import socket

from safety import SAFE

_TIMEOUT = 5  # secondi: un gateway in LAN risponde subito o mai

# Cornici speciali del protocollo.
ACK = "*#*1##"
NACK = "*#*0##"
_SESSIONE_COMANDI = "*99*0##"


# ============================================================================
# PROTOCOLLO PURO (nessuna rete): componi/interpreta cornici. Testabile a secco.
# ============================================================================
def valida_punto(dove: str) -> str:
    """
    Valida un indirizzo SCS ("dove"): SOLO cifre (es. '12', '0' = generale, '01').
    Fail closed: qualunque altra cosa è respinta prima di toccare il bus.
    """
    dove = str(dove).strip()
    if not re.fullmatch(r"\d{1,4}", dove):
        raise ValueError(f"Indirizzo SCS non valido: {dove!r} (attese solo cifre, es. '12').")
    return dove


def cornice_luce(dove: str, accendi: bool) -> str:
    """La cornice OpenWebNet per accendere/spegnere una luce (CHI=1)."""
    return f"*1*{1 if accendi else 0}*{valida_punto(dove)}##"


def cornice_tapparella(dove: str, azione: str) -> str:
    """La cornice per una tapparella (CHI=2): su=1, giu=2, stop=0."""
    codici = {"stop": 0, "su": 1, "giu": 2}
    if azione not in codici:
        raise ValueError(f"Azione tapparella non valida: {azione!r} (su/giu/stop).")
    return f"*2*{codici[azione]}*{valida_punto(dove)}##"


def cornice_stato_luce(dove: str) -> str:
    """La cornice di RICHIESTA STATO di una luce (*#CHI*DOVE##)."""
    return f"*#1*{valida_punto(dove)}##"


def interpreta_stato_luce(cornici: list[str], dove: str) -> str | None:
    """
    Cerca nelle cornici di risposta lo stato della luce `dove`: '*1*1*12##' = accesa,
    '*1*0*12##' = spenta. None se non c'è (il modello dirà onestamente che non sa).
    """
    dove = valida_punto(dove)
    for c in cornici:
        m = re.fullmatch(r"\*1\*(\d+)\*0*" + re.escape(dove.lstrip("0") or "0") + r"##", c)
        if m:
            return "accesa" if m.group(1) != "0" else "spenta"
    return None


def calcola_password_open(password: int, nonce: str) -> int:
    """
    L'algoritmo di autenticazione "OPEN password" dei gateway BTicino: dal nonce
    numerico inviato dal gateway e dalla password numerica dell'utente calcola la
    risposta attesa. È l'algoritmo pubblico storico (documentazione OpenWebNet);
    funzione PURA, testata a secco. ⚠️ Da validare anche sull'impianto reale.
    """
    num1 = 0
    num2 = 0
    pwd = int(password) & 0xFFFFFFFF
    iniziato = False
    for c in nonce:
        num2 &= 0xFFFFFFFF
        if c == "0":
            num1 = num2
        elif c == "1":
            if not iniziato:
                num2 = pwd
            iniziato = True
            num1 = (num2 & 0xFFFFFF80) >> 7
            num1 += (num2 << 25) & 0xFFFFFFFF
        elif c == "2":
            if not iniziato:
                num2 = pwd
            iniziato = True
            num1 = (num2 & 0xFFFFFFF0) >> 4
            num1 += (num2 << 28) & 0xFFFFFFFF
        elif c == "3":
            if not iniziato:
                num2 = pwd
            iniziato = True
            num1 = (num2 & 0xFFFFFFF8) >> 3
            num1 += (num2 << 29) & 0xFFFFFFFF
        elif c == "4":
            if not iniziato:
                num2 = pwd
            iniziato = True
            num1 = (num2 << 1) & 0xFFFFFFFF
            num1 += num2 >> 31
        elif c == "5":
            if not iniziato:
                num2 = pwd
            iniziato = True
            num1 = (num2 << 5) & 0xFFFFFFFF
            num1 += num2 >> 27
        elif c == "6":
            if not iniziato:
                num2 = pwd
            iniziato = True
            num1 = (num2 << 12) & 0xFFFFFFFF
            num1 += num2 >> 20
        elif c == "7":
            if not iniziato:
                num2 = pwd
            iniziato = True
            num1 = (num2 & 0x0000FF00)
            num1 += (num2 & 0x000000FF) << 24
            num1 += (num2 & 0x00FF0000) >> 16
            num1 += (num2 & 0xFF000000) >> 8
        elif c == "8":
            if not iniziato:
                num2 = pwd
            iniziato = True
            num1 = (num2 & 0x0000FFFF) << 16
            num1 += num2 >> 24
            num1 += (num2 & 0x00FF0000) >> 8
        elif c == "9":
            if not iniziato:
                num2 = pwd
            iniziato = True
            num1 = (~num2) & 0xFFFFFFFF
        else:
            raise ValueError(f"Nonce OPEN non numerico: {nonce!r}")
        num1 &= 0xFFFFFFFF
        num2 = num1
    return num1


def leggi_punti(grezzo: str) -> dict[str, str]:
    """
    Interpreta JARVIS_SCS_PUNTI ("cucina=12, salotto=25") in {nome: indirizzo}.
    Nomi in minuscolo (il confronto è case-insensitive), indirizzi validati. PURA.
    """
    punti: dict[str, str] = {}
    for pezzo in (grezzo or "").split(","):
        pezzo = pezzo.strip()
        if not pezzo:
            continue
        if "=" not in pezzo:
            raise ValueError(f"Voce di JARVIS_SCS_PUNTI non valida: {pezzo!r} (atteso nome=numero).")
        nome, dove = pezzo.split("=", 1)
        nome = nome.strip().lower()
        if not nome:
            raise ValueError(f"Nome vuoto in JARVIS_SCS_PUNTI: {pezzo!r}.")
        punti[nome] = valida_punto(dove)
    return punti


def risolvi_punto(punto: str) -> str:
    """
    Da ciò che dice l'utente all'indirizzo SCS: un numero passa com'è; un NOME viene
    cercato nella mappa JARVIS_SCS_PUNTI. Sconosciuto -> errore con i nomi noti.
    """
    punto = str(punto).strip()
    if re.fullmatch(r"\d{1,4}", punto):
        return punto
    punti = leggi_punti(os.environ.get("JARVIS_SCS_PUNTI", ""))
    nome = punto.lower()
    if nome in punti:
        return punti[nome]
    noti = ", ".join(sorted(punti)) or "(nessuno configurato)"
    raise ValueError(
        f"Punto sconosciuto: {punto!r}. Nomi configurati: {noti}. "
        "Puoi usare direttamente l'indirizzo numerico SCS, o aggiungere il nome "
        "in JARVIS_SCS_PUNTI nel file .env."
    )


# ============================================================================
# TRASPORTO: la connessione vera al gateway (sottile: tutta la logica è sopra).
# ============================================================================
class _LettoreCornici:
    """
    Lettore BUFFERIZZATO di cornici: TCP è un flusso, non un telegrafo — più cornici
    possono arrivare incollate in una sola lettura (`*1*1*12##*#*1##`) o una cornice
    spezzata in due. Accumuliamo in un buffer e restituiamo UNA cornice alla volta,
    tagliando al primo '##'. (Il primo collaudo col finto gateway ha beccato proprio
    la coalescenza: senza buffer, stato+ACK diventavano una cornice sola.)
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buffer = b""

    def cornice(self) -> str:
        while b"##" not in self._buffer:
            pezzo = self._sock.recv(256)
            if not pezzo:
                raise RuntimeError("Il gateway ha chiuso la connessione a metà dialogo.")
            self._buffer += pezzo
        taglio = self._buffer.index(b"##") + 2
        grezza, self._buffer = self._buffer[:taglio], self._buffer[taglio:]
        return grezza.decode("ascii", errors="replace")


def _apri_sessione_comandi(sock: socket.socket, lettore: "_LettoreCornici") -> None:
    """Handshake OpenWebNet: saluto, richiesta sessione comandi, eventuale password OPEN."""
    if lettore.cornice() != ACK:
        raise RuntimeError("Il gateway non ha salutato con ACK: non sembra OpenWebNet.")
    sock.sendall(_SESSIONE_COMANDI.encode("ascii"))
    risposta = lettore.cornice()
    if risposta == ACK:
        return  # nessuna password richiesta (IP abilitato nel gateway): pronti
    if risposta.startswith("*98*"):
        raise RuntimeError(
            "Il gateway richiede l'autenticazione HMAC, non ancora supportata. "
            "Soluzione tipica: abilita l'IP di questo computer tra gli 'IP abilitati' "
            "nella configurazione del gateway (salta la password)."
        )
    m = re.fullmatch(r"\*#(\d+)##", risposta)
    if not m:
        raise RuntimeError(f"Risposta inattesa dal gateway all'apertura: {risposta!r}")
    # Password OPEN numerica.
    grezza = os.environ.get("JARVIS_SCS_PASSWORD", "").strip()
    if not grezza:
        raise RuntimeError(
            "Il gateway chiede una password OPEN ma JARVIS_SCS_PASSWORD non è impostata."
        )
    if not grezza.isdigit():
        raise RuntimeError("JARVIS_SCS_PASSWORD deve essere NUMERICA (password OPEN).")
    attesa = calcola_password_open(int(grezza), m.group(1))
    sock.sendall(f"*#{attesa}##".encode("ascii"))
    if lettore.cornice() != ACK:
        raise RuntimeError("Password OPEN rifiutata dal gateway: controlla JARVIS_SCS_PASSWORD.")


def _invia(cornice: str, attendi_risposte: bool = False) -> list[str]:
    """
    Apre la connessione, fa l'handshake, invia UNA cornice e ritorna le cornici di
    risposta fino all'ACK/NACK (NACK -> errore loud). Una connessione per comando:
    semplice e robusto, e per l'uso conversazionale il costo è irrilevante.
    """
    host = os.environ.get("JARVIS_SCS_HOST", "").strip()
    if not host:
        raise RuntimeError(
            "Domotica non configurata: imposta JARVIS_SCS_HOST (IP del gateway SCS) in .env."
        )
    porta = int(os.environ.get("JARVIS_SCS_PORT", "20000"))
    with socket.create_connection((host, porta), timeout=_TIMEOUT) as sock:
        sock.settimeout(_TIMEOUT)
        lettore = _LettoreCornici(sock)
        _apri_sessione_comandi(sock, lettore)
        sock.sendall(cornice.encode("ascii"))
        risposte: list[str] = []
        while True:
            r = lettore.cornice()
            if r == ACK:
                return risposte
            if r == NACK:
                raise RuntimeError(
                    f"Il gateway ha rifiutato il comando ({cornice}): indirizzo inesistente?"
                )
            # Cornici di risposta (es. lo stato di una luce) prima dell'esito: le
            # accumuliamo sempre, chi chiama decide cosa farne (attendi_risposte è
            # documentazione dell'intento; la lettura è identica e robusta).
            risposte.append(r)


# ============================================================================
# I TOOL
# ============================================================================
def luce(punto: str, azione: str) -> str:
    """Accende o spegne una luce ('accendi'/'spegni'). punto = nome configurato o numero."""
    azione = (azione or "").strip().lower()
    if azione not in ("accendi", "spegni"):
        raise ValueError(f"Azione luce non valida: {azione!r} (accendi/spegni).")
    dove = risolvi_punto(punto)
    _invia(cornice_luce(dove, azione == "accendi"))
    dove_txt = f"{punto} (punto {dove})" if str(punto) != dove else f"punto {dove}"
    return f"Luce {dove_txt}: {'accesa' if azione == 'accendi' else 'spenta'}."


def tapparella(punto: str, azione: str) -> str:
    """Muove una tapparella: 'su', 'giu' o 'stop'."""
    azione = (azione or "").strip().lower()
    dove = risolvi_punto(punto)
    _invia(cornice_tapparella(dove, azione))
    return f"Tapparella {punto}: {azione}."


def stato_luce(punto: str) -> str:
    """Legge lo stato (accesa/spenta) di una luce dal bus."""
    dove = risolvi_punto(punto)
    risposte = _invia(cornice_stato_luce(dove), attendi_risposte=True)
    stato = interpreta_stato_luce(risposte, dove)
    if stato is None:
        return f"Il gateway non ha riportato lo stato del punto {dove} (risposte: {risposte})."
    return f"La luce {punto} è {stato}."


def punti_scs() -> str:
    """Elenca i punti configurati (nome → indirizzo) in JARVIS_SCS_PUNTI."""
    punti = leggi_punti(os.environ.get("JARVIS_SCS_PUNTI", ""))
    if not punti:
        return (
            "Nessun punto configurato. Aggiungi in .env, per esempio: "
            'JARVIS_SCS_PUNTI="cucina=12, salotto=25, tapparella camera=7"'
        )
    righe = [f"- {nome} → {dove}" for nome, dove in sorted(punti.items())]
    return "Punti SCS configurati:\n" + "\n".join(righe)


# --- Schemi per il modello ---------------------------------------------------
_NOTA_PUNTO = (
    "Il 'punto' è un nome configurato (vedi punti_scs, es. 'cucina') oppure direttamente "
    "l'indirizzo numerico SCS (es. '12'). '0' comanda TUTTI i punti (generale)."
)

LUCE = {
    "name": "luce",
    "description": (
        "Accende o spegne una LUCE dell'impianto domotico BTicino/SCS di casa. Usalo per "
        "«accendi la luce in cucina», «spegni tutte le luci» (punto '0' = generale). "
        + _NOTA_PUNTO + " Se l'utente usa un nome che non conosci, chiama prima punti_scs."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "punto": {"type": "string", "description": "Nome configurato o indirizzo numerico."},
            "azione": {"type": "string", "enum": ["accendi", "spegni"]},
        },
        "required": ["punto", "azione"],
    },
}

TAPPARELLA = {
    "name": "tapparella",
    "description": (
        "Muove una TAPPARELLA/persiana dell'impianto BTicino/SCS: 'su', 'giu' o 'stop'. "
        + _NOTA_PUNTO
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "punto": {"type": "string", "description": "Nome configurato o indirizzo numerico."},
            "azione": {"type": "string", "enum": ["su", "giu", "stop"]},
        },
        "required": ["punto", "azione"],
    },
}

STATO_LUCE = {
    "name": "stato_luce",
    "description": (
        "Legge dal bus SCS se una luce è ACCESA o SPENTA. Usalo per «è accesa la luce in "
        "cucina?», «ho lasciato luci accese?» (interrogando i punti configurati). " + _NOTA_PUNTO
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "punto": {"type": "string", "description": "Nome configurato o indirizzo numerico."},
        },
        "required": ["punto"],
    },
}

PUNTI_SCS = {
    "name": "punti_scs",
    "description": (
        "Elenca i punti domotici configurati (nome → indirizzo SCS). Chiamalo quando "
        "l'utente nomina una stanza/luce che non conosci, o chiede cosa può comandare."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

# Tutti SAFE: azioni fisiche benigne e reversibili, chieste a voce (vedi il docstring
# del modulo per il razionale). Scenari e funzioni d'allarme NON sono esposti.
TOOLS = [
    (LUCE, luce, SAFE),
    (TAPPARELLA, tapparella, SAFE),
    (STATO_LUCE, stato_luce, SAFE),
    (PUNTI_SCS, punti_scs, SAFE),
]
