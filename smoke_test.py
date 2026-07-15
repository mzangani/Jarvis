"""
smoke_test.py — Verifica "a secco" di Jarvis (Fase 7c).

Controlla ciò che si può controllare SENZA chiave API e SENZA rete: che gli import
reggano, che il registro dei tool sia integro, che i cancelli di sicurezza blocchino
ciò che devono bloccare, e che i tool SAFE (file, sistema, memoria, log) funzionino
in isolamento su cartelle temporanee — la sandbox vera dell'utente non viene toccata.

NON copre il loop agentico end-to-end (il modello che decide, la conferma, il web):
per quello servono una ANTHROPIC_API_KEY reale e la rete — vedi gli Esempi nel README.

Uso:  python smoke_test.py
Esce con codice 0 se tutte le verifiche passano, 1 altrimenti (fail loud).
"""

import json
import os
import shutil
import sys
import tempfile
import traceback

# Le variabili d'ambiente vanno impostate PRIMA di importare i moduli del progetto:
# safety.py legge JARVIS_SANDBOX al momento dell'import, memory.py e logger.py
# leggono i loro percorsi al primo uso. Puntiamo tutto su una cartella temporanea,
# così il test è ripetibile e non lascia tracce nei dati veri dell'utente.
_TMP = tempfile.mkdtemp(prefix="jarvis-smoke-")
os.environ["JARVIS_SANDBOX"] = os.path.join(_TMP, "sandbox")
os.environ["JARVIS_MEMORY"] = os.path.join(_TMP, "memoria.db")
os.environ["JARVIS_LOG"] = os.path.join(_TMP, "log.jsonl")


def check_import_moduli() -> None:
    """Gli import reggono? (importare main NON avvia la REPL: parte solo da __main__)"""
    import brain    # noqa: F401  (richiede il package `anthropic`, ma NON la chiave)
    import history  # noqa: F401
    import logger   # noqa: F401
    import main     # noqa: F401
    import memory   # noqa: F401
    import safety   # noqa: F401
    import tools    # noqa: F401


def check_registro_tool() -> None:
    """Il registro dei tool è integro: nomi unici, rischi validi, famiglie al completo."""
    import safety
    from tools import SCHEMAS, SERVER_TOOLS, risk_of

    nomi = [schema["name"] for schema in SCHEMAS]
    assert len(nomi) == len(set(nomi)), f"nomi di tool duplicati nel registro: {nomi}"

    # Un rappresentante per famiglia (e i tool usati negli esempi del README).
    attesi = {
        "get_system_info", "elenca_processi",              # Sistema
        "leggi_file", "scrivi_file", "lista_dir",          # File
        "esegui_comando",                                  # Shell
        "leggi_pagina",                                    # Web
        "ricorda", "richiama",                             # Memoria
    }
    mancanti = attesi - set(nomi)
    assert not mancanti, f"tool attesi ma assenti dal registro: {sorted(mancanti)}"

    rischi_validi = {safety.SAFE, safety.CAUTION, safety.DANGEROUS}
    for nome in nomi:
        assert risk_of(nome) in rischi_validi, f"rischio non valido per il tool {nome!r}"

    # La ricerca web è un tool server-side: deve stare in SERVER_TOOLS, non in SCHEMAS.
    assert any(t.get("name") == "web_search" for t in SERVER_TOOLS), \
        "web_search assente da SERVER_TOOLS"
    assert "web_search" not in nomi, "web_search NON deve stare tra i tool locali"


def _deve_sollevare(eccezione: type, funzione, *args) -> None:
    """Verifica che funzione(*args) sollevi `eccezione`: per un cancello, bloccare È il successo."""
    try:
        funzione(*args)
    except eccezione:
        return  # comportamento atteso: il cancello ha bloccato l'azione
    raise AssertionError(
        f"{funzione.__name__}{args!r} doveva sollevare {eccezione.__name__} e non l'ha fatto"
    )


def check_cancelli_sicurezza() -> None:
    """I cancelli bloccano ciò che devono: evasione dalla sandbox, blacklist, URL vietati."""
    import safety

    # Sandbox: un percorso che "evade" con .. deve essere rifiutato...
    _deve_sollevare(PermissionError, safety.ensure_in_sandbox, "../fuori-dalla-sandbox.txt")
    # ...mentre un percorso relativo normale viene accettato (e risolto dentro la sandbox).
    dentro = safety.ensure_in_sandbox("note/appunti.txt")
    assert str(dentro).startswith(str(safety.sandbox_root().resolve()))

    # Blacklist: comandi catastrofici rifiutati, comandi innocui lasciati passare.
    assert safety.viola_blacklist("sudo rm -rf /") is not None
    assert safety.viola_blacklist("shutdown -h now") is not None
    assert safety.viola_blacklist("date") is None

    # Anti-SSRF, senza rete: lo schema vietato è respinto prima di qualunque DNS;
    # 127.0.0.1 è un IP letterale (niente DNS) che deve risultare vietato.
    _deve_sollevare(PermissionError, safety.ensure_url_sicuro, "file:///etc/passwd")
    _deve_sollevare(PermissionError, safety.ensure_url_sicuro, "http://127.0.0.1/admin")


def check_tool_file() -> None:
    """I tool File funzionano in isolamento (nella sandbox TEMPORANEA di questo test)."""
    # Chiamiamo le implementazioni direttamente: la conferma per i tool CAUTION è un
    # cancello del LOOP (brain.py), non delle funzioni — qui testiamo i tool da soli.
    from tools import files

    files.scrivi_file("prova/appunti.txt", "riga di prova")
    assert files.leggi_file("prova/appunti.txt") == "riga di prova"
    assert "appunti.txt" in files.lista_dir("prova")
    assert "appunti.txt" in files.cerca_per_nome("*.txt")
    assert "riga di prova" in files.cerca_nel_contenuto("riga di prova")


def check_tool_sistema() -> None:
    """get_system_info (SAFE, sola lettura) restituisce le informazioni attese."""
    from tools import system

    info = system.get_system_info()
    for voce in ("Sistema operativo:", "Ora locale:", "Disco:", "Batteria:"):
        assert voce in info, f"manca {voce!r} nell'output di get_system_info"


def check_memoria() -> None:
    """La memoria lunga salva e ritrova un fatto (su un DB temporaneo)."""
    import memory

    esito = memory.ricorda_fatto("L'utente tiene i suoi progetti nella cartella ~/Sviluppo")
    assert esito, "ricorda_fatto non ha restituito una conferma"
    trovato = memory.richiama_fatti("progetti")
    assert "Sviluppo" in trovato, f"fatto salvato ma non ritrovato: {trovato!r}"
    assert any("Sviluppo" in f for f in memory.fatti_recenti()), \
        "il fatto non compare tra i fatti recenti (iniezione nel system prompt)"


def check_memoria_breve() -> None:
    """Le funzioni pure di history.py decidono correttamente se/dove compattare."""
    import history

    assert not history.serve_compattare(0)
    assert history.serve_compattare(history.SOGLIA_TOKEN + 1)
    # Cronologia troppo corta: nessun taglio possibile (None), mai spezzare a caso.
    assert history.indice_taglio([{"role": "user", "content": "ciao"}]) is None


def check_logger() -> None:
    """Il logger scrive una riga JSONL leggibile (sul file temporaneo di questo test)."""
    import logger

    logger.log_tool_call(
        tool="tool_di_prova", tool_input={"x": 1}, output="tutto ok",
        esito="ok", is_error=False, durata_ms=1.2, rischio="SAFE",
    )
    with open(os.environ["JARVIS_LOG"], encoding="utf-8") as f:
        riga = json.loads(f.readline())
    assert riga["tool"] == "tool_di_prova" and riga["esito"] == "ok"


# Le verifiche, in ordine: prima gli import (se cadono loro, cade tutto il resto).
CHECKS = [
    ("import dei moduli (senza avviare la REPL)", check_import_moduli),
    ("integrità del registro dei tool", check_registro_tool),
    ("cancelli di sicurezza (sandbox, blacklist, anti-SSRF)", check_cancelli_sicurezza),
    ("tool File in isolamento (sandbox temporanea)", check_tool_file),
    ("tool Sistema in isolamento (get_system_info)", check_tool_sistema),
    ("memoria lunga (DB temporaneo)", check_memoria),
    ("memoria breve (funzioni pure di history)", check_memoria_breve),
    ("logger JSONL (file temporaneo)", check_logger),
]


def main() -> int:
    print("Smoke test di Jarvis — verifica a secco (senza chiave API, senza rete)")
    print(f"Cartella temporanea del test: {_TMP}\n")

    falliti = 0
    for descrizione, check in CHECKS:
        try:
            check()
        except Exception:
            falliti += 1
            print(f"FALLITO  {descrizione}")
            traceback.print_exc()  # fail loud: la causa vera, non un esito generico
        else:
            print(f"ok       {descrizione}")

    if falliti:
        print(f"\n{falliti} verifiche su {len(CHECKS)} FALLITE.")
        return 1
    print(f"\nTutte le {len(CHECKS)} verifiche sono passate.")
    print("NB: il loop agentico completo NON è coperto qui: per provarlo servono una")
    print("ANTHROPIC_API_KEY reale e la rete (vedi gli Esempi nel README).")
    return 0


if __name__ == "__main__":
    try:
        codice = main()
    finally:
        # Pulizia della cartella temporanea. ignore_errors: se la rimozione fallisse
        # non vogliamo mascherare con un errore di pulizia l'ESITO vero del test.
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(codice)
