"""
brain.py — Il cuore di Jarvis.

Fase 2: introduciamo il vero LOOP AGENTICO. Ora Jarvis non solo conversa, ma può
DECIDERE di usare uno strumento (tool), riceverne il risultato e continuare.
Regola d'oro: il modello DECIDE, il nostro codice ESEGUE.
"""

from anthropic import Anthropic

import safety
# SCHEMAS = elenco dei tool da mostrare al modello.
# dispatch = funzione che, dato un nome, esegue il tool giusto.
# risk_of  = livello di rischio di un tool (SAFE / CAUTION / DANGEROUS).
from tools import SCHEMAS, dispatch, risk_of

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """Sei Jarvis, un assistente personale che gira sul computer dell'utente.
Parli in italiano, in modo diretto e conciso.

Hai a disposizione lo strumento `get_system_info`, che ti fornisce dati REALI sul
computer (sistema operativo, ora, spazio su disco, batteria). Usalo quando l'utente
chiede queste informazioni, invece di rispondere a memoria o inventarle.

Altre azioni (leggere/scrivere file, eseguire comandi, aprire applicazioni, navigare
sul web) non sono ancora disponibili: verranno aggiunte nelle fasi successive.
Non fingere mai di aver eseguito un'azione che non puoi eseguire."""


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

    def chat(self, user_input: str, on_tool=None) -> str:
        """
        Gestisce un turno completo dell'utente, tool inclusi.

        `on_tool` è una funzione opzionale chiamata quando Jarvis usa un tool
        (serve solo a mostrarlo a schermo): brain.py NON stampa nulla di suo.
        """
        # Aggiungiamo il messaggio dell'utente alla cronologia.
        self.messages.append({"role": "user", "content": user_input})

        # --- IL LOOP AGENTICO -------------------------------------------------
        # Ripete finché il modello NON chiede più tool. Può fare più giri!
        while True:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=self.messages,
                tools=SCHEMAS,          # <- diciamo al modello quali tool esistono
            )

            # Salviamo SEMPRE il turno dell'assistant così com'è: può contenere sia
            # blocchi di testo sia blocchi 'tool_use'. Ci serve integro perché i
            # tool_result che invieremo dopo devono riferirsi ai loro 'id'.
            self.messages.append({"role": "assistant", "content": response.content})

            # stop_reason spiega PERCHÉ il modello si è fermato:
            #   "end_turn"  -> ha finito: ha una risposta pronta per l'utente
            #   "tool_use"  -> vuole che eseguiamo uno o più tool prima di continuare
            #   (esistono altri valori, es. "max_tokens", ma qui ci bastano questi)
            if response.stop_reason != "tool_use":
                # Nessun tool richiesto: estraiamo il testo e chiudiamo il turno.
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
