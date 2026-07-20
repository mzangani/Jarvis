"""
test_voce.py — Test del CICLO VOCALE (Fase 6) con backend FINTI.

Non serve alcun hardware né libreria audio: iniettiamo un BackendVocale di mock e un
finto Agent, e verifichiamo l'orchestrazione (ascolto → trascrizione → wake word →
agent.chat → sintesi → riproduzione, e la frase d'uscita). È esattamente il flusso
non-audio che POSSIAMO collaudare in ambiente headless.
"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-fake")

import voice

FALLITI = []


def check(nome, condizione, dettaglio=""):
    esito = "PASS" if condizione else "FAIL"
    if not condizione:
        FALLITI.append(nome)
    print(f"[{esito}] {nome}" + (f" — {dettaglio}" if dettaglio else ""))


class FintoAgent:
    """Registra i testi ricevuti da chat() e risponde in modo prevedibile."""
    def __init__(self):
        self.ricevuti = []

    def chat(self, testo, on_tool=None, on_note=None):
        self.ricevuti.append(testo)
        return f"ho sentito: {testo}"


def backend_da_frasi(frasi):
    """
    Crea un BackendVocale finto che 'trascrive' le frasi date, una per giro. Registra
    ciò che viene 'riprodotto' (le frasi pronunciate da Jarvis) in `parlato`.
    """
    coda = list(frasi)
    parlato = []

    def ascolta():
        # Restituiamo un segnaposto: la trascrizione vera la fa trascrivi().
        return object()

    def trascrivi(_audio):
        # Esaurite le frasi, simuliamo un'uscita per non ciclare all'infinito.
        return coda.pop(0) if coda else "esci"

    def sintetizza(testo):
        return testo  # il "suono" è il testo stesso, per ispezionarlo

    def riproduci(audio):
        parlato.append(audio)

    b = voice.BackendVocale(ascolta=ascolta, trascrivi=trascrivi,
                            sintetizza=sintetizza, riproduci=riproduci)
    return b, parlato


# ============================================================================
# TEST 1 — flusso base + frase d'uscita
# ============================================================================
print("=== TEST 1: flusso base e uscita ===")
ag = FintoAgent()
backend, parlato = backend_da_frasi(["che ore sono", "esci"])
voice.ciclo_vocale(ag, backend, max_giri=10)
check("chat chiamata una sola volta col testo giusto", ag.ricevuti == ["che ore sono"], f"-> {ag.ricevuti}")
check("Jarvis ha pronunciato la risposta", "ho sentito: che ore sono" in parlato, f"-> {parlato}")
check("all'uscita saluta", parlato[-1] == "Arrivederci.", f"-> {parlato[-1]!r}")


# ============================================================================
# TEST 2 — frase vuota / non compresa viene saltata (niente chiamata al modello)
# ============================================================================
print("\n=== TEST 2: input vuoto saltato ===")
ag = FintoAgent()
backend, parlato = backend_da_frasi(["", "   ", "ciao", "esci"])
voice.ciclo_vocale(ag, backend, max_giri=10)
check("le frasi vuote non arrivano al modello", ag.ricevuti == ["ciao"], f"-> {ag.ricevuti}")


# ============================================================================
# TEST 3 — wake word: attiva solo con il prefisso, e lo rimuove
# ============================================================================
print("\n=== TEST 3: wake word ===")
ag = FintoAgent()
backend, parlato = backend_da_frasi([
    "che tempo fa",            # senza wake word -> ignorato
    "jarvis accendi la luce",  # con wake word   -> "accendi la luce"
    "jarvis",                  # solo wake word  -> ignorato
    "esci",
])
voice.ciclo_vocale(ag, backend, wake_word="jarvis", max_giri=10)
check("solo la frase con wake word arriva al modello", ag.ricevuti == ["accendi la luce"], f"-> {ag.ricevuti}")


# ============================================================================
# TEST 4 — max_giri fa da guardia (non cicla all'infinito)
# ============================================================================
print("\n=== TEST 4: guardia max_giri ===")
ag = FintoAgent()
# Frasi che NON contengono un'uscita: senza max_giri sarebbe un ciclo infinito.
frasi_infinite = ["uno"] * 100
coda = list(frasi_infinite)

def _ascolta():
    return object()
def _trascrivi(_a):
    return coda.pop(0) if coda else "ancora"
def _noop(_):
    return None
b = voice.BackendVocale(ascolta=_ascolta, trascrivi=_trascrivi, sintetizza=lambda t: t, riproduci=_noop)
voice.ciclo_vocale(ag, b, max_giri=3)
check("si ferma a max_giri (3 chiamate)", len(ag.ricevuti) == 3, f"-> {len(ag.ricevuti)}")


# ============================================================================
print("\n" + "=" * 60)
if FALLITI:
    print(f"RISULTATO: {len(FALLITI)} test FALLITI: {FALLITI}")
    raise SystemExit(1)
print("RISULTATO: tutti i test del ciclo vocale PASSATI ✔")
