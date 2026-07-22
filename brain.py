"""
brain.py — Il cuore di Jarvis.

Fase 2: introduciamo il vero LOOP AGENTICO. Ora Jarvis non solo conversa, ma può
DECIDERE di usare uno strumento (tool), riceverne il risultato e continuare.
Regola d'oro: il modello DECIDE, il nostro codice ESEGUE.
"""

import os  # per leggere la configurazione del retry/timeout dalle variabili d'ambiente
import sqlite3
import time  # per misurare la durata di esecuzione dei tool (time.monotonic)

# AnthropicError è la BASE di tutti gli errori dell'SDK; i sottotipi qui sotto ci
# servono per CLASSIFICARE un guasto (transitorio vs permanente) e dare un messaggio
# mirato (Fase 7b). Sono tutti esposti dal package `anthropic` (0.116.0).
from anthropic import (
    Anthropic,
    AnthropicError,
    APIStatusError,  # base di tutti gli errori con uno status HTTP; espone .status_code
    APIConnectionError,  # rete non raggiungibile o timeout (APITimeoutError ne è sottoclasse)
    AuthenticationError,  # 401: chiave errata/assente -> è PERMANENTE
    BadRequestError,  # 400: richiesta malformata -> PERMANENTE
    InternalServerError,  # 5xx generico -> transitorio
    NotFoundError,  # 404: es. nome del modello sbagliato -> PERMANENTE
    OverloadedError,  # 529: server sovraccarichi -> transitorio
    PermissionDeniedError,  # 403: non autorizzato -> PERMANENTE
    RateLimitError,  # 429: troppe richieste -> transitorio
    RequestTooLargeError,  # 413: richiesta troppo grande -> PERMANENTE
)

import history  # memoria BREVE (Fase 5b): compattazione della cronologia
import logger  # tracciamento (Fase 7a): ogni tool call su file JSONL, per osservabilità
import memory  # memoria LUNGA persistente (Fase 5a): fatti su SQLite
import safety
# SCHEMAS      = elenco dei NOSTRI tool da mostrare al modello (li eseguiamo noi).
# SERVER_TOOLS = tool "server-side" eseguiti da Anthropic (es. web_search): li
#                passiamo alla create() ma non li dispatchiamo mai localmente.
# dispatch     = funzione che, dato un nome, esegue il tool giusto.
# risk_of      = livello di rischio di un tool (SAFE / CAUTION / DANGEROUS).
from tools import SCHEMAS, SERVER_TOOLS, dispatch, risk_of, precheck, server_tools_per_modello

# --- LIVELLI DI RAGIONAMENTO (Fase 10) ---------------------------------------------
# Jarvis lavora a TRE livelli: modelli diversi (costo/capacità) e, al livello più alto,
# il RAGIONAMENTO ADATTIVO dell'API (thinking: il modello decide quanto "pensare").
# Il livello si cambia a runtime col tool `imposta_livello`: lo decide il MODELLO
# stesso (valuta la complessità del compito) o l'utente chiedendolo esplicitamente.
# I modelli di ciascun livello sono configurabili via env (JARVIS_MODEL_*).
LIVELLI: dict[str, dict] = {
    "base": {
        "modello": os.environ.get("JARVIS_MODEL_BASE", "claude-haiku-4-5"),
        "max_tokens": 2048,
        "thinking": False,
        "descrizione": "veloce ed economico, per l'uso quotidiano",
    },
    "normale": {
        "modello": os.environ.get("JARVIS_MODEL_NORMALE", "claude-sonnet-4-6"),
        "max_tokens": 4096,
        "thinking": False,
        "descrizione": "più capace, per compiti di media complessità",
    },
    "profondo": {
        "modello": os.environ.get("JARVIS_MODEL_PROFONDO", "claude-opus-4-8"),
        # Con il thinking il "pensiero" consuma output: serve più spazio (16k resta
        # sotto la soglia oltre cui l'SDK vorrebbe lo streaming).
        "max_tokens": 16000,
        "thinking": True,  # ragionamento adattivo + effort alto (vedi parametri_livello)
        "descrizione": "il più potente e ragiona a fondo, per i compiti difficili",
    },
}

# Livello di partenza (env JARVIS_LIVELLO). Validato FAIL LOUD come le altre config.
LIVELLO_DEFAULT = os.environ.get("JARVIS_LIVELLO", "base").strip().lower()
if LIVELLO_DEFAULT not in LIVELLI:
    raise ValueError(
        f"JARVIS_LIVELLO deve essere uno di {sorted(LIVELLI)}, ricevuto {LIVELLO_DEFAULT!r}."
    )


def parametri_livello(livello: str) -> dict:
    """
    I parametri per messages.create() del livello dato (funzione PURA sul registro):
    modello, max_tokens e — al livello 'profondo' — il ragionamento ADATTIVO
    (thinking adaptive: è il modello a decidere quanto pensare, non un budget fisso)
    con effort alto. Niente thinking ai livelli bassi: velocità e costo contenuti.
    """
    cfg = LIVELLI[livello]
    parametri: dict = {"model": cfg["modello"], "max_tokens": cfg["max_tokens"]}
    if cfg["thinking"]:
        parametri["thinking"] = {"type": "adaptive"}
        parametri["output_config"] = {"effort": "high"}
    return parametri


# Il tool con cui il MODELLO cambia livello. È un tool "del loop" (agisce sul loop
# stesso, non sul mondo): sta qui e viene intercettato in _loop_agentico PRIMA del
# dispatch normale — non è registrato in tools/ e non passa da conferme (SAFE:
# l'unico effetto è costo/qualità, ed è sempre visibile via on_note).
IMPOSTA_LIVELLO = {
    "name": "imposta_livello",
    "description": (
        "Cambia il tuo LIVELLO DI RAGIONAMENTO (modello e profondità di pensiero). "
        "Livelli: 'base' (veloce ed economico, per l'uso quotidiano), 'normale' (più "
        "capace, compiti di media complessità), 'profondo' (il più potente, ragiona a "
        "fondo: analisi complesse, progettazione, codice non banale, problemi a molti "
        "passi). Usalo QUANDO il compito richiesto supera chiaramente il livello attivo, "
        "oppure quando l'utente chiede esplicitamente di cambiare livello o di ragionare "
        "di più/di meno. Dopo la chiamata il turno RICOMINCIA da capo al nuovo livello: "
        "chiamalo PRIMA di iniziare a svolgere il compito, non a metà. I livelli alti "
        "costano di più: torna a 'base' quando il lavoro complesso è concluso."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "livello": {
                "type": "string",
                "enum": sorted(LIVELLI),
                "description": "Il livello da attivare: base, normale o profondo.",
            },
            "motivo": {
                "type": "string",
                "description": "Perché serve questo livello, in breve (mostrato all'utente).",
            },
        },
        "required": ["livello", "motivo"],
    },
}


class _RipartiTurno(Exception):
    """
    Segnale INTERNO di controllo (non un errore): il livello è cambiato a metà turno
    e il turno va rifatto DA CAPO al nuovo livello. Ripartire da zero — invece di
    proseguire a metà — è deliberato: il modello nuovo (e l'eventuale thinking)
    affronta la richiesta originale con un contesto pulito, e non ereditiamo vincoli
    dell'API su cronologie iniziate senza thinking.
    """

# --- Robustezza delle chiamate API (Fase 7b) --------------------------------------
# L'SDK `anthropic` RITENTA GIÀ da solo gli errori TRANSITORI (connessione/timeout,
# 429 rate limit, ≥500 incl. 529 overload) con backoff esponenziale + jitter e
# rispettando l'header retry-after. Quindi NON scriviamo un loop di retry a mano
# (duplicherebbe l'SDK, si accavallerebbe al suo backoff e rischierebbe di ritentare
# ciò che non va ritentato): ci limitiamo a CONFIGURARLO sul client.


def _leggi_int_env(nome: str, default: int, minimo: int) -> int:
    """
    Legge un intero da una variabile d'ambiente, validandolo FAIL LOUD ma in modo
    leggibile. Un valore malformato o sotto il minimo è un errore di configurazione
    dell'utente: meglio fermarsi subito con un messaggio chiaro che partire con un
    client rotto (o mostrare un traceback grezzo di int()).
    """
    grezzo = os.environ.get(nome)
    if grezzo is None:
        return default
    try:
        valore = int(grezzo)
    except ValueError:
        # 'from None' nasconde il traceback interno di int(): il messaggio nostro basta.
        raise ValueError(f"{nome} deve essere un intero, ricevuto {grezzo!r}.") from None
    if valore < minimo:
        raise ValueError(f"{nome} deve essere >= {minimo}, ricevuto {valore}.")
    return valore


def _leggi_timeout_env(nome: str) -> float | None:
    """
    Legge il timeout (in secondi) da env: None se la variabile non è impostata (così NON
    passiamo `timeout` al costruttore e resta il default dell'SDK). Un valore non numerico
    o <= 0 è un errore: 0/negativo darebbe un client che va SEMPRE in timeout — un guasto
    SILENZIOSO — quindi lo respingiamo LOUD. NB: non usiamo la "truthiness" della stringa
    ('0' sarebbe truthy): distinguiamo davvero "non impostato" da "valore esplicito".
    """
    grezzo = os.environ.get(nome)
    if grezzo is None:
        return None
    try:
        valore = float(grezzo)
    except ValueError:
        raise ValueError(f"{nome} deve essere un numero di secondi, ricevuto {grezzo!r}.") from None
    if valore <= 0:
        raise ValueError(f"{nome} deve essere > 0 secondi, ricevuto {valore}.")
    return valore


# Numero massimo di ritentativi automatici dell'SDK sugli errori transitori (>= 0).
# Default alzato da 2 (default SDK) a 4: assorbe qualche blip di rete/overload in più
# senza disturbare l'utente. Più alto = più resistenza ai guasti passeggeri, ma anche
# attesa più lunga prima di arrenderci quando l'API è davvero giù.
_API_RETRIES = _leggi_int_env("JARVIS_API_RETRIES", default=4, minimo=0)

# Timeout (secondi) sull'intera richiesta HTTP. None = non impostato -> default SDK.
_API_TIMEOUT = _leggi_timeout_env("JARVIS_API_TIMEOUT")

# Quante volte, in un singolo turno, accettiamo di "riprendere" un tool server-side
# fermatosi con stop_reason == "pause_turn" (vedi il loop). È una guardia anti-loop:
# se il server continuasse a chiedere di riprendere oltre questo tetto, ci fermiamo
# con un errore invece di restare bloccati per sempre.
_MAX_PAUSE_RESUME = 10

SYSTEM_PROMPT = """Sei Jarvis, un assistente personale che gira sul computer dell'utente.
Parli in italiano, in modo diretto e conciso.

Hai a disposizione strumenti (tool) che compiono azioni REALI. Quando servono per
rispondere, usali invece di rispondere a memoria o di inventare:
- Sistema: informazioni sul computer, elenco dei processi, apertura di
  applicazioni, chiusura di un processo, screenshot dello schermo.
- File: leggere, scrivere, elencare, cercare, spostare file e creare cartelle.
  Le operazioni sui file sono confinate a una cartella sicura ("sandbox"):
  percorsi al suo esterno vengono rifiutati, ed è normale.
- Shell: eseguire un comando di shell del sistema (es. `date`, `git status`,
  `df -h`) quando serve un'operazione da riga di comando non coperta dagli altri
  strumenti. È lo strumento più potente e delicato: preferisci sempre un tool
  dedicato quando esiste. Alcuni comandi distruttivi sono sempre vietati, e ogni
  comando chiede conferma prima di essere eseguito.
- Web: cercare informazioni sul web (ricerca online) e leggere una pagina web dato
  il suo URL. La RICERCA trova fonti e informazioni aggiornate: usala quando servono
  notizie recenti o dati che non conosci, e cita sempre le fonti. La LETTURA
  (leggi_pagina) recupera il testo leggibile di una pagina di cui hai (o puoi
  costruire) l'URL, senza HTML/script/stile; funziona con http/https, gli indirizzi
  locali e di rete privata sono bloccati per sicurezza (ed è normale), e non esegue
  JavaScript, quindi su pagine molto dinamiche potresti ottenere poco testo. Spesso
  il flusso naturale è: prima cerchi, poi (se serve) leggi una delle fonti trovate.
  Nota onesta: la ricerca invia la richiesta in rete (ad Anthropic e al motore di
  ricerca) e ha un piccolo costo per ogni ricerca.
  FONTI: quando rispondi basandoti su fonti web, cita la fonte per NOME (testata,
  sito), non recitare l'URL, e CHIEDI all'utente se vuole vederla (es. «vuoi che
  apra la fonte?»). Solo se dice sì, aprila nel suo browser con apri_url. Mai
  aprire pagine senza il suo sì esplicito nello scambio corrente.
- Mac: volume di sistema, musica (riproduci/pausa/brani), notifiche a schermo, timer
  (i timer vivono finché Jarvis è aperto; per promemoria durevoli usa i Promemoria).
- Calendario e promemoria del Mac: leggere gli impegni (oggi/domani/settimana),
  elencare e AGGIUNGERE promemoria (con scadenza: se serve la data di oggi per
  calcolarla, usa prima data_ora).
- Domotica di casa (impianto BTicino/SCS): accendere/spegnere le luci, muovere le
  tapparelle, leggere lo stato. I punti hanno nomi configurati (punti_scs li elenca)
  o indirizzi numerici; '0' è il comando generale (tutte le luci).
- Memoria: puoi RICORDARE fatti persistenti sull'utente (ricorda) e RICHIAMARLI
  quando servono (richiama). Usa 'ricorda' quando l'utente ti comunica un'informazione
  durevole (dove tiene i progetti, come si chiama, una preferenza stabile). I fatti
  più recenti che ricordi sono già elencati qui sotto nel blocco "Cose che ricordi
  sull'utente", quando presente: usa 'richiama' per cercare il resto. Il richiamo è
  per parole chiave, non semantico: non capisce i sinonimi, cerca le parole.

Alcune azioni che modificano il sistema o i file chiedono conferma all'utente
prima di essere eseguite: se l'utente rifiuta, riceverai un risultato che te lo
dice: proponi allora un'alternativa, non insistere.

Non fingere mai di aver eseguito un'azione che non puoi eseguire, e non dichiarare
riuscita un'azione il cui tool ha restituito un errore."""

# Blocco sui LIVELLI DI RAGIONAMENTO (Fase 10), appeso al system prompt a ogni turno
# con il livello ATTIVO. Le regole d'uso del tool stanno qui (e nella description del
# tool): valutare la complessità, obbedire all'utente, tornare a base quando si può.
SYSTEM_LIVELLI = """

LIVELLI DI RAGIONAMENTO. Lavori a livelli, cambiabili col tool 'imposta_livello':
- "base": veloce ed economico, per l'uso quotidiano (domande, tool semplici, conversazione).
- "normale": più capace, per compiti di media complessità.
- "profondo": il più potente e ragiona a fondo, per compiti DIFFICILI (analisi complesse,
  progettazione, codice non banale, molti passi interdipendenti).
Livello ATTIVO: {livello}.
Regole:
- Se il compito supera chiaramente il livello attivo, chiama imposta_livello PRIMA di
  svolgerlo: il turno ricomincerà da capo al livello giusto. Non usarlo per compiti che
  il livello attivo svolge già bene.
- Se l'utente chiede di cambiare livello o di ragionare di più/di meno, obbedisci.
- I livelli alti costano di più: concluso il lavoro complesso, torna a "base"."""

# Blocco AGGIUNTIVO per la MODALITÀ VOCE (Fase 6). Le risposte vengono LETTE ad alta voce
# da un sintetizzatore: il registro "da schermo" (elenchi, grassetti, codice) suona malissimo.
# Lo appendiamo al system prompt solo quando Jarvis è pilotato a voce (Agent.modalita_voce).
SYSTEM_VOCE = """

MODALITÀ VOCE. Le tue risposte vengono LETTE AD ALTA VOCE da un sintetizzatore vocale.
Adatta il REGISTRO di conseguenza:
- Rispondi BREVE e discorsivo, come PARLERESTI, non come scriveresti. Una o due frasi quando bastano.
- NIENTE formattazione: nessun elenco puntato o numerato, nessun grassetto o asterischi,
  nessun titolo, nessun blocco di codice, nessun emoji, nessun URL letto per intero.
- Se devi elencare più cose, dille a parole ("prima..., poi..., infine...") senza puntini.
- Numeri, date e unità in forma leggibile e naturale, non simbolica.
Il senso resta lo stesso: cambia solo il modo, pensato per essere ASCOLTATO."""


# System prompt DEDICATO al riassuntore della memoria BREVE (Fase 5b). Non è Jarvis:
# è un compito separato, "comprimi questa conversazione conservandone il senso".
SYSTEM_RIASSUNTO = """Sei un assistente che riassume una conversazione tra un utente e \
Jarvis (un assistente personale) per conservarne il contesto quando la cronologia \
diventa troppo lunga. Scrivi in italiano un riassunto CONCISO ma completo, in punti \
elenco, che conservi: i fatti e le preferenze dell'utente emersi, le decisioni prese, \
il compito in corso e le questioni ancora aperte, e i risultati importanti degli \
strumenti usati. Non inventare nulla e non aggiungere commenti fuori dal riassunto."""


def descrivi_errore_api(errore: AnthropicError) -> str:
    """
    Traduce un errore dell'SDK `anthropic` in un messaggio LEGGIBILE in italiano.

    È una funzione PURA (nessuna rete, nessuno stato): le passi un errore, ti restituisce
    una stringa. Per questo è testabile senza connessione (basta costruirle un finto
    errore) pur restando in brain.py, dove vive il client.

    Distinzione chiave della Fase 7b:
      - TRANSITORI: l'SDK li ha GIÀ ritentati (con backoff); se arrivano fin qui i
        tentativi sono esauriti -> messaggio "problema temporaneo, riprova".
      - PERMANENTI: ritentare non aiuterebbe -> messaggio mirato su cosa correggere.
    Fail closed: un errore che NON sappiamo classificare finisce nel ramo finale
    ("non riprovo, ecco cos'è"): meglio avvisare che fingere di sapere.

    Nota sull'ordine: in pratica quasi tutte queste classi sono "sorelle" (sotto
    APIStatusError), quindi l'ordine tra loro non cambia il risultato; l'unica gerarchia
    reale è APITimeoutError <: APIConnectionError, coperta testando APIConnectionError.
    L'ordine resta comunque difensivo (se in futuro una classe diventasse sottotipo di
    un'altra restiamo corretti) e va dal più specifico al più generico.
    """
    # --- PERMANENTI: nessun retry avrebbe aiutato, l'utente deve correggere qualcosa ---
    if isinstance(errore, AuthenticationError):  # 401
        return (
            "autenticazione fallita (401): controlla che ANTHROPIC_API_KEY sia "
            "corretta, attiva e con credito."
        )
    if isinstance(errore, PermissionDeniedError):  # 403
        return "permesso negato dall'API (403): la chiave non è autorizzata a questa operazione."
    if isinstance(errore, RequestTooLargeError):  # 413
        return (
            "richiesta troppo grande (413): la conversazione è troppo lunga. "
            "Prova a ripartire o ad alleggerire il contesto."
        )
    if isinstance(errore, NotFoundError):  # 404
        return (
            "risorsa non trovata (404): forse il nome di uno dei modelli configurati "
            "non è più valido (vedi JARVIS_MODEL_* in .env)."
        )
    if isinstance(errore, BadRequestError):  # 400
        return f"richiesta rifiutata dall'API (400, malformata): {errore}"
    # --- TRANSITORI: già ritentati dall'SDK, invano; ha senso solo riprovare più tardi ---
    if isinstance(errore, RateLimitError):  # 429
        return "troppe richieste (rate limit 429): aspetta un momento e riprova."
    if isinstance(errore, OverloadedError):  # 529
        return "i server di Anthropic sono sovraccarichi (529): riprova tra poco."
    if isinstance(errore, APIConnectionError):  # rete giù o timeout (incl. APITimeoutError)
        return (
            "non riesco a raggiungere l'API (rete non disponibile o timeout): "
            "controlla la connessione e riprova."
        )
    if isinstance(errore, InternalServerError):  # altri 5xx
        return "errore temporaneo del server API (5xx): riprova tra poco."
    # --- ALTRI errori con uno status HTTP non trattato sopra: classifichiamo per CODICE.
    #     Così i transitori che l'SDK ritenta (408 timeout, 409 lock, eventuali 5xx senza
    #     classe dedicata) NON finiscono nel fallback "imprevisto" col consiglio sbagliato,
    #     e i 4xx restanti (es. 422 non elaborabile) restano permanenti con un messaggio. ---
    if isinstance(errore, APIStatusError):
        code = errore.status_code
        if code in (408, 409) or (code is not None and code >= 500):
            return f"problema temporaneo del server API (codice {code}): riprova tra poco."
        return f"richiesta rifiutata dall'API (codice {code}): {errore}"
    # --- FALLBACK (fail closed): AnthropicError SENZA status HTTP (es. errori di
    #     validazione della risposta) che non rientrano in nessun caso sopra ---
    return f"errore imprevisto dell'API ({type(errore).__name__}): {errore}"


class Agent:
    """
    L'agente di Jarvis.

    Responsabilità in Fase 2:
      - tenere la cronologia (self.messages)
      - eseguire il loop agentico: chiama l'API, se il modello chiede un tool
        lo esegue e gli rimanda il risultato, finché non arriva la risposta finale.
    """

    def __init__(self) -> None:
        # Configuriamo il RETRY NATIVO dell'SDK (Fase 7b): max_retries governa quante
        # volte l'SDK ritenta AUTOMATICAMENTE gli errori transitori. Il timeout lo
        # passiamo SOLO se impostato via env: altrimenti lasciamo il default dell'SDK
        # (passare timeout=None significherebbe "nessun timeout", non ciò che vogliamo).
        opzioni_client = {"max_retries": _API_RETRIES}
        if _API_TIMEOUT is not None:
            opzioni_client["timeout"] = _API_TIMEOUT
        self.client = Anthropic(**opzioni_client)  # legge la chiave da ANTHROPIC_API_KEY
        self.messages: list[dict] = []
        # MODALITÀ VOCE (Fase 6): quando True, il system prompt guadagna il blocco SYSTEM_VOCE
        # (risposte brevi e senza formattazione, adatte a essere lette a voce). Lo attiva
        # `voice.avvia_voce`; in modalità testo resta False e nulla cambia.
        self.modalita_voce = False
        # CONFERMA INIETTABILE (Fase 9, front end): il cancello di conferma per i tool
        # non-SAFE. Default: il prompt a console di safety.confirm (REPL e voce). Un
        # front end diverso (es. il server web) la sostituisce con la propria — stessa
        # firma (nome_tool, tool_input, rischio) -> bool — senza toccare il loop.
        self.conferma = safety.confirm
        # LIVELLO DI RAGIONAMENTO attivo (Fase 10): parte dal default (env JARVIS_LIVELLO,
        # validato all'import) e cambia a runtime via il tool imposta_livello.
        self.livello = LIVELLO_DEFAULT
        # Quanti token di INPUT ha usato l'ultima chiamata all'API in questo turno.
        # È il segnale (gratis, incluso in ogni risposta) per decidere se compattare
        # la cronologia a fine turno. 0 = nessuna chiamata ancora.
        self.ultimi_input_tokens = 0

    def _costruisci_system(self) -> str:
        """
        Costruisce il system prompt DINAMICO: SYSTEM_PROMPT base + un blocco con i
        fatti che Jarvis ricorda (memoria LUNGA). Lo ricostruiamo a ogni turno, così
        un fatto appena imparato con 'ricorda' compare già dal turno successivo, senza
        dover riavviare. È una query SQLite banale: il costo è trascurabile.

        Iniezione + richiamo insieme = RAG semplificato: i fatti recenti sono già nel
        contesto (iniettati), il resto resta recuperabile su richiesta con 'richiama'.
        """
        try:
            fatti = memory.fatti_recenti()
        except (sqlite3.Error, OSError) as e:
            # except MIRATO e motivato: la memoria è un "di più". Se il suo disco è
            # rotto o non scrivibile NON facciamo crashare l'intero assistente, e NON
            # inventiamo fatti: lo dichiariamo apertamente nel prompt (loud) e andiamo
            # avanti a conversare. (Se l'utente prova comunque 'ricorda'/'richiama',
            # quei tool falliranno LOUD per conto loro, via il tool_result del loop.)
            prompt = (
                SYSTEM_PROMPT
                + "\n\n[Nota: la memoria persistente non è al momento disponibile "
                f"({e}). Puoi conversare, ma non posso salvare né richiamare fatti.]"
            )
        else:
            if not fatti:
                prompt = SYSTEM_PROMPT  # nessun fatto ancora: prompt base, senza blocco vuoto
            else:
                righe = "\n".join(f"- {f}" for f in fatti)
                prompt = (
                    SYSTEM_PROMPT
                    + "\n\nCose che ricordi sull'utente (dalla memoria persistente; usa il "
                    "tool 'richiama' se ti serve qualcosa che non è elencato qui):\n"
                    + righe
                )

        # Livelli di ragionamento (Fase 10): il modello deve sapere a che livello sta
        # lavorando ORA per decidere se cambiarlo. Ricostruito a ogni turno (e a ogni
        # ripartenza di turno), così riflette sempre il livello attivo.
        prompt += SYSTEM_LIVELLI.format(livello=self.livello)

        # In modalità voce, aggiungiamo le istruzioni di registro "parlato" (SYSTEM_VOCE):
        # risposte brevi e senza formattazione, adatte a essere lette a voce.
        if self.modalita_voce:
            prompt += SYSTEM_VOCE
        return prompt

    def _riassumi(self, vecchi: list) -> str:
        """
        Chiede al modello un riassunto della parte VECCHIA della cronologia.

        Passiamo un resoconto TESTUALE (non la cronologia in formato API) così il
        riassuntore non ha bisogno degli schemi dei tool. Nessun tool disponibile qui:
        vogliamo solo testo, e non deve esserci un pause_turn da gestire.
        """
        resoconto = history.serializza_per_riassunto(vecchi)
        risposta = self.client.messages.create(
            # Riassumere è un compito semplice: usiamo SEMPRE il modello del livello
            # "base" (economico), a prescindere dal livello attivo della conversazione.
            model=LIVELLI["base"]["modello"],
            max_tokens=1024,
            system=SYSTEM_RIASSUNTO,
            messages=[{"role": "user", "content": "Riassumi questa conversazione:\n\n" + resoconto}],
        )
        return "".join(b.text for b in risposta.content if b.type == "text").strip()

    def _compatta_se_serve(self, on_note=None) -> None:
        """
        A FINE turno, se l'ultimo input ha superato la soglia, compatta la cronologia
        per alleggerire i turni successivi. Strategia: riassunto della parte vecchia,
        con TRONCAMENTO come ripiego se il riassunto fallisce.

        `on_note` (opzionale) rende l'operazione visibile a schermo: brain.py non stampa.
        """
        if not history.serve_compattare(self.ultimi_input_tokens):
            return

        indice = history.indice_taglio(self.messages)
        if indice is None:
            # Oltre soglia, ma con troppi pochi turni "veri" per tagliare senza
            # rischiare di spezzare una coppia tool_use/tool_result: non tocchiamo nulla.
            if on_note is not None:
                on_note(
                    f"cronologia oltre soglia ({self.ultimi_input_tokens} token) ma non "
                    "ancora compattabile in sicurezza"
                )
            return

        vecchi = self.messages[:indice]  # parte da riassumere
        coda = self.messages[indice:]    # ultimi turni, tenuti intatti

        try:
            riassunto = self._riassumi(vecchi)
        except AnthropicError as e:
            # except MIRATO e motivato: se la chiamata di riassunto fallisce NON
            # lasciamo crescere il contesto all'infinito -> ripieghiamo sul TRONCAMENTO
            # (perdiamo il vecchio, ma restiamo entro i limiti) e lo dichiariamo.
            # Anche qui l'SDK ha già ritentato i guasti transitori; riusiamo la stessa
            # classificazione della Fase 7b per una nota leggibile (nessun doppione:
            # questo ramo NON crasha e NON propaga, ripiega e basta).
            riassunto = None
            nota = (
                "riassunto non riuscito (" + descrivi_errore_api(e)
                + "); ho troncato la parte vecchia della cronologia"
            )
        else:
            if not riassunto:
                # Riassunto vuoto: stesso ripiego, senza fingere di aver conservato il contesto.
                riassunto = None
                nota = "il riassunto è risultato vuoto; ho troncato la parte vecchia della cronologia"
            else:
                nota = "ho compattato la cronologia riassumendone la parte più vecchia"

        self.messages = history.costruisci_compattata(coda, riassunto)
        if on_note is not None:
            on_note(f"{nota} (input era {self.ultimi_input_tokens} token, soglia {history.SOGLIA_TOKEN})")

    def chat(self, user_input: str, on_tool=None, on_note=None) -> str:
        """
        Gestisce un turno completo dell'utente, tool inclusi.

        `on_tool` è una funzione opzionale chiamata quando Jarvis usa un tool
        (serve solo a mostrarlo a schermo): brain.py NON stampa nulla di suo.

        Fase 7b — COERENZA DELLA CRONOLOGIA. Fotografiamo la lunghezza di self.messages
        PRIMA di aggiungere alcunché (checkpoint). Se QUALSIASI cosa va storta a metà
        turno, ripristiniamo self.messages a quel punto (del ...[checkpoint:]): il turno
        fallito sparisce del tutto e il successivo riparte da uno stato VALIDO (l'ultimo
        messaggio è un assistant di fine turno, oppure la lista è vuota), senza mai un
        blocco tool_use rimasto senza il suo tool_result. Il rollback protegge l'INTERO
        turno, non solo la chiamata all'API: anche un EOFError da confirm(), un OSError
        dal logger o la RuntimeError della guardia pause_turn lascerebbero, altrimenti,
        una cronologia spezzata che farebbe fallire OGNI turno futuro.
        """
        checkpoint = len(self.messages)
        self.messages.append({"role": "user", "content": user_input})

        try:
            # RIPARTENZA DI TURNO (Fase 10): se a metà turno il modello cambia livello
            # (tool imposta_livello -> _RipartiTurno), scartiamo il turno parziale e lo
            # rifacciamo DA CAPO al nuovo livello — stesso input utente, cronologia
            # pulita, system prompt ricalcolato (mostra il livello attivo). Guardia
            # anti-rimbalzo: al massimo 2 ripartenze per turno, poi fail loud.
            ripartenze = 0
            while True:
                # System prompt DINAMICO: base + fatti ricordati + livello attivo. Lo
                # ricalcoliamo a ogni (ri)partenza del turno, non a ogni giro del loop.
                system = self._costruisci_system()
                try:
                    risposta_finale = self._loop_agentico(system, on_tool, on_note)
                    break
                except _RipartiTurno:
                    ripartenze += 1
                    if ripartenze > 2:
                        raise RuntimeError(
                            "il modello ha continuato a cambiare livello nello stesso "
                            "turno; interrompo per sicurezza."
                        ) from None
                    del self.messages[checkpoint:]
                    self.messages.append({"role": "user", "content": user_input})
        except AnthropicError as e:
            # GRACEFUL DEGRADATION: l'API ha fallito anche dopo i retry dell'SDK (o è un
            # errore permanente). Non propaghiamo (ucciderebbe la REPL): ripuliamo il
            # turno e restituiamo un messaggio leggibile.
            del self.messages[checkpoint:]
            descrizione = descrivi_errore_api(e)
            # Visibilità (decisione 5): l'evento come NOTA interna via on_note (brain.py
            # non stampa; è main.py a mostrarla). Tersa: la spiegazione completa sta nella
            # stringa restituita, per non ripetere due volte lo stesso testo.
            if on_note is not None:
                on_note(f"chiamata all'API non riuscita, turno annullato ({type(e).__name__})")
            return "Non sono riuscito a rispondere: " + descrizione
        except BaseException:
            # QUALSIASI altro guasto a metà turno (EOFError da una conferma, OSError dal
            # logger, la RuntimeError della guardia pause_turn, un Ctrl-C...): NON lo
            # inghiottiamo — fail loud — ma prima ripristiniamo la cronologia coerente,
            # così il turno successivo non eredita un tool_use spaiato; poi rilanciamo.
            # La rete di sicurezza in main.py mostrerà l'errore e terrà viva la REPL.
            del self.messages[checkpoint:]
            raise

        # Turno concluso con successo. Prima di restituire, valutiamo se la cronologia è
        # cresciuta troppo e va compattata: alleggerisce i turni futuri senza cambiare
        # la risposta di questo.
        self._compatta_se_serve(on_note)
        return risposta_finale

    def _loop_agentico(self, system: str, on_tool=None, on_note=None) -> str:
        """
        Il LOOP AGENTICO vero e proprio: chiama l'API e, finché il modello chiede tool,
        li esegue e gli rimanda i risultati; ritorna il testo finale quando il modello ha
        finito. NON gestisce qui i guasti: lascia PROPAGARE le eccezioni a chat(), che
        possiede il checkpoint e ripristina la cronologia in modo coerente (Fase 7b).
        """
        # Contatore delle "riprese" dei tool server-side in questo turno (vedi
        # pause_turn più sotto). Serve solo come guardia anti-loop.
        riprese_pause = 0

        # --- IL LOOP AGENTICO -------------------------------------------------
        # Ripete finché il modello NON chiede più tool. Può fare più giri!
        while True:
            # La chiamata al modello è il punto in cui l'API può fallire (rete giù, rate
            # limit, overload, chiave errata...). L'SDK ha GIÀ ritentato da solo i guasti
            # transitori (vedi _API_RETRIES); se solleva comunque, l'eccezione risale a
            # chat() che la traduce in un messaggio leggibile senza crashare.
            # Parametri del LIVELLO attivo (Fase 10): modello, max_tokens e (al livello
            # profondo) il ragionamento adattivo. Ricalcolati a ogni giro: costano nulla
            # e sono sempre coerenti con self.livello.
            parametri = parametri_livello(self.livello)
            response = self.client.messages.create(
                system=system,  # dinamico: base + fatti + livello (vedi _costruisci_system)
                messages=self.messages,
                # SCHEMAS = i nostri tool (li eseguiamo noi). IMPOSTA_LIVELLO = il tool
                # del loop per cambiare livello (intercettato più sotto, mai dispatchato).
                # server_tools_per_modello = i tool nativi eseguiti da Anthropic (es.
                # web_search), nella VARIANTE giusta per il modello attivo.
                tools=SCHEMAS + [IMPOSTA_LIVELLO] + server_tools_per_modello(parametri["model"]),
                **parametri,  # model, max_tokens e (se profondo) thinking + effort
            )

            # Quanti token di input ha pesato QUESTA chiamata (system + tool + cronologia).
            # L'ultima del turno è la più grande: la usiamo a fine turno per decidere se
            # compattare la cronologia. È gratis: arriva già dentro la risposta.
            self.ultimi_input_tokens = response.usage.input_tokens

            # Salviamo SEMPRE il turno dell'assistant così com'è: può contenere
            # blocchi di testo, blocchi 'tool_use' (nostri) e blocchi server-side
            # (server_tool_use + web_search_tool_result). Ci serve integro perché i
            # tool_result che invieremo dopo devono riferirsi ai loro 'id'.
            self.messages.append({"role": "assistant", "content": response.content})

            # VISIBILITÀ del web search: è server-side e NON passa dal cancello
            # confirm(). Per trasparenza mostriamo comunque la ricerca (la query)
            # tramite la callback, così l'utente vede cosa Jarvis ha cercato. I
            # blocchi 'server_tool_use' sono la richiesta di un tool eseguito da
            # Anthropic (il risultato è già nella risposta, non tocca a noi).
            if on_tool is not None:
                for block in response.content:
                    if block.type == "server_tool_use":
                        on_tool(block.name, block.input)

            # PAUSE_TURN: il loop server-side dei tool nativi ha raggiunto il suo
            # limite interno di iterazioni ma NON ha finito. Non è una risposta
            # pronta: ri-mandiamo la conversazione (che già termina con questo turno
            # assistant) per far RIPRENDERE il server. Non aggiungiamo un messaggio
            # "continua": l'API riconosce il blocco server_tool_use in coda e riprende.
            if response.stop_reason == "pause_turn":
                riprese_pause += 1
                if riprese_pause > _MAX_PAUSE_RESUME:
                    # Fail loud: non restiamo bloccati e non fingiamo una risposta.
                    raise RuntimeError(
                        "Il tool di ricerca web ha continuato a chiedere di riprendere "
                        f"oltre il limite ({_MAX_PAUSE_RESUME}); interrompo per sicurezza."
                    )
                continue

            # MAX_TOKENS: la risposta ha raggiunto il tetto di max_tokens ed è TRONCATA.
            # Va gestito PRIMA del ramo "risposta finale" qui sotto, perché:
            #   - se il modello è stato troncato MENTRE generava un tool_use, quel blocco
            #     resta senza tool_result: la cronologia sarebbe spezzata e OGNI turno
            #     successivo fallirebbe (tool_use spaiato). Non possiamo completarlo:
            #     sollevo, così chat() fa il rollback dell'INTERO turno e la cronologia
            #     resta valida (l'assistant troncato viene scartato con tutto il resto).
            #   - se invece è solo testo, è una risposta parziale ma coerente: la
            #     restituiamo avvisando via on_note che è incompleta.
            if response.stop_reason == "max_tokens":
                if any(b.type == "tool_use" for b in response.content):
                    raise RuntimeError(
                        "La risposta è stata troncata (max_tokens) mentre preparavo "
                        "un'azione: non posso completarla. Riprova con una richiesta più "
                        "breve o suddivisa in più passi."
                    )
                if on_note is not None:
                    on_note("risposta troncata per lunghezza (max_tokens): potrebbe essere incompleta")
                return "".join(b.text for b in response.content if b.type == "text")

            # stop_reason spiega PERCHÉ il modello si è fermato:
            #   "end_turn"  -> ha finito: ha una risposta pronta per l'utente
            #   "tool_use"  -> vuole che eseguiamo uno o più tool prima di continuare
            #   "pause_turn"-> gestito sopra (tool server-side da riprendere)
            #   "max_tokens"-> gestito sopra (risposta troncata)
            if response.stop_reason != "tool_use":
                # Nessun NOSTRO tool richiesto: estraiamo il testo e chiudiamo il loop.
                # Ritorniamo il testo a chat(), che poi deciderà se compattare la
                # cronologia (così il turno SUCCESSIVO parte più leggero).
                return "".join(b.text for b in response.content if b.type == "text")

            # Il modello ha chiesto uno o PIÙ tool nello stesso turno: li eseguiamo tutti.
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue  # ignoriamo eventuali blocchi di testo qui

                if on_tool is not None:
                    on_tool(block.name, block.input)

                # IMPOSTA_LIVELLO (Fase 10): tool DEL LOOP, intercettato PRIMA dei
                # cancelli e del dispatch (non è registrato in tools/: risk_of lo
                # tratterebbe come sconosciuto). Se il livello CAMBIA davvero, il turno
                # riparte da capo al nuovo livello (vedi _RipartiTurno e chat()).
                if block.name == "imposta_livello":
                    ingresso = block.input or {}
                    nuovo = (ingresso.get("livello") or "").strip().lower()
                    motivo = (ingresso.get("motivo") or "").strip()
                    if nuovo not in LIVELLI:
                        tool_results.append({
                            "type": "tool_result", "tool_use_id": block.id,
                            "content": f"Livello sconosciuto: {nuovo!r}. Validi: {sorted(LIVELLI)}.",
                            "is_error": True,
                        })
                        continue
                    if nuovo == self.livello:
                        tool_results.append({
                            "type": "tool_result", "tool_use_id": block.id,
                            "content": f"Il livello '{nuovo}' è già attivo.",
                            "is_error": False,
                        })
                        continue
                    self.livello = nuovo
                    # Trasparenza: il cambio è SEMPRE visibile (nota) e tracciato (log).
                    if on_note is not None:
                        on_note(f"livello di ragionamento → {nuovo}"
                                + (f" ({motivo})" if motivo else ""))
                    logger.log_tool_call(
                        tool="imposta_livello", tool_input=ingresso,
                        output=f"livello attivo: {nuovo}", esito="ok",
                        is_error=False, durata_ms=0.0, rischio=safety.SAFE,
                    )
                    raise _RipartiTurno()

                # Rischio del tool: ci serve sia per il cancello di conferma sia per
                # tracciarlo nel log a OGNI esito (anche i rifiuti). Lo calcoliamo qui
                # una volta, prima dei cancelli.
                rischio = risk_of(block.name)

                # CANCELLO 0: validazione categorica PRIMA della conferma. Alcune
                # azioni sono SEMPRE vietate (es. la blacklist della shell): le
                # respingiamo subito, senza nemmeno mostrare il prompt di conferma.
                # precheck è un no-op per i tool che non dichiarano un pre-check.
                # Se solleva, rimandiamo l'errore al modello e passiamo oltre.
                try:
                    precheck(block.name, block.input)
                except Exception as e:
                    rifiuto = f"Azione rifiutata: {e}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": rifiuto,
                        "is_error": True,
                    })
                    # LOG (osservabilità): rifiutato dal precheck. Il tool non è mai
                    # partito -> durata 0; nel log is_error=False (lì è True solo quando
                    # il tool viene ESEGUITO e fallisce). Il perché sta in `output`.
                    logger.log_tool_call(
                        tool=block.name, tool_input=block.input, output=rifiuto,
                        esito="rifiutato", is_error=False, durata_ms=0.0, rischio=rischio,
                    )
                    continue

                # CANCELLO DI SICUREZZA: se l'azione non è SAFE, chiediamo conferma
                # esplicita PRIMA di eseguire. Se l'utente rifiuta, non eseguiamo e
                # rimandiamo al modello un tool_result che glielo comunica (così può
                # proporre un'alternativa invece di bloccarsi).
                if rischio != safety.SAFE and not self.conferma(
                    block.name, block.input, rischio
                ):
                    rifiuto = "L'utente ha rifiutato l'esecuzione di questa azione."
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": rifiuto,
                        "is_error": False,
                    })
                    # LOG: rifiutato dall'utente alla conferma. Durata 0 (non eseguito):
                    # NON misuriamo attorno a confirm(), che include il tempo di
                    # riflessione dell'utente e falserebbe la durata del tool.
                    logger.log_tool_call(
                        tool=block.name, tool_input=block.input, output=rifiuto,
                        esito="rifiutato", is_error=False, durata_ms=0.0, rischio=rischio,
                    )
                    continue

                # Fail loud: se il tool fallisce, NON nascondiamo l'errore.
                # Lo rimandiamo al modello come osservazione, così può correggersi.
                # time.monotonic() è un orologio monotòno (non torna indietro se cambia
                # l'ora di sistema): giusto per misurare una durata. Misuriamo SOLO
                # attorno a dispatch(), cioè l'esecuzione vera del tool.
                t0 = time.monotonic()
                try:
                    result = dispatch(block.name, block.input)
                    is_error = False
                except Exception as e:
                    result = f"Errore durante l'esecuzione del tool: {e}"
                    is_error = True
                durata_ms = (time.monotonic() - t0) * 1000

                tool_results.append({
                    "type": "tool_result",
                    # tool_use_id DEVE combaciare con block.id del 'tool_use':
                    # è così che il modello riconosce a quale richiesta appartiene
                    # ciascun risultato (potrebbero essercene più d'uno in parallelo).
                    "tool_use_id": block.id,
                    "content": result,
                    "is_error": is_error,
                })
                # LOG: esito dell'esecuzione vera. "ok"/"errore" a seconda che il tool
                # abbia sollevato; durata reale misurata sopra.
                logger.log_tool_call(
                    tool=block.name, tool_input=block.input, output=result,
                    esito="errore" if is_error else "ok", is_error=is_error,
                    durata_ms=durata_ms, rischio=rischio,
                )

            # I risultati dei tool si inviano come messaggio con ruolo 'user'.
            self.messages.append({"role": "user", "content": tool_results})
            # ...e il while RIPETE: il modello legge i risultati e decide il passo
            # successivo (un altro tool, oppure finalmente la risposta all'utente).
