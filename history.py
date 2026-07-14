"""
history.py — La memoria BREVE di Jarvis (Fase 5b).

La memoria LUNGA (memory.py) sono FATTI su disco, persistenti tra le sessioni.
Questa invece gestisce la CRONOLOGIA della conversazione in corso (brain.Agent.messages):
quella che rimandiamo, INTERA, a ogni chiamata API. Cresce a ogni turno, quindi:
  - costa (più token di input a ogni chiamata),
  - prima o poi riempie la finestra di contesto del modello (l'API darebbe errore).

Rimedio: quando l'input supera una soglia di token, COMPATTIAMO la cronologia
(riassunto della parte vecchia + troncamento come ripiego). Questo file contiene la
logica PURA e testabile senza rete: decidere se compattare, trovare un confine di
taglio SICURO, ricostruire la lista compattata. La parte che parla col modello (il
riassunto vero) sta in brain.py, dove vive il client.

Il punto delicato è la CORRETTEZZA della lista di messaggi che l'API accetta:
  - il primo messaggio deve avere ruolo 'user';
  - un blocco 'tool_use' (assistant) DEVE essere seguito dai suoi 'tool_result' (user):
    guai a spezzare la coppia.
Per questo tagliamo SOLO all'inizio di un vero turno utente (vedi indice_taglio).
"""

import os

# Soglia in TOKEN DI INPUT oltre la quale scatta la compattazione. La misuriamo con
# response.usage.input_tokens (gratis, incluso in ogni risposta). Default prudente e
# configurabile via env (comodo per testare con valori bassi).
SOGLIA_TOKEN = int(os.environ.get("JARVIS_MAX_TOKENS", 40_000))

# Quanti turni utente PIÙ RECENTI teniamo intatti quando compattiamo. Il resto
# (più vecchio) viene riassunto o troncato.
_TURNI_RECENTI_DA_TENERE = 3

# Tetto ai caratteri di un singolo risultato-tool quando lo serializziamo per il
# riassunto: un tool_result può essere enorme (una pagina web, un elenco file) e non
# vogliamo gonfiare il prompt del riassuntore.
_MAX_CHAR_RISULTATO = 600


def serve_compattare(input_tokens: int) -> bool:
    """True se l'ultimo input inviato all'API ha superato la soglia."""
    return input_tokens > SOGLIA_TOKEN


def _e_turno_utente(messaggio: dict) -> bool:
    """
    True se `messaggio` è un VERO input dell'utente (non i risultati di un tool).

    Distinzione chiave nel nostro codice: l'input utente ha content di tipo str
    (vedi brain.chat), mentre i tool_result hanno content di tipo list. È questa
    differenza che ci dà i confini "puliti" dove tagliare senza rischi.
    """
    return messaggio["role"] == "user" and isinstance(messaggio["content"], str)


def indice_taglio(messages: list, turni_da_tenere: int = _TURNI_RECENTI_DA_TENERE) -> int | None:
    """
    Indice dove spezzare la cronologia in "vecchio" (da riassumere/troncare) e
    "recente" (da tenere intatto). È l'inizio del `turni_da_tenere`-ultimo turno utente.

    Perché all'INIZIO di un turno utente:
      - non spezziamo mai una coppia tool_use / tool_result (stanno dentro un turno);
      - la coda risultante comincia con un messaggio 'user', come l'API pretende.

    Ritorna None se i turni utente sono troppo pochi per ridurre qualcosa in sicurezza
    (in tal caso il chiamante non tocca la cronologia).
    """
    indici_utente = [i for i, m in enumerate(messages) if _e_turno_utente(m)]
    if len(indici_utente) <= turni_da_tenere:
        return None
    indice = indici_utente[-turni_da_tenere]
    # indice 0 significherebbe "non c'è niente prima da compattare": niente da fare.
    return indice if indice > 0 else None


def _tipo_blocco(blocco) -> str | None:
    """Legge il campo 'type' di un blocco, sia esso oggetto SDK o dizionario."""
    if isinstance(blocco, dict):
        return blocco.get("type")
    return getattr(blocco, "type", None)


def _campo(blocco, nome, default=None):
    """Legge un campo di un blocco, sia esso oggetto SDK (attributo) o dizionario (chiave)."""
    if isinstance(blocco, dict):
        return blocco.get(nome, default)
    return getattr(blocco, nome, default)


def _testo_risultato(res) -> str:
    """Riduce il content di un tool_result (str o lista di blocchi) a testo breve."""
    if isinstance(res, str):
        testo = res
    elif isinstance(res, list):
        parti = []
        for b in res:
            if _tipo_blocco(b) == "text":
                parti.append(_campo(b, "text", ""))
            else:
                parti.append(str(b))
        testo = " ".join(parti)
    else:
        testo = str(res)
    testo = testo.strip().replace("\n", " ")
    if len(testo) > _MAX_CHAR_RISULTATO:
        testo = testo[:_MAX_CHAR_RISULTATO] + " […]"
    return testo


def serializza_per_riassunto(messages: list) -> str:
    """
    Appiattisce i messaggi "vecchi" in un resoconto testuale (Utente / Jarvis / tool),
    da dare al modello riassuntore. NON è la cronologia in formato API: così il
    riassuntore non ha bisogno degli schemi dei tool per capire cosa è successo.
    """
    righe: list[str] = []
    for m in messages:
        contenuto = m["content"]
        if isinstance(contenuto, str):
            # Input utente vero, oppure una nostra coppia sintetica di un riassunto
            # precedente (in tal caso l'assistant ha anch'esso content str).
            prefisso = "Utente" if m["role"] == "user" else "Jarvis"
            righe.append(f"{prefisso}: {contenuto}")
            continue
        # contenuto è una lista di blocchi (assistant, o tool_result dell'utente)
        for blocco in contenuto:
            tipo = _tipo_blocco(blocco)
            if tipo == "text":
                testo = (_campo(blocco, "text", "") or "").strip()
                if testo:
                    righe.append(f"Jarvis: {testo}")
            elif tipo in ("tool_use", "server_tool_use"):
                nome = _campo(blocco, "name", "?")
                args = _campo(blocco, "input", {})
                righe.append(f"(Jarvis ha usato il tool {nome} con {args})")
            elif tipo in ("tool_result", "web_search_tool_result"):
                righe.append(f"(risultato tool: {_testo_risultato(_campo(blocco, 'content', ''))})")
            # altri tipi di blocco: irrilevanti per il resoconto, li saltiamo
    return "\n".join(righe)


def costruisci_compattata(coda: list, riassunto: str | None) -> list:
    """
    Ricompone la cronologia dopo la compattazione.

      - riassunto presente: anteponiamo alla coda recente una COPPIA SINTETICA
        (utente = il riassunto, assistant = "ok, ho il contesto"). La struttura resta
        valida: parte con 'user', i ruoli si alternano, nessuna coppia tool spezzata.
      - riassunto None (ripiego TRONCAMENTO): teniamo solo la coda recente.

    La `coda` comincia sempre con un vero turno utente (garantito da indice_taglio),
    quindi è già un inizio valido di per sé.
    """
    if riassunto is None:
        return list(coda)
    coppia = [
        {
            "role": "user",
            "content": (
                "[Riassunto della conversazione precedente, per continuità. Non è un "
                "nuovo messaggio dell'utente: è il contesto di ciò di cui avete già "
                f"parlato in questa sessione.]\n{riassunto}"
            ),
        },
        {
            "role": "assistant",
            "content": "Va bene, ho presente questo contesto. Continuiamo.",
        },
    ]
    return coppia + list(coda)
