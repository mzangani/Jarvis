"""
server.py — Front end WEB locale di Jarvis (Fase 9), stile HUD.

Come la voce, è un GUSCIO attorno allo stesso Agent: brain.py non cambia. Il server è
SOLO libreria standard (http.server): niente Flask, niente websocket, niente dipendenze.

Architettura (un solo utente, in locale):

  browser ── GET  /            → ui/index.html (la pagina HUD, statica)
          ── GET  /api/eventi  → flusso SSE (Server-Sent Events): stati, tool, risposte
          ── POST /api/chat    → {"testo": ...} avvia un turno dell'agente (in un thread)
          ── POST /api/conferma→ {"ok": true/false} risponde a una richiesta di conferma

  Il turno gira in un THREAD di lavoro; le cose da mostrare (tool usati, note, risposta
  finale, richieste di conferma) diventano EVENTI in una coda, che il flusso SSE riversa
  al browser. La CONFERMA delle azioni non-SAFE sfrutta l'aggancio iniettabile
  `agent.conferma`: qui la sostituiamo con una funzione che emette l'evento
  'conferma_richiesta' e BLOCCA il thread di lavoro finché il browser non risponde
  (o scade il tempo → rifiuto, coerente con "meglio negare per errore").

Sicurezza (scelte oneste):
  - il server ascolta SOLO su 127.0.0.1: non è raggiungibile dalla rete;
  - un solo turno per volta (l'agente è uno): se occupato → 409;
  - la conferma con TIMEOUT (default 120 s) scade in RIFIUTO, mai in silenzio-assenso.

Avvio:  python server.py         (apre anche il browser)
Config: JARVIS_UI_PORT (default 8765).
"""

import json
import os
import queue
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv

# Quanto aspettiamo la risposta del browser a una conferma prima di considerare
# l'azione RIFIUTATA (fail closed: mai un silenzio-assenso).
_TIMEOUT_CONFERMA = 120  # secondi

_INDEX = Path(__file__).parent / "ui" / "index.html"


class StatoServer:
    """
    Lo stato condiviso tra le richieste HTTP e il thread di lavoro dell'agente.

    - eventi: coda degli eventi da riversare al browser via SSE.
    - occupato: True mentre un turno è in corso (un turno per volta).
    - conferma_attesa/conferma_esito: il "ponte" tra il thread di lavoro (che aspetta)
      e la POST /api/conferma (che risponde). Protetti da un lock.
    """

    def __init__(self, agent) -> None:
        self.agent = agent
        self.eventi: "queue.Queue[dict]" = queue.Queue()
        self.occupato = False
        self._lock = threading.Lock()
        self._conferma_evento: threading.Event | None = None
        self._conferma_esito = False
        # L'aggancio: da qui in poi le conferme dell'agente passano dal browser.
        agent.conferma = self._conferma_via_browser

    # --- eventi ---------------------------------------------------------------
    def emetti(self, tipo: str, **dati) -> None:
        self.eventi.put({"tipo": tipo, **dati})

    # --- conferma via browser ---------------------------------------------------
    def _conferma_via_browser(self, nome_tool: str, tool_input: dict, rischio: str) -> bool:
        """
        Sostituto di safety.confirm per il front end web. Emette l'evento e BLOCCA il
        thread di lavoro finché il browser non risponde (POST /api/conferma) o scade
        il timeout (→ rifiuto). Gira NEL thread di lavoro, mai nel thread HTTP.
        """
        with self._lock:
            self._conferma_evento = threading.Event()
            self._conferma_esito = False
        self.emetti("conferma_richiesta", tool=nome_tool,
                    input=tool_input, rischio=rischio)
        ok = self._conferma_evento.wait(timeout=_TIMEOUT_CONFERMA)
        with self._lock:
            esito = self._conferma_esito if ok else False
            self._conferma_evento = None
        if not ok:
            self.emetti("nota", testo="conferma scaduta: azione rifiutata")
        return esito

    def rispondi_conferma(self, ok: bool) -> bool:
        """Chiamata dalla POST /api/conferma. True se c'era davvero una conferma in attesa."""
        with self._lock:
            if self._conferma_evento is None:
                return False
            self._conferma_esito = bool(ok)
            self._conferma_evento.set()
            return True

    # --- turni ------------------------------------------------------------------
    def avvia_turno(self, testo: str) -> bool:
        """Avvia un turno in un thread di lavoro. False se l'agente è già occupato."""
        with self._lock:
            if self.occupato:
                return False
            self.occupato = True

        def lavoro() -> None:
            self.emetti("stato", valore="elaboro")
            try:
                risposta = self.agent.chat(
                    testo,
                    on_tool=lambda nome, ingresso: self.emetti("tool", tool=nome, input=ingresso),
                    on_note=lambda msg: self.emetti("nota", testo=msg),
                )
                self.emetti("risposta", testo=risposta)
            except Exception as e:  # rete di sicurezza come in main.py: fail loud, server vivo
                self.emetti("errore", testo=f"{type(e).__name__}: {e}")
            finally:
                with self._lock:
                    self.occupato = False
                self.emetti("stato", valore="pronto")

        threading.Thread(target=lavoro, daemon=True).start()
        return True


def crea_handler(stato: StatoServer):
    """Costruisce la classe handler con lo `stato` catturato nella chiusura."""

    class Handler(BaseHTTPRequestHandler):
        # Niente log di ogni richiesta sul terminale (il flusso SSE ne farebbe tanti).
        def log_message(self, fmt, *args):  # noqa: D102
            pass

        # --- helper ----------------------------------------------------------
        def _json(self, codice: int, corpo: dict) -> None:
            dati = json.dumps(corpo).encode("utf-8")
            self.send_response(codice)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(dati)))
            self.end_headers()
            self.wfile.write(dati)

        def _leggi_json(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}

        # --- GET --------------------------------------------------------------
        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                try:
                    pagina = _INDEX.read_bytes()
                except OSError as e:
                    self._json(500, {"errore": f"ui/index.html non leggibile: {e}"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(pagina)))
                self.end_headers()
                self.wfile.write(pagina)
            elif self.path == "/api/eventi":
                # SSE: teniamo la richiesta aperta e riversiamo la coda degli eventi.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    while True:
                        try:
                            evento = stato.eventi.get(timeout=15)
                            riga = f"data: {json.dumps(evento, ensure_ascii=False)}\n\n"
                        except queue.Empty:
                            riga = ": keepalive\n\n"  # commento SSE: tiene viva la connessione
                        self.wfile.write(riga.encode("utf-8"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return  # il browser ha chiuso la pagina: fine del flusso, non è un errore
            else:
                self._json(404, {"errore": "percorso sconosciuto"})

        # --- POST -------------------------------------------------------------
        def do_POST(self) -> None:
            if self.path == "/api/chat":
                corpo = self._leggi_json()
                testo = (corpo.get("testo") or "").strip()
                if not testo:
                    self._json(400, {"errore": "campo 'testo' mancante o vuoto"})
                elif stato.avvia_turno(testo):
                    self._json(202, {"ok": True})
                else:
                    self._json(409, {"errore": "l'agente sta già elaborando un turno"})
            elif self.path == "/api/conferma":
                corpo = self._leggi_json()
                if stato.rispondi_conferma(bool(corpo.get("ok"))):
                    self._json(200, {"ok": True})
                else:
                    self._json(409, {"errore": "nessuna conferma in attesa"})
            else:
                self._json(404, {"errore": "percorso sconosciuto"})

    return Handler


def avvia(porta: int | None = None, apri_browser: bool = True):
    """Costruisce l'Agent, avvia il server su 127.0.0.1 e (opzionale) apre il browser."""
    load_dotenv()
    from brain import Agent  # import qui: dopo load_dotenv, come fa main.py

    porta = porta if porta is not None else int(os.environ.get("JARVIS_UI_PORT", "8765"))
    stato = StatoServer(Agent())
    server = ThreadingHTTPServer(("127.0.0.1", porta), crea_handler(stato))
    indirizzo = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"Jarvis UI: {indirizzo}  (Ctrl-C per fermare)")
    if apri_browser:
        webbrowser.open(indirizzo)
    return server


if __name__ == "__main__":
    srv = avvia()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nArrivederci.")
        srv.shutdown()
