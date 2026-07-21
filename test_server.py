"""
test_server.py — Test del front end web (Fase 9) SENZA rete esterna né browser.

Colaudiamo il server con un AGENTE FINTO iniettato in StatoServer: la pagina si serve,
un turno produce gli eventi SSE giusti (stato → tool → risposta → stato), la conferma
via browser autorizza/nega davvero, e i casi d'errore (testo vuoto, agente occupato,
conferma senza attesa) rispondono con i codici giusti. Tutto su 127.0.0.1 con porta
effimera: nessuna dipendenza, nessuna chiave.
"""
import json
import http.client
import os
import threading
from http.server import ThreadingHTTPServer

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-fake")

import server as srv_mod

FALLITI = []


def check(nome, condizione, dettaglio=""):
    esito = "PASS" if condizione else "FAIL"
    if not condizione:
        FALLITI.append(nome)
    print(f"[{esito}] {nome}" + (f" — {dettaglio}" if dettaglio else ""))


class FintoAgent:
    """
    Un agente prevedibile. Se il testo contiene 'conferma', chiede l'autorizzazione
    tramite l'aggancio iniettabile (self.conferma, che StatoServer sostituisce) e
    risponde in base all'esito — esattamente il contratto che brain.py usa davvero.
    """
    def __init__(self):
        self.conferma = None  # la imposta StatoServer

    def chat(self, testo, on_tool=None, on_note=None):
        if on_tool is not None:
            on_tool("tool_di_prova", {"arg": 1})
        if "conferma" in testo:
            ok = self.conferma("scrivi_file", {"percorso": "x.txt"}, "CAUTION")
            return "autorizzato" if ok else "negato"
        if "markdown" in testo:
            return "ecco **grassetto** e `codice` 🎉"
        return f"eco: {testo}"


# --- Avvio del server di test (porta effimera, nessun browser) -------------------
stato = srv_mod.StatoServer(FintoAgent())
httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv_mod.crea_handler(stato))
porta = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def post(percorso, corpo):
    c = http.client.HTTPConnection("127.0.0.1", porta, timeout=10)
    c.request("POST", percorso, body=json.dumps(corpo),
              headers={"Content-Type": "application/json"})
    r = c.getresponse()
    dati = json.loads(r.read().decode("utf-8"))
    c.close()
    return r.status, dati


class LettoreSSE:
    """Legge eventi dal flusso /api/eventi, uno alla volta (ignora i keepalive)."""
    def __init__(self):
        self.conn = http.client.HTTPConnection("127.0.0.1", porta, timeout=20)
        self.conn.request("GET", "/api/eventi")
        self.resp = self.conn.getresponse()

    def prossimo(self):
        while True:
            riga = self.resp.fp.readline().decode("utf-8").strip()
            if riga.startswith("data: "):
                return json.loads(riga[len("data: "):])

    def chiudi(self):
        self.conn.close()


# ============================================================================
# 1) La pagina HUD si serve
# ============================================================================
print("=== 1. pagina ===")
c = http.client.HTTPConnection("127.0.0.1", porta, timeout=10)
c.request("GET", "/")
r = c.getresponse()
pagina = r.read().decode("utf-8")
c.close()
check("GET / -> 200", r.status == 200, f"-> {r.status}")
check("la pagina è l'HUD di Jarvis", "J.A.R.V.I.S." in pagina)
check("la pagina ha il pannello di conferma", 'id="conferma"' in pagina)

# ============================================================================
# 2) Un turno completo: stato -> tool -> risposta -> stato
# ============================================================================
print("\n=== 2. turno completo via SSE ===")
sse = LettoreSSE()
codice, corpo = post("/api/chat", {"testo": "ciao"})
check("POST /api/chat -> 202", codice == 202, f"-> {codice} {corpo}")

ev1 = sse.prossimo()
check("primo evento: stato elaboro", ev1 == {"tipo": "stato", "valore": "elaboro"}, f"-> {ev1}")
ev2 = sse.prossimo()
check("secondo evento: tool", ev2["tipo"] == "tool" and ev2["tool"] == "tool_di_prova", f"-> {ev2}")
ev3 = sse.prossimo()
check("terzo evento: risposta", ev3["tipo"] == "risposta" and ev3["testo"] == "eco: ciao", f"-> {ev3}")
check("la risposta porta anche la versione per la voce", ev3.get("parlato") == "eco: ciao", f"-> {ev3}")
ev4 = sse.prossimo()
check("quarto evento: stato pronto", ev4 == {"tipo": "stato", "valore": "pronto"}, f"-> {ev4}")

# La versione 'parlato' è RIPULITA: markdown/emoji non arrivano alla sintesi del browser.
post("/api/chat", {"testo": "prova markdown"})
sse.prossimo(); sse.prossimo()           # stato elaboro, tool
ev = sse.prossimo()
check("a schermo il testo resta com'è", "**grassetto**" in ev["testo"], f"-> {ev['testo']!r}")
check("alla voce arriva testo pulito", ev["parlato"] == "ecco grassetto e codice", f"-> {ev['parlato']!r}")
sse.prossimo()                            # stato pronto

# Il registro conversazionale è attivo per l'HUD (risposte brevi, da ascoltare).
check("modalita_voce attiva nell'agente del server", stato.agent.modalita_voce is True)

# ============================================================================
# 3) Conferma via browser: APPROVA e NEGA
# ============================================================================
print("\n=== 3. conferma via browser ===")
codice, _ = post("/api/chat", {"testo": "chiedi conferma"})
check("turno con conferma avviato", codice == 202)
ev = sse.prossimo()  # stato elaboro
ev = sse.prossimo()  # tool
ev = sse.prossimo()  # conferma_richiesta
check("arriva conferma_richiesta", ev["tipo"] == "conferma_richiesta" and ev["rischio"] == "CAUTION",
      f"-> {ev}")
codice, _ = post("/api/conferma", {"ok": True})
check("POST /api/conferma -> 200", codice == 200)
ev = sse.prossimo()
check("con APPROVA la risposta è 'autorizzato'",
      ev["tipo"] == "risposta" and ev["testo"] == "autorizzato", f"-> {ev}")
sse.prossimo()  # stato pronto

codice, _ = post("/api/chat", {"testo": "chiedi conferma di nuovo"})
sse.prossimo(); sse.prossimo()          # stato elaboro, tool
ev = sse.prossimo()                      # conferma_richiesta
post("/api/conferma", {"ok": False})
ev = sse.prossimo()
check("con NEGA la risposta è 'negato'",
      ev["tipo"] == "risposta" and ev["testo"] == "negato", f"-> {ev}")
sse.prossimo()  # stato pronto

# ============================================================================
# 4) Errori onesti: testo vuoto, conferma fuori tempo, percorso ignoto
# ============================================================================
print("\n=== 4. casi d'errore ===")
codice, _ = post("/api/chat", {"testo": "   "})
check("testo vuoto -> 400", codice == 400, f"-> {codice}")
codice, _ = post("/api/conferma", {"ok": True})
check("conferma senza attesa -> 409", codice == 409, f"-> {codice}")
c = http.client.HTTPConnection("127.0.0.1", porta, timeout=10)
c.request("GET", "/non/esiste")
r = c.getresponse(); r.read(); c.close()
check("percorso ignoto -> 404", r.status == 404, f"-> {r.status}")

sse.chiudi()
httpd.shutdown()

# ============================================================================
print("\n" + "=" * 60)
if FALLITI:
    print(f"RISULTATO: {len(FALLITI)} test FALLITI: {FALLITI}")
    raise SystemExit(1)
print("RISULTATO: tutti i test del server PASSATI ✔")
