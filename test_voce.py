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
# TEST 5 — RilevatoreFine (VAD): la logica di rilevazione del silenzio, senza audio
# ============================================================================
# Usiamo numeri "puliti" per rendere ovvi i conteggi: blocco=0.1s, così
#   silenzio_fine=0.3 -> 3 blocchi di silenzio chiudono la frase,
#   attesa_inizio=1.0 -> 10 blocchi senza voce chiudono (frase vuota),
#   durata_massima=2.0 -> 20 blocchi è il tetto assoluto.
print("\n=== TEST 5: VAD (RilevatoreFine) ===")
PARAMETRI = dict(blocco=0.1, silenzio_fine=0.3, attesa_inizio=1.0, durata_massima=2.0)
FORTE, DEBOLE = 1.0, 0.0  # energie ben sopra/sotto la soglia
SOGLIA = 0.5


def conta_fino_a_stop(energie):
    """Alimenta il rilevatore con la sequenza di energie; ritorna (indice_di_stop 1-based,
    parlato_iniziato). Se non si ferma entro la sequenza, indice = None."""
    r = voice.RilevatoreFine(soglia=SOGLIA, **PARAMETRI)
    for i, e in enumerate(energie, start=1):
        if r.considera(e):
            return i, r.parlato_iniziato
    return None, r.parlato_iniziato


# 5a — solo silenzio: si chiude allo scadere dell'attesa (10° blocco), senza parlato.
idx, iniziato = conta_fino_a_stop([DEBOLE] * 50)
check("VAD: solo silenzio -> stop all'attesa (10)", idx == 10, f"-> {idx}")
check("VAD: solo silenzio -> nessun parlato", iniziato is False, f"-> {iniziato}")

# 5b — parlato poi silenzio: 5 blocchi forti, poi silenzio; chiude al 3° silenzio (8° blocco).
idx, iniziato = conta_fino_a_stop([FORTE] * 5 + [DEBOLE] * 50)
check("VAD: parlato+silenzio -> stop al 3° silenzio (8)", idx == 8, f"-> {idx}")
check("VAD: parlato+silenzio -> parlato iniziato", iniziato is True, f"-> {iniziato}")

# 5c — pausa BREVE non taglia: forte, 2 silenzi (<3), forte, poi 3 silenzi -> stop al 9°.
idx, _ = conta_fino_a_stop([FORTE] * 3 + [DEBOLE] * 2 + [FORTE] * 1 + [DEBOLE] * 3 + [FORTE] * 20)
check("VAD: pausa breve non chiude (stop al 9)", idx == 9, f"-> {idx}")

# 5d — parlato continuo: nessun silenzio -> si ferma solo al tetto massimo (20° blocco).
idx, _ = conta_fino_a_stop([FORTE] * 100)
check("VAD: parlato continuo -> stop al tetto (20)", idx == 20, f"-> {idx}")


# ============================================================================
print("\n" + "=" * 60)
if FALLITI:
    print(f"RISULTATO: {len(FALLITI)} test FALLITI: {FALLITI}")
    raise SystemExit(1)
print("RISULTATO: tutti i test del ciclo vocale PASSATI ✔")
