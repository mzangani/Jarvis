"""
brain.py — Il cuore di Jarvis (Fase 1: il loop "nudo", senza tool).

In questa fase Jarvis è ancora solo un chatbot: riceve testo e risponde testo.
Non ha accesso ad alcuno strumento (arriveranno dalla Fase 2 in poi).
Qui costruiamo le fondamenta: la gestione della cronologia e la chiamata all'API.
"""

from anthropic import Anthropic

# Modello scelto in configurazione: è un identificatore ESATTO dell'API Anthropic.
# Cambiare questa stringa cambia il "cervello" di Jarvis.
MODEL = "claude-sonnet-4-6"

# Il "system prompt" definisce CHI è Jarvis e QUALI limiti ha.
# Non fa parte della cronologia dei messaggi: è un'istruzione permanente,
# sempre identica, che il modello riceve a ogni singola chiamata.
SYSTEM_PROMPT = """Sei Jarvis, un assistente personale che gira sul computer dell'utente.
Parli in italiano, in modo diretto e conciso.

Al momento (Fase 1) NON hai ancora accesso ad alcuno strumento: non puoi leggere file,
eseguire comandi, aprire applicazioni o navigare sul web. Se ti viene chiesta un'azione
sul sistema, spiega con onestà che in questa fase puoi soltanto conversare e che le
capacità operative verranno aggiunte nelle fasi successive.

Non fingere mai di aver eseguito un'azione che non puoi eseguire."""


class Agent:
    """
    L'agente conversazionale di Jarvis.

    Responsabilità in Fase 1:
      - tenere la cronologia della conversazione (self.messages)
      - inviare la cronologia all'API Anthropic e restituire la risposta
    """

    def __init__(self) -> None:
        # Il client legge automaticamente la chiave dalla variabile
        # d'ambiente ANTHROPIC_API_KEY: non la scriviamo mai nel codice.
        self.client = Anthropic()

        # La cronologia: una lista di dict con forma {"role": ..., "content": ...}.
        # 'role' vale "user" (noi) oppure "assistant" (Jarvis).
        # ATTENZIONE: il system prompt NON va qui dentro, ha un parametro a parte.
        self.messages: list[dict] = []

    def chat(self, user_input: str) -> str:
        """Aggiunge il messaggio utente, chiama l'API, salva e restituisce la risposta."""

        # 1) Aggiungiamo il nostro messaggio in fondo alla cronologia.
        self.messages.append({"role": "user", "content": user_input})

        # 2) Chiamiamo l'API inviando OGNI VOLTA l'intera cronologia.
        #    Il modello è "stateless" (senza memoria propria fra le chiamate):
        #    se non gli rimandiamo il passato, per lui quel passato non esiste.
        response = self.client.messages.create(
            model=MODEL,
            max_tokens=2048,          # tetto massimo di token della risposta
            system=SYSTEM_PROMPT,     # identità e limiti, sempre uguali
            messages=self.messages,   # tutta la conversazione fin qui
        )

        # 3) La risposta arriva come lista di "blocchi" di contenuto. In Fase 1
        #    (senza tool) c'è un solo blocco di tipo "text": ne uniamo il testo.
        reply = "".join(
            block.text for block in response.content if block.type == "text"
        )

        # 4) Salviamo la risposta di Jarvis nella cronologia: così, al turno
        #    successivo, quando rimanderemo tutto, il modello "ricorderà"
        #    anche ciò che ha detto lui stesso.
        self.messages.append({"role": "assistant", "content": reply})

        return reply
