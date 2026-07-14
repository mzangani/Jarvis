"""
brain.py — Il cuore di Jarvis.

Fase 2: introduciamo il vero LOOP AGENTICO. Ora Jarvis non solo conversa, ma può
DECIDERE di usare uno strumento (tool), riceverne il risultato e continuare.
Regola d'oro: il modello DECIDE, il nostro codice ESEGUE.
"""

import sqlite3
import time  # per misurare la durata di esecuzione dei tool (time.monotonic)

from anthropic import Anthropic, AnthropicError

import history  # memoria BREVE (Fase 5b): compattazione della cronologia
import logger  # tracciamento (Fase 7a): ogni tool call su file JSONL, per osservabilità
import memory  # memoria LUNGA persistente (Fase 5a): fatti su SQLite
import safety
# SCHEMAS      = elenco dei NOSTRI tool da mostrare al modello (li eseguiamo noi).
# SERVER_TOOLS = tool "server-side" eseguiti da Anthropic (es. web_search): li
#                passiamo alla create() ma non li dispatchiamo mai localmente.
# dispatch     = funzione che, dato un nome, esegue il tool giusto.
# risk_of      = livello di rischio di un tool (SAFE / CAUTION / DANGEROUS).
from tools import SCHEMAS, SERVER_TOOLS, dispatch, risk_of, precheck

MODEL = "claude-sonnet-4-6"

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


# System prompt DEDICATO al riassuntore della memoria BREVE (Fase 5b). Non è Jarvis:
# è un compito separato, "comprimi questa conversazione conservandone il senso".
SYSTEM_RIASSUNTO = """Sei un assistente che riassume una conversazione tra un utente e \
Jarvis (un assistente personale) per conservarne il contesto quando la cronologia \
diventa troppo lunga. Scrivi in italiano un riassunto CONCISO ma completo, in punti \
elenco, che conservi: i fatti e le preferenze dell'utente emersi, le decisioni prese, \
il compito in corso e le questioni ancora aperte, e i risultati importanti degli \
strumenti usati. Non inventare nulla e non aggiungere commenti fuori dal riassunto."""


class Agent:
    """
    L'agente di Jarvis.

    Responsabilità in Fase 2:
      - tenere la cronologia (self.messages)
      - eseguire il loop agentico: chiama l'API, se il modello chiede un tool
        lo esegue e gli rimanda il risultato, finché non arriva la risposta finale.
    """

    def __init__(self) -> None:
        self.client = Anthropic()  # legge la chiave da ANTHROPIC_API_KEY
        self.messages: list[dict] = []
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
            return (
                SYSTEM_PROMPT
                + "\n\n[Nota: la memoria persistente non è al momento disponibile "
                f"({e}). Puoi conversare, ma non posso salvare né richiamare fatti.]"
            )

        if not fatti:
            return SYSTEM_PROMPT  # nessun fatto ancora: prompt base, senza blocco vuoto

        righe = "\n".join(f"- {f}" for f in fatti)
        return (
            SYSTEM_PROMPT
            + "\n\nCose che ricordi sull'utente (dalla memoria persistente; usa il "
            "tool 'richiama' se ti serve qualcosa che non è elencato qui):\n"
            + righe
        )

    def _riassumi(self, vecchi: list) -> str:
        """
        Chiede al modello un riassunto della parte VECCHIA della cronologia.

        Passiamo un resoconto TESTUALE (non la cronologia in formato API) così il
        riassuntore non ha bisogno degli schemi dei tool. Nessun tool disponibile qui:
        vogliamo solo testo, e non deve esserci un pause_turn da gestire.
        """
        resoconto = history.serializza_per_riassunto(vecchi)
        risposta = self.client.messages.create(
            model=MODEL,
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
            riassunto = None
            nota = f"riassunto non riuscito ({e}); ho troncato la parte vecchia della cronologia"
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
        """
        # Aggiungiamo il messaggio dell'utente alla cronologia.
        self.messages.append({"role": "user", "content": user_input})

        # System prompt DINAMICO: base + fatti ricordati. Lo calcoliamo una volta per
        # turno (non serve rifarlo a ogni giro del loop interno: nello stesso turno un
        # 'ricorda' è già visibile al modello via il suo tool_result).
        system = self._costruisci_system()

        # Contatore delle "riprese" dei tool server-side in questo turno (vedi
        # pause_turn più sotto). Serve solo come guardia anti-loop.
        riprese_pause = 0

        # --- IL LOOP AGENTICO -------------------------------------------------
        # Ripete finché il modello NON chiede più tool. Può fare più giri!
        while True:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=2048,
                system=system,  # dinamico: base + fatti ricordati (vedi _costruisci_system)
                messages=self.messages,
                # SCHEMAS = i nostri tool (li eseguiamo noi). SERVER_TOOLS = i tool
                # nativi eseguiti da Anthropic (es. web_search): li dichiariamo qui,
                # ma non li dispatchiamo mai localmente.
                tools=SCHEMAS + SERVER_TOOLS,
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

            # stop_reason spiega PERCHÉ il modello si è fermato:
            #   "end_turn"  -> ha finito: ha una risposta pronta per l'utente
            #   "tool_use"  -> vuole che eseguiamo uno o più tool prima di continuare
            #   "pause_turn"-> gestito sopra (tool server-side da riprendere)
            #   (esistono altri valori, es. "max_tokens", ma qui ci bastano questi)
            if response.stop_reason != "tool_use":
                # Nessun NOSTRO tool richiesto: estraiamo il testo e usciamo dal loop.
                # Non restituiamo subito: prima (dopo il while) valutiamo se compattare
                # la cronologia, così il turno SUCCESSIVO parte più leggero.
                risposta_finale = "".join(
                    b.text for b in response.content if b.type == "text"
                )
                break

            # Il modello ha chiesto uno o PIÙ tool nello stesso turno: li eseguiamo tutti.
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue  # ignoriamo eventuali blocchi di testo qui

                if on_tool is not None:
                    on_tool(block.name, block.input)

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
                if rischio != safety.SAFE and not safety.confirm(
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

        # --- FINE TURNO ------------------------------------------------------
        # Il turno è concluso (siamo usciti dal loop con break). Prima di restituire,
        # valutiamo se la cronologia è cresciuta troppo e va compattata: alleggerisce
        # i turni futuri senza cambiare la risposta di questo.
        self._compatta_se_serve(on_note)
        return risposta_finale
