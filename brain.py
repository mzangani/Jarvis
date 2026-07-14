"""
brain.py — Il cuore di Jarvis.

Fase 2: introduciamo il vero LOOP AGENTICO. Ora Jarvis non solo conversa, ma può
DECIDERE di usare uno strumento (tool), riceverne il risultato e continuare.
Regola d'oro: il modello DECIDE, il nostro codice ESEGUE.
"""

import sqlite3

from anthropic import Anthropic

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

    def chat(self, user_input: str, on_tool=None) -> str:
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
                # Nessun NOSTRO tool richiesto: estraiamo il testo e chiudiamo il turno.
                return "".join(
                    b.text for b in response.content if b.type == "text"
                )

            # Il modello ha chiesto uno o PIÙ tool nello stesso turno: li eseguiamo tutti.
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue  # ignoriamo eventuali blocchi di testo qui

                if on_tool is not None:
                    on_tool(block.name, block.input)

                # CANCELLO 0: validazione categorica PRIMA della conferma. Alcune
                # azioni sono SEMPRE vietate (es. la blacklist della shell): le
                # respingiamo subito, senza nemmeno mostrare il prompt di conferma.
                # precheck è un no-op per i tool che non dichiarano un pre-check.
                # Se solleva, rimandiamo l'errore al modello e passiamo oltre.
                try:
                    precheck(block.name, block.input)
                except Exception as e:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Azione rifiutata: {e}",
                        "is_error": True,
                    })
                    continue

                # CANCELLO DI SICUREZZA: se l'azione non è SAFE, chiediamo conferma
                # esplicita PRIMA di eseguire. Se l'utente rifiuta, non eseguiamo e
                # rimandiamo al modello un tool_result che glielo comunica (così può
                # proporre un'alternativa invece di bloccarsi).
                rischio = risk_of(block.name)
                if rischio != safety.SAFE and not safety.confirm(
                    block.name, block.input, rischio
                ):
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "L'utente ha rifiutato l'esecuzione di questa azione.",
                        "is_error": False,
                    })
                    continue

                # Fail loud: se il tool fallisce, NON nascondiamo l'errore.
                # Lo rimandiamo al modello come osservazione, così può correggersi.
                try:
                    result = dispatch(block.name, block.input)
                    is_error = False
                except Exception as e:
                    result = f"Errore durante l'esecuzione del tool: {e}"
                    is_error = True

                tool_results.append({
                    "type": "tool_result",
                    # tool_use_id DEVE combaciare con block.id del 'tool_use':
                    # è così che il modello riconosce a quale richiesta appartiene
                    # ciascun risultato (potrebbero essercene più d'uno in parallelo).
                    "tool_use_id": block.id,
                    "content": result,
                    "is_error": is_error,
                })

            # I risultati dei tool si inviano come messaggio con ruolo 'user'.
            self.messages.append({"role": "user", "content": tool_results})
            # ...e il while RIPETE: il modello legge i risultati e decide il passo
            # successivo (un altro tool, oppure finalmente la risposta all'utente).
