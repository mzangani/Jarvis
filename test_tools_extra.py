"""
test_tools_extra.py — Test delle nuove famiglie di tool (appunti, utilità, file avanzati).

Colaudiamo ciò che è verificabile SENZA rete né hardware: data/ora, info_file, hash_file,
il giro completo comprimi_zip → estrai_zip, la protezione anti zip-slip, e le note.
La clipboard (dipende dai programmi di sistema) e il meteo (dipende dalla rete) si provano
a mano in locale.

Come lo smoke-test: puntiamo la sandbox in una cartella temporanea PRIMA degli import, così
non tocchiamo ~/Jarvis-Sandbox.
"""
import hashlib
import os
import tempfile
import zipfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="jarvis-tools-"))
os.environ["JARVIS_SANDBOX"] = str(_TMP / "sandbox")
os.environ["JARVIS_MEMORY"] = str(_TMP / "memoria.db")
os.environ["JARVIS_LOG"] = str(_TMP / "jarvis.jsonl")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-fake")

import safety
from tools import appunti, utilita, files_extra

FALLITI = []


def check(nome, condizione, dettaglio=""):
    esito = "PASS" if condizione else "FAIL"
    if not condizione:
        FALLITI.append(nome)
    print(f"[{esito}] {nome}" + (f" — {dettaglio}" if dettaglio else ""))


SANDBOX = safety.sandbox_root()


# ============================================================================
# 1) data_ora — forma leggibile in italiano
# ============================================================================
print("=== 1. data_ora ===")
d = utilita.data_ora()
check("data_ora inizia con 'Oggi è'", d.startswith("Oggi è"), f"-> {d!r}")
check("data_ora contiene l'ora (HH:MM)", ":" in d.split("sono le")[-1], f"-> {d!r}")


# ============================================================================
# 2) info_file e hash_file — sola lettura, nella sandbox
# ============================================================================
print("\n=== 2. info_file / hash_file ===")
contenuto = b"ciao mondo\n"
(SANDBOX / "prova.txt").write_bytes(contenuto)

info = files_extra.info_file("prova.txt")
check("info_file dice che è un file", "tipo: file" in info, f"-> {info!r}")
check("info_file mostra i byte esatti", f"({len(contenuto)} byte)" in info, f"-> {info!r}")

atteso = hashlib.sha256(contenuto).hexdigest()
h = files_extra.hash_file("prova.txt")
check("hash_file sha256 corretto", atteso in h, f"-> {h!r}")
check("hash_file di default usa sha256", h.startswith("sha256("), f"-> {h!r}")
try:
    files_extra.hash_file("prova.txt", algoritmo="rot13")
    check("hash_file rifiuta algoritmo ignoto", False, "non ha sollevato")
except ValueError:
    check("hash_file rifiuta algoritmo ignoto", True)

# fuori sandbox -> PermissionError (difesa comune a tutta la famiglia file)
try:
    files_extra.info_file("/etc/passwd")
    check("info_file blocca fuori sandbox", False, "non ha sollevato")
except PermissionError:
    check("info_file blocca fuori sandbox", True)


# ============================================================================
# 3) comprimi_zip → estrai_zip — giro completo
# ============================================================================
print("\n=== 3. zip: comprimi ed estrai ===")
(SANDBOX / "cartella").mkdir(exist_ok=True)
(SANDBOX / "cartella" / "a.txt").write_text("AAA", encoding="utf-8")
(SANDBOX / "cartella" / "b.txt").write_text("BBB", encoding="utf-8")

msg = files_extra.comprimi_zip(["cartella"], "out.zip")
check("comprimi_zip crea l'archivio", (SANDBOX / "out.zip").exists(), f"-> {msg!r}")
check("comprimi_zip conta i file", "con 2 file" in msg, f"-> {msg!r}")

files_extra.estrai_zip("out.zip", "estratto")
estratti = {p.name: p.read_text(encoding="utf-8")
            for p in (SANDBOX / "estratto").rglob("*") if p.is_file()}
check("estrai_zip ripristina a.txt", estratti.get("a.txt") == "AAA", f"-> {estratti}")
check("estrai_zip ripristina b.txt", estratti.get("b.txt") == "BBB", f"-> {estratti}")


# ============================================================================
# 4) estrai_zip — RIFIUTO dello zip-slip (voce che evade la destinazione)
# ============================================================================
print("\n=== 4. anti zip-slip ===")
cattivo = SANDBOX / "cattivo.zip"
with zipfile.ZipFile(cattivo, "w") as z:
    z.writestr("../evasione.txt", "non dovrei uscire")  # percorso che tenta di uscire
try:
    files_extra.estrai_zip("cattivo.zip", "dest_sicura")
    check("estrai_zip blocca lo zip-slip", False, "non ha sollevato")
except PermissionError:
    check("estrai_zip blocca lo zip-slip", True)
# ...e non deve aver creato il file fuori dalla destinazione.
check("lo zip-slip non ha scritto fuori", not (SANDBOX / "evasione.txt").exists())


# ============================================================================
# 5) note — aggiungi ed elenca
# ============================================================================
print("\n=== 5. note veloci ===")
check("all'inizio nessuna nota", "nessuna nota" in appunti.elenca_note().lower())
appunti.aggiungi_nota("comprare il pane")
appunti.aggiungi_nota("chiamare Luca")
elenco = appunti.elenca_note()
check("le note compaiono nell'elenco",
      "comprare il pane" in elenco and "chiamare Luca" in elenco, f"-> {elenco!r}")
try:
    appunti.aggiungi_nota("   ")
    check("nota vuota rifiutata", False, "non ha sollevato")
except ValueError:
    check("nota vuota rifiutata", True)


# ============================================================================
# 6) clipboard — selezione del programma (logica, senza toccare la clipboard vera)
# ============================================================================
print("\n=== 6. clipboard: selezione programma ===")
# _scegli deve trovare il primo programma disponibile secondo shutil.which; con una
# tabella di nomi inesistenti deve restituire None (e i tool falliscono LOUD).
check("_scegli con nomi inesistenti -> None",
      appunti._scegli([("programma_che_non_esiste_xyz", ["x"])]) is None)


# ============================================================================
# 7) apri_url — guardiano SSRF attivo; apertura verificata con un browser finto
# ============================================================================
print("\n=== 7. apri_url (fonti nel browser) ===")
from tools import web as tool_web

# Un URL locale è RIFIUTATO dal guardiano prima ancora di toccare il browser
# (127.0.0.1 non richiede DNS: il controllo gira anche offline).
try:
    tool_web.apri_url("http://127.0.0.1:8080/segreta")
    check("apri_url blocca gli host locali (SSRF)", False, "non ha sollevato")
except PermissionError:
    check("apri_url blocca gli host locali (SSRF)", True)

# Percorso felice: neutralizziamo il guardiano (niente DNS nel test) e sostituiamo
# webbrowser con un finto che registra. Ripristiniamo SEMPRE (finally).
_guardiano = safety.ensure_url_sicuro
_apri = tool_web.webbrowser.open
aperti = []
safety.ensure_url_sicuro = lambda u: u
tool_web.webbrowser.open = lambda u: (aperti.append(u), True)[1]
try:
    esito = tool_web.apri_url("https://esempio.it/articolo")
    check("apri_url apre l'URL richiesto", aperti == ["https://esempio.it/articolo"], f"-> {aperti}")
    check("apri_url riferisce cosa ha fatto", "aperto" in esito.lower(), f"-> {esito!r}")
    # Senza browser (open -> False) deve fallire LOUD, non fingere.
    tool_web.webbrowser.open = lambda u: False
    try:
        tool_web.apri_url("https://esempio.it/x")
        check("senza browser fallisce loud", False, "non ha sollevato")
    except RuntimeError:
        check("senza browser fallisce loud", True)
finally:
    safety.ensure_url_sicuro = _guardiano
    tool_web.webbrowser.open = _apri


# ============================================================================
print("\n" + "=" * 60)
if FALLITI:
    print(f"RISULTATO: {len(FALLITI)} test FALLITI: {FALLITI}")
    raise SystemExit(1)
print("RISULTATO: tutti i test delle nuove famiglie PASSATI ✔")
