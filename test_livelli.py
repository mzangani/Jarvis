"""
test_livelli.py — Test dei LIVELLI DI RAGIONAMENTO (Fase 10), senza rete.

Colaudiamo: il registro dei livelli e i parametri API che genera (funzioni pure), la
scelta della variante di web_search per modello, e — con un CLIENT FINTO iniettato
nell'Agent — il flusso completo di escalation: il modello chiama imposta_livello, il
turno RIPARTE da capo al nuovo livello (modello più potente + thinking), e la guardia
anti-rimbalzo ferma i cambi di livello infiniti.
"""
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

_TMP = Path(tempfile.mkdtemp(prefix="jarvis-livelli-"))
os.environ["JARVIS_SANDBOX"] = str(_TMP / "sandbox")
os.environ["JARVIS_MEMORY"] = str(_TMP / "memoria.db")
os.environ["JARVIS_LOG"] = str(_TMP / "jarvis.jsonl")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-fake")

import brain

FALLITI = []


def check(nome, condizione, dettaglio=""):
    esito = "PASS" if condizione else "FAIL"
    if not condizione:
        FALLITI.append(nome)
    print(f"[{esito}] {nome}" + (f" — {dettaglio}" if dettaglio else ""))


# ============================================================================
# 1) Registro dei livelli e parametri API (funzioni pure)
# ============================================================================
print("=== 1. registro e parametri ===")
check("i tre livelli esistono", set(brain.LIVELLI) == {"base", "normale", "profondo"})
check("default: livello base", brain.LIVELLO_DEFAULT == "base", f"-> {brain.LIVELLO_DEFAULT}")
check("base usa un modello haiku (economico)",
      "haiku" in brain.LIVELLI["base"]["modello"], f"-> {brain.LIVELLI['base']['modello']}")
check("profondo usa un modello opus (potente)",
      "opus" in brain.LIVELLI["profondo"]["modello"], f"-> {brain.LIVELLI['profondo']['modello']}")

p_base = brain.parametri_livello("base")
p_prof = brain.parametri_livello("profondo")
check("base: niente thinking", "thinking" not in p_base, f"-> {p_base}")
check("profondo: thinking ADATTIVO", p_prof.get("thinking") == {"type": "adaptive"}, f"-> {p_prof}")
check("profondo: effort alto", p_prof.get("output_config") == {"effort": "high"})
check("profondo: più spazio di output", p_prof["max_tokens"] > p_base["max_tokens"])

# ============================================================================
# 2) Variante di web_search per modello (i modelli base non supportano la nuova)
# ============================================================================
print("\n=== 2. web_search per modello ===")
import tools
t_haiku = tools.server_tools_per_modello("claude-haiku-4-5")
t_opus = tools.server_tools_per_modello("claude-opus-4-8")
t_sonnet = tools.server_tools_per_modello("claude-sonnet-4-6")
check("haiku -> variante base (20250305)", t_haiku[0]["type"] == "web_search_20250305", f"-> {t_haiku}")
check("opus -> variante nuova (20260209)", t_opus[0]["type"] == "web_search_20260209")
check("sonnet 4.6 -> variante nuova", t_sonnet[0]["type"] == "web_search_20260209")
check("modello ignoto -> variante base (fail closed)",
      tools.server_tools_per_modello("claude-boh-1")[0]["type"] == "web_search_20250305")


# ============================================================================
# Client FINTO: risponde con una sequenza predefinita e REGISTRA le chiamate.
# ============================================================================
def blocco_testo(t):
    return SimpleNamespace(type="text", text=t)


def blocco_tool(nome, ingresso, id_="tu_1"):
    return SimpleNamespace(type="tool_use", name=nome, input=ingresso, id=id_)


def risposta(blocchi, stop="end_turn"):
    return SimpleNamespace(content=blocchi, stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=100))


class FintoClient:
    """Restituisce le risposte in sequenza e registra i kwargs di ogni create()."""
    def __init__(self, risposte):
        self._risposte = list(risposte)
        self.chiamate = []
        self.messages = self  # espone .messages.create come l'SDK

    def create(self, **kwargs):
        self.chiamate.append(kwargs)
        if not self._risposte:
            raise AssertionError("il finto client ha esaurito le risposte previste")
        return self._risposte.pop(0)


def nuovo_agent(risposte):
    ag = brain.Agent()
    ag.client = FintoClient(risposte)
    return ag


# ============================================================================
# 3) ESCALATION: il modello chiede 'profondo' -> il turno riparte al nuovo livello
# ============================================================================
print("\n=== 3. escalation con ripartenza del turno ===")
ag = nuovo_agent([
    # 1ª chiamata (livello base): il modello valuta e chiede il livello profondo.
    risposta([blocco_tool("imposta_livello",
                          {"livello": "profondo", "motivo": "analisi complessa"})],
             stop="tool_use"),
    # 2ª chiamata (turno RIPARTITO al livello profondo): risponde direttamente.
    risposta([blocco_testo("Analisi completata.")]),
])
note = []
reply = ag.chat("progetta l'architettura di un sistema distribuito",
                on_note=note.append)

check("risposta finale del livello profondo", reply == "Analisi completata.", f"-> {reply!r}")
check("livello attivo aggiornato", ag.livello == "profondo", f"-> {ag.livello}")
c1, c2 = ag.client.chiamate
check("1ª chiamata col modello base", c1["model"] == brain.LIVELLI["base"]["modello"], f"-> {c1['model']}")
check("2ª chiamata col modello profondo", c2["model"] == brain.LIVELLI["profondo"]["modello"], f"-> {c2['model']}")
check("2ª chiamata con thinking adattivo", c2.get("thinking") == {"type": "adaptive"})
check("1ª chiamata SENZA thinking", "thinking" not in c1)
check("il system della 2ª chiamata mostra il livello attivo",
      "Livello ATTIVO: profondo" in c2["system"], "")
check("il cambio è stato notificato", any("profondo" in n for n in note), f"-> {note}")
# La cronologia NON contiene il turno parziale: solo user + risposta finale.
check("cronologia pulita dopo la ripartenza",
      len(ag.messages) == 2 and ag.messages[0]["content"].startswith("progetta"),
      f"-> {len(ag.messages)} messaggi")
# Il web_search dichiarato segue il modello: base -> variante base, profondo -> nuova.
tipi1 = [t.get("type") for t in c1["tools"] if t.get("name") == "web_search"]
tipi2 = [t.get("type") for t in c2["tools"] if t.get("name") == "web_search"]
check("web_search base nella 1ª chiamata", tipi1 == ["web_search_20250305"], f"-> {tipi1}")
check("web_search nuovo nella 2ª chiamata", tipi2 == ["web_search_20260209"], f"-> {tipi2}")
check("imposta_livello è dichiarato al modello",
      any(t.get("name") == "imposta_livello" for t in c1["tools"]))

# ============================================================================
# 4) Livello già attivo: nessuna ripartenza, il turno prosegue
# ============================================================================
print("\n=== 4. livello già attivo ===")
ag = nuovo_agent([
    risposta([blocco_tool("imposta_livello", {"livello": "base", "motivo": "x"})],
             stop="tool_use"),
    risposta([blocco_testo("ok, resto al base")]),
])
reply = ag.chat("ciao")
check("nessun cambio: risponde normalmente", reply == "ok, resto al base", f"-> {reply!r}")
check("livello invariato", ag.livello == "base")
check("il tool_result 'già attivo' è tornato al modello",
      any("già attivo" in str(m) for m in ag.messages), "")

# ============================================================================
# 5) Livello sconosciuto: errore onesto al modello, niente crash
# ============================================================================
print("\n=== 5. livello sconosciuto ===")
ag = nuovo_agent([
    risposta([blocco_tool("imposta_livello", {"livello": "turbo", "motivo": "x"})],
             stop="tool_use"),
    risposta([blocco_testo("capito, livello non valido")]),
])
reply = ag.chat("vai a livello turbo")
check("risponde dopo l'errore", reply == "capito, livello non valido", f"-> {reply!r}")
check("livello invariato dopo errore", ag.livello == "base")

# ============================================================================
# 6) Guardia anti-rimbalzo: cambi di livello continui -> RuntimeError, rollback
# ============================================================================
print("\n=== 6. guardia anti-rimbalzo ===")
rimbalzo = [
    risposta([blocco_tool("imposta_livello", {"livello": "profondo", "motivo": "su"})], stop="tool_use"),
    risposta([blocco_tool("imposta_livello", {"livello": "base", "motivo": "giù"})], stop="tool_use"),
    risposta([blocco_tool("imposta_livello", {"livello": "profondo", "motivo": "su"})], stop="tool_use"),
    risposta([blocco_tool("imposta_livello", {"livello": "base", "motivo": "giù"})], stop="tool_use"),
]
ag = nuovo_agent(rimbalzo)
try:
    ag.chat("fai qualcosa")
    check("la guardia ferma il rimbalzo", False, "nessuna eccezione")
except RuntimeError as e:
    check("la guardia ferma il rimbalzo", "livello" in str(e), f"-> {e}")
check("cronologia ripulita dal rollback", ag.messages == [], f"-> {len(ag.messages)} messaggi")

# ============================================================================
print("\n" + "=" * 60)
if FALLITI:
    print(f"RISULTATO: {len(FALLITI)} test FALLITI: {FALLITI}")
    raise SystemExit(1)
print("RISULTATO: tutti i test dei livelli PASSATI ✔")
