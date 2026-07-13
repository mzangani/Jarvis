"""
tools/web.py — Famiglia di tool "Web".

È la prima volta che Jarvis esce dal computer e tocca la RETE. Rispetto a file e
shell la superficie di rischio è diversa: non tanto distruggere qualcosa in locale,
quanto (a) raggiungere servizi INTERNI che non dovrebbe vedere — attacco SSRF — e
(b) far entrare nel contesto del modello contenuto non fidato. La sicurezza è "a
cancelli", come per la shell:

  1. ensure_url_sicuro()  -> PRIMA riga del tool: rifiuta schemi non http/https e
     host locali/di rete privata (localhost, 127.x, 169.254.169.254, 10/8, ...).
     È esposto anche via PRECHECKS, quindi un URL vietato è respinto PRIMA della
     conferma (come la blacklist della shell).
  2. conferma CAUTION     -> gestita dal loop in brain.py (safety.confirm()):
     l'utente vede l'URL ESATTO e deve dire sì. Non la duplichiamo qui.
  3. output onesto e limitato -> timeout, tetto ai byte scaricati, tetto al testo
     restituito, redirect ri-controllati e limitati. Se la richiesta fallisce
     (4xx/5xx, DNS, timeout) NON fingiamo successo: rilanciamo un errore LOUD.

Scelte di progetto discusse e decise insieme:
  - RISCHIO = CAUTION. Una GET in sola lettura non cancella, non esegue codice, non
    scrive su disco: è più mite della shell. Il guardiano SSRF chiude il buco
    specifico della rete, e la conferma (CAUTION chiede comunque conferma) fa vedere
    l'URL all'utente a ogni fetch. Teniamo DANGEROUS per ciò che è davvero distruttivo.
  - HTML -> TESTO. Restituiamo il testo leggibile, non l'HTML grezzo: il markup
    riempirebbe il budget di contesto prima del contenuto vero, e "leggi_pagina"
    promette proprio il testo. Saltiamo <script>/<style> e andiamo a capo sui blocchi.

Vincolo di progetto: SOLO libreria standard (urllib + html.parser), niente
dipendenze nuove (requests, httpx, beautifulsoup...).

Limite onesto: html.parser NON esegue JavaScript. Su pagine che costruiscono il
contenuto via script (molte "single page app") uscirà poco testo — è un limite del
metodo, non un bug, e lo dichiariamo nella description e all'utente.
"""

import re
import urllib.error
import urllib.request
from html.parser import HTMLParser

import safety
from safety import CAUTION

# Tempo massimo per la richiesta (come la shell: un fetch che si impianta non deve
# bloccare l'agente per sempre). Vale sulle operazioni della socket.
_TIMEOUT = 20  # secondi

# Tetto ai BYTE scaricati dalla socket. Protegge da risposte enormi o infinite.
# In files.py _MAX_BYTES è 100 KB, ma l'HTML è verboso (tag, CSS, script): qui
# concediamo di più sul grezzo, perché il vero freno al contesto è _MAX_TESTO qui sotto.
_MAX_DOWNLOAD = 2_000_000  # ~2 MB

# Tetto ai CARATTERI di testo restituiti al modello (stessa idea di _MAX_OUTPUT in
# shell.py): è questo a proteggere il contesto, misurato sul contenuto utile.
_MAX_TESTO = 100_000

# Quanti redirect seguiamo al massimo (un ciclo di redirect non deve girare all'infinito).
_MAX_REDIRECT = 5

# Ci presentiamo onestamente: molti server rifiutano richieste senza User-Agent.
_USER_AGENT = "Jarvis/1.0 (assistente personale locale)"


# --- HTML -> testo -----------------------------------------------------------
class _EstrattoreTesto(HTMLParser):
    """
    Trasforma HTML in testo leggibile:
      - scarta del tutto il contenuto di <script>/<style>/<head> (rumore/eseguibile),
      - inserisce un a capo sui tag di blocco, così il testo non è un unico blob,
      - accumula il testo visibile e alla fine normalizza gli spazi.

    Non esegue JavaScript: è un estrattore, non un browser.
    """

    # Tag il cui CONTENUTO non è testo da leggere: lo saltiamo interamente.
    _TAG_DA_SALTARE = {"script", "style", "head", "noscript", "template"}
    # Tag di "blocco": dopo l'apertura/chiusura andiamo a capo, per separare le righe.
    _TAG_BLOCCO = {
        "p", "div", "br", "li", "tr", "ul", "ol", "table", "section", "article",
        "header", "footer", "nav", "aside", "blockquote", "pre", "hr",
        "h1", "h2", "h3", "h4", "h5", "h6",
    }

    def __init__(self) -> None:
        # convert_charrefs=True: le entità (&amp; &#233; ...) diventano testo normale.
        super().__init__(convert_charrefs=True)
        self._parti: list[str] = []
        self._salta = 0  # profondità dentro un tag da saltare (gestisce l'annidamento)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._TAG_DA_SALTARE:
            self._salta += 1
        elif tag in self._TAG_BLOCCO:
            self._parti.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._TAG_DA_SALTARE and self._salta > 0:
            self._salta -= 1
        elif tag in self._TAG_BLOCCO:
            self._parti.append("\n")

    def handle_data(self, data: str) -> None:
        # Ignoriamo il testo mentre siamo dentro <script>/<style>/<head>.
        if self._salta == 0:
            self._parti.append(data)

    def testo(self) -> str:
        """Unisce le parti raccolte e normalizza gli spazi in un testo leggibile."""
        grezzo = "".join(self._parti)
        # Riga per riga: spazi/tabulazioni multipli -> uno solo, bordi ripuliti.
        righe = [re.sub(r"[^\S\n]+", " ", riga).strip() for riga in grezzo.split("\n")]
        # Compattiamo le righe vuote consecutive in una sola (leggibilità).
        risultato: list[str] = []
        for riga in righe:
            if riga == "" and (not risultato or risultato[-1] == ""):
                continue
            risultato.append(riga)
        return "\n".join(risultato).strip()


# --- Redirect ri-controllati -------------------------------------------------
class _RedirectSicuro(urllib.request.HTTPRedirectHandler):
    """
    Ri-passa OGNI redirect dal guardiano di rete. Serve perché un URL pubblico può
    rimbalzare (301/302) verso un indirizzo interno: controllare solo l'URL iniziale
    non basta. Limita anche il numero di redirect (attributi usati da urllib).
    """

    max_repeats = _MAX_REDIRECT
    max_redirections = _MAX_REDIRECT

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Se la nuova tappa è vietata, ensure_url_sicuro solleva e il redirect si ferma
        # qui (l'eccezione risale fino a brain.py, che la rimanda al modello: loud).
        safety.ensure_url_sicuro(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _charset_da_content_type(content_type: str) -> str | None:
    """Estrae il charset da un header tipo 'text/html; charset=iso-8859-1'."""
    for parte in content_type.split(";"):
        parte = parte.strip()
        if parte.lower().startswith("charset="):
            return parte.split("=", 1)[1].strip().strip('"') or None
    return None


# --- Il tool -----------------------------------------------------------------
def leggi_pagina(url: str) -> str:
    """
    Recupera la pagina all'indirizzo `url` e ne restituisce il testo leggibile.

    Ordine dei cancelli:
      1. ensure_url_sicuro (qui sotto, prima riga): schemi e host vietati -> solleva.
      2. la conferma CAUTION è già stata chiesta dal loop in brain.py prima di qui.
    """
    # --- CANCELLO: guardiano di rete come PRIMISSIMA cosa --------------------
    # È la stessa verifica del pre-check: qui la ripetiamo come difesa in profondità,
    # così il tool resta sicuro anche se chiamato direttamente (es. nei test).
    url = safety.ensure_url_sicuro(url)

    richiesta = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    # build_opener mantiene i gestori di default (incl. il proxy da ambiente) e
    # sostituisce SOLO il redirect handler con il nostro, che ri-controlla le tappe.
    opener = urllib.request.build_opener(_RedirectSicuro())

    try:
        with opener.open(richiesta, timeout=_TIMEOUT) as risposta:
            content_type = risposta.headers.get("Content-Type", "")
            # Leggiamo al più _MAX_DOWNLOAD+1 byte: se ne arrivano di più sappiamo
            # che la pagina era più grande e lo segnaleremo nel marcatore.
            grezzo = risposta.read(_MAX_DOWNLOAD + 1)
            url_finale = risposta.geturl()  # l'URL effettivo dopo eventuali redirect
    except urllib.error.HTTPError as e:
        # 4xx/5xx (incluso "troppi redirect"): errore HTTP esplicito. Niente finto successo.
        raise RuntimeError(f"Errore HTTP {e.code} ({e.reason}) recuperando {url}")
    except urllib.error.URLError as e:
        # DNS, connessione rifiutata, timeout della socket, TLS non valido, ...
        raise RuntimeError(f"Impossibile recuperare {url}: {e.reason}")

    troppo_lungo = len(grezzo) > _MAX_DOWNLOAD
    grezzo = grezzo[:_MAX_DOWNLOAD]

    # Byte -> testo: usiamo il charset dichiarato se c'è, altrimenti utf-8. Con
    # errors="replace" i byte illeggibili diventano '�' invece di far esplodere il tool.
    charset = _charset_da_content_type(content_type) or "utf-8"
    try:
        html = grezzo.decode(charset, errors="replace")
    except LookupError:
        # Il server ha dichiarato un charset che Python non conosce: ripieghiamo su utf-8.
        html = grezzo.decode("utf-8", errors="replace")

    tipo = content_type.split(";", 1)[0].strip().lower()
    if tipo in ("", "text/html", "application/xhtml+xml"):
        # HTML (o tipo non dichiarato: assumiamo HTML): strip a testo leggibile.
        parser = _EstrattoreTesto()
        parser.feed(html)
        parser.close()
        corpo = parser.testo()
    elif (
        tipo.startswith("text/")
        or tipo in ("application/json", "application/xml")
        or tipo.endswith(("+json", "+xml"))
    ):
        # Già testo di per sé (JSON, XML, CSV, testo semplice): lo diamo com'è.
        corpo = html.strip()
    else:
        # Binario (immagine, PDF, ...): non lo riversiamo nel contesto. Onestà su
        # cosa abbiamo trovato, così il modello non finge di "aver letto" un'immagine.
        return (
            f"Pagina: {url_finale}\n\n"
            f"Il contenuto non è testo leggibile (Content-Type: {tipo or 'sconosciuto'}, "
            f"{len(grezzo)} byte). Questo strumento estrae testo da pagine web/HTML."
        )

    # --- Tetto sul testo finale + marcatori EVIDENTI se abbiamo troncato ------
    troncato_testo = len(corpo) > _MAX_TESTO
    corpo = corpo[:_MAX_TESTO]

    note = []
    if troppo_lungo:
        note.append(
            f"[...download interrotto ai primi {_MAX_DOWNLOAD} byte: la pagina era più grande...]"
        )
    if troncato_testo:
        note.append(f"[...testo troncato ai primi {_MAX_TESTO} caratteri...]")

    intestazione = f"Pagina: {url_finale}\n\n"
    coda = ("\n\n" + "\n".join(note)) if note else ""
    return intestazione + corpo + coda


# --- Schema per il modello ---------------------------------------------------
# Description scritta PER IL MODELLO: cosa fa, quando usarlo, quando NON usarlo
# (con rimando al tool giusto), e note sul formato dell'argomento.
LEGGI_PAGINA = {
    "name": "leggi_pagina",
    "description": (
        "Recupera una pagina web dato il suo URL e ne restituisce il TESTO leggibile "
        "(senza HTML, script o CSS). Usalo quando l'utente chiede di leggere, "
        "riassumere o cercare informazioni in una pagina web di cui hai (o puoi "
        "costruire) l'URL. Funziona solo con http/https; gli indirizzi locali e di "
        "rete privata (localhost, 127.x, 169.254.x, reti interne) sono bloccati per "
        "sicurezza, ed è normale. NON esegue JavaScript: su siti che caricano il "
        "contenuto via script (molte 'single page app') potresti ottenere poco testo "
        "— dillo all'utente invece di inventare. NON usarlo per file locali (usa la "
        "famiglia File) né come comando di rete generico (non fa POST, login o "
        "download). È un'azione che richiede conferma dell'utente. L'argomento 'url' "
        "è l'indirizzo completo comprensivo di schema, es. 'https://example.com/pagina'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": (
                    "L'URL completo della pagina, con schema http:// o https://, "
                    "es. 'https://example.com'."
                ),
            }
        },
        "required": ["url"],
    },
}


# Terna (schema, funzione, rischio). Web è CAUTION: la conferma è del loop in
# brain.py, non di questo file.
TOOLS = [
    (LEGGI_PAGINA, leggi_pagina, CAUTION),
]


# --- Pre-validazione (cancello 0, PRIMA della conferma) ----------------------
# Come per la shell, esponiamo il guardiano anche come pre-check, così un URL
# vietato (schema o host) viene respinto senza nemmeno mostrare il prompt di
# conferma. È la STESSA verifica della prima riga del tool: qui la anticipiamo.
def _precheck_leggi_pagina(tool_input: dict) -> None:
    """Solleva se l'URL è vietato dal guardiano di rete (prima della conferma)."""
    safety.ensure_url_sicuro(tool_input.get("url", ""))


PRECHECKS = {
    "leggi_pagina": _precheck_leggi_pagina,
}
