"""
test_domotica.py — Test della famiglia DOMOTICA (BTicino/SCS, OpenWebNet), senza impianto.

Colaudiamo: le funzioni PURE di protocollo (cornici, parsing dello stato, mappa punti,
algoritmo password OPEN) e — con un FINTO GATEWAY su localhost che parla OpenWebNet —
il dialogo completo: handshake, sessione comandi, autenticazione, invio cornice, ACK.
La validazione sull'impianto VERO resta da fare a casa (come l'audio per la voce).

Include anche i controlli delle famiglie Mac/Calendario: in questo ambiente (Linux)
devono fallire LOUD con il messaggio "solo su macOS", mai in modo oscuro.
"""
import os
import socket
import tempfile
import threading
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="jarvis-dom-"))
os.environ["JARVIS_SANDBOX"] = str(_TMP / "sandbox")
os.environ["JARVIS_MEMORY"] = str(_TMP / "memoria.db")
os.environ["JARVIS_LOG"] = str(_TMP / "jarvis.jsonl")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-fake")

from tools import domotica

FALLITI = []


def check(nome, condizione, dettaglio=""):
    esito = "PASS" if condizione else "FAIL"
    if not condizione:
        FALLITI.append(nome)
    print(f"[{esito}] {nome}" + (f" — {dettaglio}" if dettaglio else ""))


# ============================================================================
# 1) Cornici di protocollo (pure)
# ============================================================================
print("=== 1. cornici OpenWebNet ===")
check("luce ON", domotica.cornice_luce("12", True) == "*1*1*12##")
check("luce OFF", domotica.cornice_luce("12", False) == "*1*0*12##")
check("generale (0)", domotica.cornice_luce("0", True) == "*1*1*0##")
check("tapparella su", domotica.cornice_tapparella("7", "su") == "*2*1*7##")
check("tapparella giu", domotica.cornice_tapparella("7", "giu") == "*2*2*7##")
check("tapparella stop", domotica.cornice_tapparella("7", "stop") == "*2*0*7##")
check("richiesta stato", domotica.cornice_stato_luce("12") == "*#1*12##")
try:
    domotica.cornice_luce("12; rm", True)
    check("indirizzo non numerico respinto", False, "non ha sollevato")
except ValueError:
    check("indirizzo non numerico respinto", True)

check("stato: accesa", domotica.interpreta_stato_luce(["*1*1*12##"], "12") == "accesa")
check("stato: spenta", domotica.interpreta_stato_luce(["*1*0*12##"], "12") == "spenta")
check("stato: assente -> None", domotica.interpreta_stato_luce(["*1*1*99##"], "12") is None)

# ============================================================================
# 2) Mappa dei punti (JARVIS_SCS_PUNTI) e risoluzione nome→indirizzo
# ============================================================================
print("\n=== 2. mappa punti ===")
punti = domotica.leggi_punti("cucina=12, Salotto=25, tapparella camera=7")
check("parsing mappa", punti == {"cucina": "12", "salotto": "25", "tapparella camera": "7"},
      f"-> {punti}")
os.environ["JARVIS_SCS_PUNTI"] = "cucina=12, salotto=25"
check("risolve un nome", domotica.risolvi_punto("Cucina") == "12")
check("un numero passa com'è", domotica.risolvi_punto("31") == "31")
try:
    domotica.risolvi_punto("bagno")
    check("nome ignoto -> errore coi nomi noti", False, "non ha sollevato")
except ValueError as e:
    check("nome ignoto -> errore coi nomi noti", "cucina" in str(e), f"-> {e}")
try:
    domotica.leggi_punti("cucina")
    check("mappa malformata respinta", False, "non ha sollevato")
except ValueError:
    check("mappa malformata respinta", True)

# ============================================================================
# 3) Password OPEN (pura): deterministica, nel range, sensibile a pwd e nonce
# ============================================================================
print("\n=== 3. algoritmo password OPEN ===")
r1 = domotica.calcola_password_open(12345, "603356072")
r2 = domotica.calcola_password_open(12345, "603356072")
r3 = domotica.calcola_password_open(12345, "603356073")
r4 = domotica.calcola_password_open(12346, "603356072")
check("deterministica", r1 == r2, f"-> {r1}")
check("32 bit", 0 <= r1 < 2**32)
check("dipende dal nonce", r1 != r3)
check("dipende dalla password", r1 != r4)
try:
    domotica.calcola_password_open(12345, "60x")
    check("nonce non numerico respinto", False, "non ha sollevato")
except ValueError:
    check("nonce non numerico respinto", True)

# ============================================================================
# FINTO GATEWAY: un server socket che parla OpenWebNet (handshake vero).
# ============================================================================
class FintoGateway(threading.Thread):
    """
    Accetta UNA connessione e recita il gateway: ACK di saluto, sessione comandi
    (con o senza password OPEN), poi registra la cornice ricevuta e risponde ACK
    (o le cornici di stato). La verifica della password usa la STESSA funzione
    pura del client: qui testiamo il FLUSSO, l'algoritmo va validato in casa.
    """

    def __init__(self, password=None, risposte_extra=()):
        super().__init__(daemon=True)
        self.password = password
        self.risposte_extra = list(risposte_extra)
        self.ricevute = []
        self.errore = None
        self._srv = socket.create_server(("127.0.0.1", 0))
        self.porta = self._srv.getsockname()[1]

    def _leggi(self, conn):
        dati = b""
        while not dati.endswith(b"##"):
            pezzo = conn.recv(256)
            if not pezzo:
                raise RuntimeError("client sparito")
            dati += pezzo
        return dati.decode("ascii")

    def run(self):
        try:
            conn, _ = self._srv.accept()
            with conn:
                conn.sendall(b"*#*1##")  # saluto
                if self._leggi(conn) != "*99*0##":
                    raise AssertionError("attesa sessione comandi")
                if self.password is not None:
                    nonce = "603356072"
                    conn.sendall(f"*#{nonce}##".encode())
                    attesa = domotica.calcola_password_open(self.password, nonce)
                    if self._leggi(conn) != f"*#{attesa}##":
                        conn.sendall(b"*#*0##")
                        raise AssertionError("password errata dal client")
                conn.sendall(b"*#*1##")  # sessione aperta
                self.ricevute.append(self._leggi(conn))
                for r in self.risposte_extra:
                    conn.sendall(r.encode("ascii"))
                conn.sendall(b"*#*1##")  # esito del comando
        except Exception as e:  # riportato al test, non inghiottito
            self.errore = e
        finally:
            self._srv.close()


def con_gateway(gw, azione):
    """Punta l'env al finto gateway, esegue, ripulisce. Ritorna l'esito dell'azione."""
    gw.start()
    os.environ["JARVIS_SCS_HOST"] = "127.0.0.1"
    os.environ["JARVIS_SCS_PORT"] = str(gw.porta)
    try:
        return azione()
    finally:
        gw.join(timeout=3)
        os.environ.pop("JARVIS_SCS_HOST", None)
        os.environ.pop("JARVIS_SCS_PORT", None)


# ============================================================================
# 4) Dialogo completo col finto gateway (senza password)
# ============================================================================
print("\n=== 4. dialogo col finto gateway ===")
gw = FintoGateway()
esito = con_gateway(gw, lambda: domotica.luce("cucina", "accendi"))
check("il gateway ha ricevuto la cornice giusta", gw.ricevute == ["*1*1*12##"], f"-> {gw.ricevute}")
check("esito riferito all'utente", "accesa" in esito, f"-> {esito!r}")
check("nessun errore nel gateway", gw.errore is None, f"-> {gw.errore}")

gw = FintoGateway(risposte_extra=["*1*1*12##"])
esito = con_gateway(gw, lambda: domotica.stato_luce("12"))
check("stato letto dal bus", "accesa" in esito, f"-> {esito!r}")
check("richiesta di stato corretta", gw.ricevute == ["*#1*12##"], f"-> {gw.ricevute}")

# ============================================================================
# 5) Dialogo con password OPEN
# ============================================================================
print("\n=== 5. autenticazione OPEN ===")
gw = FintoGateway(password=12345)
os.environ["JARVIS_SCS_PASSWORD"] = "12345"
esito = con_gateway(gw, lambda: domotica.tapparella("7", "giu"))
os.environ.pop("JARVIS_SCS_PASSWORD", None)
check("autenticato e comandato", gw.ricevute == ["*2*2*7##"], f"-> {gw.ricevute}")
check("nessun errore di auth", gw.errore is None, f"-> {gw.errore}")
check("esito tapparella", "giu" in esito, f"-> {esito!r}")

# ============================================================================
# 6) Errori onesti: host non configurato; Mac/Calendario fuori da macOS
# ============================================================================
print("\n=== 6. errori onesti ===")
os.environ.pop("JARVIS_SCS_HOST", None)
try:
    domotica.luce("12", "accendi")
    check("senza host -> errore chiaro", False, "non ha sollevato")
except RuntimeError as e:
    check("senza host -> errore chiaro", "JARVIS_SCS_HOST" in str(e), f"-> {e}")

import shutil
from tools import mac as tool_mac, calendario as tool_cal
if shutil.which("osascript") is None:
    try:
        tool_mac.volume("leggi")
        check("mac: fuori da macOS -> errore chiaro", False, "non ha sollevato")
    except RuntimeError as e:
        check("mac: fuori da macOS -> errore chiaro", "macOS" in str(e), f"-> {e}")
    try:
        tool_cal.impegni("oggi")
        check("calendario: fuori da macOS -> errore chiaro", False, "non ha sollevato")
    except RuntimeError as e:
        check("calendario: fuori da macOS -> errore chiaro", "macOS" in str(e), f"-> {e}")
else:
    check("mac: osascript presente (siamo su macOS), salto il controllo del ripiego", True)

# la scadenza malformata è respinta PRIMA di toccare osascript (anche su Linux)
try:
    tool_cal.promemoria_aggiungi("x", scadenza="domani alle 9")
    check("scadenza malformata respinta", False, "non ha sollevato")
except ValueError:
    check("scadenza malformata respinta", True)

# il timer valida i minuti prima di tutto? No: prima controlla osascript (voluto).
# Qui verifichiamo la REGISTRAZIONE delle nuove famiglie nel registro dei tool.
import tools
NUOVI = {"volume", "musica", "notifica", "timer", "timer_attivi",
         "impegni", "promemoria_lista", "promemoria_aggiungi",
         "luce", "tapparella", "stato_luce", "punti_scs"}
nomi = {s["name"] for s in tools.SCHEMAS}
check("tutte le nuove famiglie registrate", NUOVI <= nomi, f"mancanti={NUOVI - nomi or '∅'}")
check("i nuovi tool sono SAFE", all(tools.risk_of(n) == "SAFE" for n in NUOVI))

# ============================================================================
print("\n" + "=" * 60)
if FALLITI:
    print(f"RISULTATO: {len(FALLITI)} test FALLITI: {FALLITI}")
    raise SystemExit(1)
print("RISULTATO: tutti i test della domotica PASSATI ✔")
