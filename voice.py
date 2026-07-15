"""
voice.py — Modalità VOCE di Jarvis (FASE 6, opzionale).

È un WRAPPER attorno al loop agentico: brain.py, i tool e la sicurezza non vengono
toccati. Parli al microfono, faster-whisper (STT, locale) trascrive, il testo entra
in agent.chat() come se lo avessi digitato, e la risposta viene stampata E letta ad
alta voce da Piper (TTS, locale). Audio e trascrizione NON lasciano il tuo computer:
l'unica parte remota resta, come sempre, la chiamata all'API Anthropic.

Due modalità:
  - push-to-talk (default): premi Invio e parla; una pausa di silenzio chiude il turno.
    (Se invece scrivi del testo e premi Invio, viene usato quello: comodo se il
    microfono fa i capricci.)
  - wake word (`--wake`): ascolto continuo; di' "Jarvis" per attivarlo.

La modalità testo (`python main.py`) resta la via principale e NON richiede nulla di
tutto questo: le dipendenze extra (requirements-voice.txt) sono importate SOLO qui,
pigramente, con messaggi chiari se mancano.

SICUREZZA: le conferme per le azioni CAUTION/DANGEROUS restano da TASTIERA (s/N),
non vocali. Un "sì" mal trascritto non deve poter autorizzare un comando rischioso:
la voce è input/output della conversazione, i cancelli non si indeboliscono.
"""

import argparse
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from brain import Agent

console = Console()

# Riferimenti alle librerie della voce, valorizzati da _carica_librerie_voce()
# all'avvio. Restano None finché non si avvia davvero la modalità voce: così
# `import voice` (es. dallo smoke test) NON richiede le dipendenze opzionali.
np = None
sd = None

# --- Parametri di registrazione ------------------------------------------------
# 16 kHz mono è il formato nativo di whisper: registriamo direttamente così.
_CAMPIONAMENTO = 16_000
_BLOCCO_S = 0.1          # dimensione dei blocchi letti dal microfono (100 ms)
_CALIBRAZIONE_S = 0.3    # ascolto iniziale del rumore ambiente per tarare la soglia
_PAUSA_CHIUSURA_S = 1.2  # pausa di silenzio che chiude il turno di parlato
_ATTESA_MAX_S = 8.0      # push-to-talk: se non parli entro questo tempo, rinuncia
_DURATA_MAX_S = 60.0     # tetto assoluto a una registrazione (non restare aperti per sempre)
_SOGLIA_MINIMA_RMS = 0.005  # sotto questo volume è comunque silenzio, ovunque tu sia


def _carica_librerie_voce():
    """
    Importa le librerie della modalità voce, con messaggi CHIARI se mancano.

    Nota scoperta collaudando: se manca la libreria di SISTEMA PortAudio,
    `import sounddevice` solleva OSError (non ImportError) — catturiamo entrambe,
    ciascuna con il rimedio giusto. Fail loud, ma con la soluzione nel messaggio.
    """
    global np, sd
    try:
        import numpy as _np
        from faster_whisper import WhisperModel
        from piper import PiperVoice
        import sounddevice as _sd
    except ImportError as e:
        raise SystemExit(
            f"Dipendenza della modalità voce mancante ({e.name}).\n"
            "Installa con:  pip install -r requirements-voice.txt\n"
            "(La modalità testo funziona comunque:  python main.py)"
        ) from e
    except OSError as e:
        raise SystemExit(
            f"Libreria audio di sistema mancante: {e}\n"
            "Su Linux (Debian/Ubuntu):  sudo apt install libportaudio2\n"
            "(La modalità testo funziona comunque:  python main.py)"
        ) from e
    np = _np
    sd = _sd
    return WhisperModel, PiperVoice


# --- Logica PURA (testabile a secco, senza microfono) ---------------------------

# Wake word TOLLERANTE: collaudando a secco (Piper -> whisper) "Jarvis" è stato
# trascritto anche come "Giorvis"; whisper italianizza la pronuncia. Accettiamo le
# varianti vicine (jarvis, giarvis, giorvis, jervis, ...) invece di ignorare l'utente.
_WAKE_REGEX = re.compile(r"\b[jg]i?[aeo]?rvis\b", re.IGNORECASE)


def estrai_dopo_wake(testo: str) -> str | None:
    """
    Se `testo` contiene la wake word, ritorna ciò che la segue (anche stringa vuota,
    se l'utente ha detto solo "Jarvis"); altrimenti None.
    """
    m = _WAKE_REGEX.search(testo)
    if m is None:
        return None
    # lstrip, NON strip: togliamo solo il separatore DOPO la wake word ("Jarvis, ...");
    # la punteggiatura in coda ("che ore sono?") è parte della richiesta e resta.
    return testo[m.end():].lstrip(" ,.;:!?").strip()


def e_comando_uscita(testo: str) -> bool:
    """True se il testo (detto o digitato) è un congedo: 'esci', 'exit', 'quit'."""
    return testo.lower().strip(" .,!?") in {"esci", "exit", "quit"}


# --- Ascolto e trascrizione -----------------------------------------------------

def _rms(blocco) -> float:
    """Volume medio (RMS) di un blocco audio float32: la nostra misura di 'silenzio'."""
    return float(np.sqrt(np.mean(np.square(blocco, dtype=np.float64))))


def registra_fino_al_silenzio(attesa_max_s: float = _ATTESA_MAX_S):
    """
    Registra dal microfono finché non senti una pausa di _PAUSA_CHIUSURA_S.
    Ritorna l'audio (numpy float32 mono, 16 kHz) o None se nessuno ha parlato
    entro `attesa_max_s`.

    La soglia di silenzio non è fissa: i primi ~0.3 s calibrano il rumore ambiente
    (una soglia buona in una stanza silenziosa è pessima vicino a un ventilatore).
    Il parlato deve superare il rumore di fondo con un buon margine.
    """
    frame_per_blocco = int(_CAMPIONAMENTO * _BLOCCO_S)
    blocchi = []
    volumi_calibrazione = []
    soglia = None
    silenzio_s = 0.0
    ha_parlato = False

    with sd.InputStream(samplerate=_CAMPIONAMENTO, channels=1, dtype="float32") as stream:
        while True:
            blocco, _overflow = stream.read(frame_per_blocco)
            blocco = blocco[:, 0]  # mono: teniamo il solo canale
            blocchi.append(blocco)
            volume = _rms(blocco)

            if soglia is None:
                # Fase di calibrazione: misuriamo il rumore ambiente.
                volumi_calibrazione.append(volume)
                if len(volumi_calibrazione) * _BLOCCO_S >= _CALIBRAZIONE_S:
                    rumore = float(np.median(volumi_calibrazione))
                    soglia = max(_SOGLIA_MINIMA_RMS, 3.0 * rumore)
                continue

            if volume >= soglia:
                ha_parlato = True
                silenzio_s = 0.0
            else:
                silenzio_s += _BLOCCO_S

            durata = len(blocchi) * _BLOCCO_S
            if ha_parlato and silenzio_s >= _PAUSA_CHIUSURA_S:
                break  # frase finita: c'è stata una pausa vera dopo il parlato
            if not ha_parlato and durata >= attesa_max_s:
                return None  # nessuno ha parlato: non è un errore, solo silenzio
            if durata >= _DURATA_MAX_S:
                break  # tetto di sicurezza: non registriamo all'infinito

    return np.concatenate(blocchi)


def trascrivi(modello_stt, audio) -> str:
    """
    Trascrive l'audio in italiano con faster-whisper (tutto in locale).
    `vad_filter` scarta i tratti senza voce: meno allucinazioni sul silenzio.
    """
    segmenti, _info = modello_stt.transcribe(audio, language="it", vad_filter=True)
    return " ".join(s.text.strip() for s in segmenti).strip()


# --- Sintesi vocale ---------------------------------------------------------------

def pronuncia(voce_tts, testo: str) -> None:
    """
    Legge `testo` ad alta voce con Piper. Ctrl-C durante la riproduzione la
    interrompe SENZA uscire da Jarvis (utile sulle risposte lunghe).
    """
    if not testo.strip():
        return
    pezzi = [chunk.audio_int16_array for chunk in voce_tts.synthesize(testo)]
    if not pezzi:
        return
    audio = np.concatenate(pezzi)
    try:
        sd.play(audio, samplerate=voce_tts.config.sample_rate)
        sd.wait()
    except KeyboardInterrupt:
        # except mirato e motivato: il Ctrl-C qui esprime "smetti di parlare",
        # non "chiudi Jarvis". Zittiamo subito e la conversazione continua.
        sd.stop()
        console.print("[dim]🔇 riproduzione interrotta[/]")


# --- Caricamento dei modelli ------------------------------------------------------

def _carica_stt(WhisperModel):
    """Carica il modello di trascrizione (al primo avvio lo scarica, ~250 MB per 'small')."""
    nome = os.environ.get("JARVIS_STT_MODEL", "small")
    console.print(f"[dim]carico il modello di trascrizione '{nome}' (la prima volta lo scarica)…[/]")
    # CPU + int8: il compromesso onesto per girare ovunque senza GPU.
    return WhisperModel(nome, device="cpu", compute_type="int8")


def _carica_tts(PiperVoice):
    """
    Carica la voce di Piper da JARVIS_VOICE_DIR. Il download NON è automatico:
    è un passo di setup esplicito e una tantum, con il comando esatto nel messaggio.
    """
    nome = os.environ.get("JARVIS_TTS_VOICE", "it_IT-paola-medium")
    cartella = Path(
        os.environ.get("JARVIS_VOICE_DIR", Path.home() / "Jarvis-Sandbox" / "voci-piper")
    ).expanduser()
    cartella.mkdir(parents=True, exist_ok=True)  # così il comando di download trova la cartella
    percorso = cartella / f"{nome}.onnx"
    if not percorso.exists():
        raise SystemExit(
            f"Voce Piper '{nome}' non trovata in {cartella}.\n"
            "Scaricala una volta sola con:\n"
            f"  python -m piper.download_voices {nome} --data-dir {cartella}"
        )
    console.print(f"[dim]carico la voce '{nome}'…[/]")
    return PiperVoice.load(percorso)


# --- Interfaccia (callback identiche nello spirito a main.py) ---------------------

def _mostra_tool(name: str, tool_input: dict) -> None:
    """Callback: mostra i tool usati. Solo a schermo: leggerli a voce sarebbe rumore."""
    console.print(f"[dim]🔧 uso {name}({tool_input})[/]")


def _mostra_nota(messaggio: str) -> None:
    """Callback: note interne (compattazione cronologia, turni annullati...)."""
    console.print(f"[dim]🧠 {messaggio}[/]")


def _prossima_richiesta(wake: bool, stt, tts) -> str | None:
    """
    Ottiene la prossima richiesta dell'utente (o None se questo giro è andato a vuoto).

    push-to-talk: aspetta Invio, poi registra e trascrive. Testo digitato = usato così com'è.
    wake: registra in continuo; si attiva solo se la trascrizione contiene la wake word.
    """
    if not wake:
        digitato = console.input("[bold green]Invio e parla[/] [dim](o scrivi qui; 'esci' per uscire)[/] ").strip()
        if digitato:
            return digitato  # ripiego tastiera: quello che hai scritto vale come richiesta
        console.print("[dim]🎤 parla pure… (una pausa chiude il turno)[/]")
        audio = registra_fino_al_silenzio()
        if audio is None:
            console.print("[dim]non ho sentito nulla.[/]")
            return None
        richiesta = trascrivi(stt, audio)
        if not richiesta:
            console.print("[dim]non ho capito (trascrizione vuota).[/]")
            return None
        return richiesta

    # Modalità wake word: un "giro" di ascolto; il chiamante ci richiama in loop.
    audio = registra_fino_al_silenzio(attesa_max_s=float("inf"))
    if audio is None:
        return None
    testo = trascrivi(stt, audio)
    if not testo:
        return None
    dopo_wake = estrai_dopo_wake(testo)
    if dopo_wake is None:
        return None  # parlato senza wake word: non era rivolto a Jarvis
    if dopo_wake:
        return dopo_wake  # "Jarvis, che ore sono?" -> richiesta già completa
    # Solo "Jarvis": rispondiamo a voce e ascoltiamo la richiesta vera.
    console.print("[bold cyan]Jarvis >[/] Sì? Dimmi.")
    pronuncia(tts, "Sì? Dimmi.")
    audio = registra_fino_al_silenzio()
    if audio is None:
        console.print("[dim]non ho sentito la richiesta.[/]")
        return None
    richiesta = trascrivi(stt, audio)
    return richiesta or None


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis in modalità voce (FASE 6, opzionale).")
    parser.add_argument(
        "--wake", action="store_true",
        help='ascolto continuo con wake word ("Jarvis") invece del push-to-talk',
    )
    argomenti = parser.parse_args()

    load_dotenv()
    WhisperModel, PiperVoice = _carica_librerie_voce()

    # Controllo onesto PRIMA di scaricare/caricare i modelli: senza un microfono
    # utilizzabile la modalità voce non ha senso, meglio dirlo subito.
    try:
        sd.check_input_settings(samplerate=_CAMPIONAMENTO, channels=1)
    except Exception as e:
        # except largo ma motivato: sounddevice segnala l'assenza del microfono con
        # tipi diversi (PortAudioError, ValueError...). Non inghiottiamo nulla:
        # usciamo LOUD spiegando il problema e l'alternativa.
        raise SystemExit(
            f"Microfono non disponibile: {e}\n"
            "La modalità testo funziona comunque:  python main.py"
        ) from e

    stt = _carica_stt(WhisperModel)
    tts = _carica_tts(PiperVoice)
    agent = Agent()

    if argomenti.wake:
        console.print(
            '\n[bold cyan]Jarvis[/] è in ascolto continuo: di\' "[bold]Jarvis[/]" per attivarlo '
            "(es. \"Jarvis, che ore sono?\"). Ctrl-C per uscire.\n"
            "[dim]Le conferme di sicurezza restano da tastiera (s/N).[/]\n"
        )
    else:
        console.print(
            "\n[bold cyan]Jarvis[/] è attivo in modalità voce (push-to-talk). "
            "Premi Invio, parla, fai una pausa per chiudere. Di' o scrivi [bold]esci[/] per uscire.\n"
            "[dim]Le conferme di sicurezza restano da tastiera (s/N).[/]\n"
        )

    while True:
        try:
            richiesta = _prossima_richiesta(argomenti.wake, stt, tts)
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Arrivederci.[/]")
            break
        if richiesta is None:
            continue

        console.print(f"[bold green]Tu (voce) >[/] {richiesta}")
        if e_comando_uscita(richiesta):
            console.print("[dim]Arrivederci.[/]")
            pronuncia(tts, "A presto.")
            break

        # Stessa rete di sicurezza di main.py: un imprevisto non-API mostra l'errore
        # e tiene viva la sessione (chat() ha già ripristinato la cronologia).
        try:
            risposta = agent.chat(richiesta, on_tool=_mostra_tool, on_note=_mostra_nota)
        except Exception as e:
            console.print(f"[bold red]⚠ Errore imprevisto:[/] {e}")
            console.print("[dim]La sessione resta attiva; puoi continuare.[/]\n")
            continue

        console.print(f"[bold cyan]Jarvis >[/] {risposta}\n")
        pronuncia(tts, risposta)


if __name__ == "__main__":
    main()
