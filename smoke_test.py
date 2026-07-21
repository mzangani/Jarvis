"""
smoke_test.py — Verifica rapida "a secco" di Jarvis (Fase 7c).

NON è la prova end-to-end degli esempi del README: quella richiede una chiave API vera e
la rete. Questo script controlla, SENZA chiave e SENZA rete, che le fondamenta reggano:
  1. tutti i moduli si importano (nessun errore di sintassi/import nel grafo);
  2. il registro dei tool è ben formato (schemi, rischi, dispatch);
  3. l'Agent si costruisce (il client SDK non chiama la rete alla costruzione);
  4. un tool SAFE gira davvero in isolamento e restituisce testo.

È stdlib-only e ERMETICO: prima di importare qualsiasi cosa puntiamo sandbox, memoria e
log in una cartella temporanea usa-e-getta, così NON tocchiamo ~/Jarvis-Sandbox, e mettiamo
una ANTHROPIC_API_KEY finta (mai usata: non facciamo chiamate).

Uso:  python smoke_test.py       (dalla radice del progetto)
Esce con codice 0 se tutto passa, 1 altrimenti (fail loud).
"""
import os
import tempfile
from pathlib import Path

# --- Isolamento PRIMA degli import del progetto (le costanti si leggono all'import) ---
_TMP = Path(tempfile.mkdtemp(prefix="jarvis-smoke-"))
os.environ["JARVIS_SANDBOX"] = str(_TMP / "sandbox")
os.environ["JARVIS_MEMORY"] = str(_TMP / "memoria.db")
os.environ["JARVIS_LOG"] = str(_TMP / "jarvis.jsonl")
# Chiave FINTA: serve solo perché il costruttore Anthropic() la pretende; non la usiamo,
# perché in questo smoke-test non facciamo NESSUNA chiamata all'API.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-smoke-fake")

FALLITI: list[str] = []


def check(nome: str, condizione: bool, dettaglio: str = "") -> None:
    esito = "PASS" if condizione else "FAIL"
    if not condizione:
        FALLITI.append(nome)
    print(f"[{esito}] {nome}" + (f" — {dettaglio}" if dettaglio else ""))


# --- 1) IMPORT: se il grafo dei moduli è rotto, qui esplode subito (ed è quello che vogliamo)
print("=== 1. import dei moduli ===")
import brain
import main  # noqa: F401  (importarlo basta a validarne sintassi e import)
import safety
import memory  # noqa: F401
import history  # noqa: F401
import logger  # noqa: F401
import tools
check("tutti i moduli si importano", True)

# --- 2) REGISTRO DEI TOOL --------------------------------------------------------------
print("\n=== 2. registro dei tool ===")
# I tool che DEVONO esserci (sottoinsieme: robusto se in futuro se ne aggiungono).
ATTESI = {
    "get_system_info", "elenca_processi", "apri_applicazione", "screenshot", "chiudi_processo",
    "leggi_file", "lista_dir", "cerca_per_nome", "cerca_nel_contenuto",
    "scrivi_file", "sposta", "crea_cartella",
    "esegui_comando", "leggi_pagina", "ricorda", "richiama",
    # Nuove famiglie: appunti (clipboard + note), utilità (data/ora + meteo), file avanzati.
    "leggi_clipboard", "scrivi_clipboard", "aggiungi_nota", "elenca_note",
    "data_ora", "meteo",
    "info_file", "hash_file", "comprimi_zip", "estrai_zip",
}
nomi = {s["name"] for s in tools.SCHEMAS}
mancanti = ATTESI - nomi
check("tutti i tool attesi sono registrati", not mancanti, f"mancanti={mancanti or '∅'}")
check("ogni schema ha name/description/input_schema",
      all({"name", "description", "input_schema"} <= set(s) for s in tools.SCHEMAS))
check("web_search è tra i SERVER_TOOLS",
      any((s.get("name") or s.get("type")) == "web_search" for s in tools.SERVER_TOOLS))
# I rischi devono essere quelli attesi per un campione rappresentativo.
check("rischio get_system_info = SAFE", tools.risk_of("get_system_info") == safety.SAFE)
check("rischio scrivi_file = CAUTION", tools.risk_of("scrivi_file") == safety.CAUTION)
check("rischio esegui_comando = DANGEROUS", tools.risk_of("esegui_comando") == safety.DANGEROUS)
# Fail closed: un tool sconosciuto è trattato come DANGEROUS.
check("tool sconosciuto -> DANGEROUS (fail closed)",
      tools.risk_of("non_esiste") == safety.DANGEROUS)

# --- 3) COSTRUZIONE DELL'AGENT (nessuna rete) ------------------------------------------
print("\n=== 3. costruzione dell'Agent ===")
ag = brain.Agent()
check("Agent() si costruisce", ag is not None)
check("client configurato col retry (max_retries >= 1)", ag.client.max_retries >= 1)
check("cronologia iniziale vuota", ag.messages == [])

# --- 4) UN TOOL SAFE GIRA DAVVERO (in isolamento, senza modello) ------------------------
print("\n=== 4. un tool SAFE gira in isolamento ===")
out = tools.dispatch("get_system_info", {})
check("get_system_info restituisce testo non vuoto", isinstance(out, str) and bool(out.strip()))
check("l'output contiene 'Sistema operativo'", "Sistema operativo" in out, f"-> {out[:60]!r}…")
# Un tool della nuova famiglia "utilità": data_ora è SAFE e offline (solo l'orologio).
ora = tools.dispatch("data_ora", {})
check("data_ora gira e restituisce testo", isinstance(ora, str) and ora.startswith("Oggi è"),
      f"-> {ora!r}")

# --- 5) VOCE (Fase 6): il design OPZIONALE/GUARDATO regge senza dipendenze audio --------
print("\n=== 5. voce: modulo opzionale e guardato ===")
# `import voice` deve funzionare SEMPRE (non tira dentro l'audio al top del modulo).
import voice
check("import voice funziona senza dipendenze audio", True)
check("ciclo_vocale e crea_backend_reali esistono",
      hasattr(voice, "ciclo_vocale") and hasattr(voice, "crea_backend_reali"))
# La logica di rilevazione del silenzio (VAD) è pura: la esercitiamo a secco, senza audio.
check("RilevatoreFine esiste", hasattr(voice, "RilevatoreFine"))
_r = voice.RilevatoreFine(soglia=0.5, blocco=0.1, silenzio_fine=0.3,
                          attesa_inizio=1.0, durata_massima=2.0)
# Solo silenzio (energia 0): si deve fermare all'attesa (10° blocco), senza "parlato".
_stop_a = next((i for i in range(1, 100) if _r.considera(0.0)), None)
check("RilevatoreFine: solo silenzio -> stop all'attesa", _stop_a == 10, f"-> {_stop_a}")
check("RilevatoreFine: nessun parlato sul silenzio", _r.parlato_iniziato is False)
# Senza le librerie audio installate, crea_backend_reali deve fallire con un messaggio
# CHIARO (RuntimeError con le istruzioni), non un ImportError oscuro. Se invece qui le
# librerie ci fossero, saltiamo il controllo (non è un errore).
try:
    import faster_whisper  # noqa: F401
    import sounddevice  # noqa: F401
    _audio_presente = True
except ImportError:
    _audio_presente = False

if _audio_presente:
    check("deps audio presenti: salto il controllo del ripiego", True,
          "(faster_whisper/sounddevice installati)")
else:
    try:
        voice.crea_backend_reali()
        check("crea_backend_reali fallisce senza deps", False, "non ha sollevato")
    except RuntimeError as e:
        check("crea_backend_reali fallisce con messaggio chiaro",
              "requirements-voice.txt" in str(e), f"-> {e}")

# --- Esito ------------------------------------------------------------------------------
print("\n" + "=" * 60)
if FALLITI:
    print(f"SMOKE-TEST FALLITO: {len(FALLITI)} controlli KO -> {FALLITI}")
    raise SystemExit(1)
print("SMOKE-TEST OK ✔  (le fondamenta reggono; per gli esempi completi serve la chiave API)")
