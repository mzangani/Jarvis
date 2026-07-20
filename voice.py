"""
voice.py — Guscio VOCALE opzionale di Jarvis (Fase 6).

È un WRAPPER attorno al loop: NON tocca brain.py. Flusso di un turno:

    microfono --(STT)--> testo --> agent.chat(testo) --> risposta --(TTS)--> altoparlante

Due principi guida (dettati da PIANO.md):

1) OPZIONALE E DISATTIVABILE. Il core di Jarvis resta a 3 dipendenze
   (anthropic/rich/python-dotenv). Le librerie audio — pesanti e specifiche per sistema
   operativo — stanno in `requirements-voice.txt` e si importano SOLO quando la voce
   viene davvero avviata (import GUARDATI dentro `crea_backend_reali`). Così `import voice`
   funziona sempre, e se le dipendenze mancano diamo un messaggio CHIARO, non un crash.

2) TESTABILE SENZA HARDWARE. L'orchestrazione (`ciclo_vocale`) è scritta contro un
   BACKEND iniettabile (ascolta/trascrivi/sintetizza/riproduci): con backend finti la
   provi con dei mock, senza microfono né modelli. I backend REALI stanno in
   `crea_backend_reali()` e vanno COLLAUDATI IN LOCALE — questo ambiente è headless
   (niente audio), quindi qui verifichiamo solo il flusso, non il suono.
"""

from dataclasses import dataclass
from typing import Callable, Optional

# Frasi che chiudono la sessione vocale (equivalente vocale di "esci" nella REPL testuale).
FRASI_USCITA = {"esci", "exit", "quit", "stop", "ferma", "basta", "arrivederci"}


@dataclass
class BackendVocale:
    """
    I quattro "mattoni" audio, iniettati nel ciclo. Tenendoli come funzioni possiamo
    passare quelli VERI (crea_backend_reali) oppure dei FINTI nei test.

      - ascolta()      -> cattura una frase dal microfono e la restituisce (oggetto audio)
      - trascrivi(a)   -> STT: dall'audio al testo
      - sintetizza(t)  -> TTS: dal testo all'audio
      - riproduci(a)   -> riproduce l'audio sull'altoparlante
    """
    ascolta: Callable[[], object]
    trascrivi: Callable[[object], str]
    sintetizza: Callable[[str], object]
    riproduci: Callable[[object], None]


def _parla(backend: BackendVocale, testo: str) -> None:
    """Sintetizza e riproduce una frase. Salta se il testo è vuoto (niente da dire)."""
    testo = (testo or "").strip()
    if not testo:
        return
    backend.riproduci(backend.sintetizza(testo))


def _stato(on_stato: Optional[Callable[[str], None]], messaggio: str) -> None:
    """Notifica lo stato corrente (ascolto/elaboro/parlo) a chi vuole mostrarlo a schermo."""
    if on_stato is not None:
        on_stato(messaggio)


def ciclo_vocale(
    agent,
    backend: BackendVocale,
    *,
    wake_word: Optional[str] = None,
    on_stato: Optional[Callable[[str], None]] = None,
    max_giri: Optional[int] = None,
) -> None:
    """
    Il ciclo vocale: ascolta -> trascrive -> (wake word?) -> agent.chat -> sintetizza ->
    riproduce, all'infinito finché non arriva una frase d'uscita.

    Parametri:
      - wake_word: se impostata, un turno parte SOLO se la frase inizia con questa parola
        (che viene poi rimossa dal testo passato al modello). Se None, ogni frase
        trascritta viene passata al modello.
      - on_stato: callback opzionale per mostrare lo stato ("ascolto", "elaboro", "parlo").
      - max_giri: tetto opzionale di iterazioni. Serve ai TEST per non ciclare all'infinito;
        in produzione resta None (ciclo perpetuo fino alla frase d'uscita).

    Nota (limite noto di questa prima versione): se il modello chiede un tool CAUTION/
    DANGEROUS, la CONFERMA passa ancora dalla tastiera (safety.confirm usa la console).
    Una conferma interamente vocale è un miglioramento da fare in locale.
    """
    giro = 0
    while max_giri is None or giro < max_giri:
        giro += 1

        # 1) ASCOLTO: catturiamo una frase e la trascriviamo in testo.
        _stato(on_stato, "ascolto")
        testo = backend.trascrivi(backend.ascolta()).strip()
        if not testo:
            continue  # nulla di comprensibile: riascoltiamo, senza disturbare il modello

        # 2) USCITA: una frase d'uscita chiude la sessione vocale (dopo un saluto).
        if testo.lower() in FRASI_USCITA:
            _parla(backend, "Arrivederci.")
            return

        # 3) WAKE WORD (opzionale): se richiesta, il turno parte solo se la frase la contiene
        #    in testa; poi la togliamo, così al modello arriva solo il comando vero.
        if wake_word:
            prefisso = wake_word.lower()
            if not testo.lower().startswith(prefisso):
                continue  # non attivato: ignoriamo e torniamo ad ascoltare
            testo = testo[len(wake_word):].strip()
            if not testo:
                continue  # solo la wake word, nessun comando: riascoltiamo

        # 4) PENSO: passiamo il testo al loop agentico COMPLETO (tool inclusi). chat()
        #    gestisce già API/errori e non solleva per i guasti dell'API.
        _stato(on_stato, "elaboro")
        risposta = agent.chat(testo)

        # 5) PARLO: sintetizziamo e riproduciamo la risposta.
        _stato(on_stato, "parlo")
        _parla(backend, risposta)


def crea_backend_reali(
    *,
    secondi_ascolto: float = 5.0,
    sample_rate: int = 16000,
    modello_whisper: str = "base",
    voce_piper: Optional[str] = None,
    motore_tts: Optional[str] = None,
) -> BackendVocale:
    """
    Costruisce i backend audio REALI, facendo import GUARDATI delle librerie opzionali.

    ⚠️ DA COLLAUDARE IN LOCALE. Questo ambiente cloud è headless (niente microfono/
    altoparlanti), quindi questa funzione qui non è eseguibile: i dettagli (durata di
    ascolto, sample rate, nome dei modelli, invocazione del TTS) sono un PUNTO DI
    PARTENZA ragionevole da verificare e rifinire sulla tua macchina.

    STT (voce→testo): sempre faster-whisper + sounddevice (microfono).

    TTS (testo→voce): due motori possibili, scelti da `motore_tts` (o `JARVIS_TTS`):
      - "say"   → il comando `say` INTEGRATO in macOS: nessun binario esterno né modello
                  da scaricare, arm64-nativo, zero problemi di architettura. Voce via
                  `JARVIS_SAY_VOICE` (es. "Alice"/"Luca" per l'italiano; default: di sistema).
      - "piper" → il binario esterno `piper` con un modello voce .onnx (TTS neurale
                  locale, multipiattaforma). `voce_piper` è il PERCORSO del modello .onnx
                  (estensione inclusa: piper non lo indovina da un nome logico); se non
                  passato si legge da `JARVIS_PIPER_MODEL`.
      - "auto"  → default: "say" su macOS, "piper" altrove.

    Se le dipendenze STT non sono installate, solleviamo un RuntimeError CHIARO con le
    istruzioni, invece di un ImportError oscuro.
    """
    import os
    import platform

    if voce_piper is None:
        voce_piper = os.environ.get("JARVIS_PIPER_MODEL", "it_IT-riccardo-x_low.onnx")
    if motore_tts is None:
        motore_tts = os.environ.get("JARVIS_TTS", "auto").strip().lower()
    if motore_tts == "auto":
        # Su un Mac 'say' è sempre presente e nativo: è il default ovvio. Altrove piper.
        motore_tts = "say" if platform.system() == "Darwin" else "piper"

    try:
        import numpy as np
        import sounddevice as sd
        from faster_whisper import WhisperModel
    except ImportError as e:
        # e.name = nome del modulo mancante. Messaggio azionabile, non un traceback grezzo.
        raise RuntimeError(
            f"Dipendenze della voce mancanti (manca '{e.name}'). "
            "Installa con: pip install -r requirements-voice.txt "
            "(e assicurati di avere il binario 'piper' con un modello voce; vedi README)."
        ) from e

    # Il modello STT si carica UNA sola volta: è costoso. 'base' è un buon compromesso
    # accuratezza/velocità; modelli più grandi sono più precisi ma più lenti e pesanti.
    modello = WhisperModel(modello_whisper)

    def ascolta():
        # VERSIONE SEMPLICE: registriamo una finestra di durata FISSA. In locale valuta
        # una rilevazione del silenzio (VAD) per non tagliare le frasi lunghe/corte.
        audio = sd.rec(
            int(secondi_ascolto * sample_rate),
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
        )
        sd.wait()  # blocca finché la registrazione non è finita
        return audio.reshape(-1)  # array mono 1-D, il formato che si aspetta whisper

    def trascrivi(audio) -> str:
        # faster-whisper: transcribe() restituisce (segmenti, info); uniamo i testi.
        segmenti, _info = modello.transcribe(audio, language="it")
        return " ".join(seg.text for seg in segmenti).strip()

    # ---- TTS: due motori. Definiamo sintetizza/riproduci in base a `motore_tts`. ----
    if motore_tts == "say":
        # macOS 'say': parla direttamente, senza binari esterni né problemi di
        # architettura. Nel nostro modello a due passi (sintetizza -> riproduci)
        # sintetizza è l'IDENTITÀ (il "suono" è il testo stesso) e riproduci lo fa
        # pronunciare a `say`, che legge il testo da stdin (niente limiti di ARG_MAX
        # né problemi di quoting). Voce opzionale via JARVIS_SAY_VOICE.
        voce_say = os.environ.get("JARVIS_SAY_VOICE", "").strip()

        def sintetizza(testo: str):
            return testo

        def riproduci(testo) -> None:
            import subprocess
            cmd = ["say"]
            if voce_say:
                cmd += ["-v", voce_say]
            proc = subprocess.run(cmd, input=(testo or "").encode("utf-8"),
                                  capture_output=True)
            if proc.returncode != 0:
                dettaglio = proc.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"il comando 'say' è fallito (codice {proc.returncode}): "
                    f"{dettaglio or '(nessun messaggio su stderr)'}"
                )
    else:
        # piper: binario esterno + modello .onnx (TTS neurale locale). Produce un WAV
        # in memoria, che riproduci() suona via sounddevice.
        def sintetizza(testo: str):
            import subprocess
            proc = subprocess.run(
                ["piper", "--model", voce_piper, "--output_file", "-"],
                input=testo.encode("utf-8"),
                capture_output=True,
                # NIENTE check=True: CalledProcessError non mostra stderr nel traceback
                # di default, e senza lo stderr di piper (crash C++, modello mancante,
                # architettura sbagliata, ecc.) la diagnosi è alla cieca. Controlliamo a
                # mano e lo includiamo nel messaggio.
            )
            if proc.returncode != 0:
                dettaglio = proc.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"piper è fallito (codice {proc.returncode}, segnale se negativo): "
                    f"{dettaglio or '(nessun messaggio su stderr)'}"
                )
            return proc.stdout  # bytes di un file WAV

        def riproduci(audio_wav) -> None:
            # Riproduce i byte WAV prodotti da piper sull'altoparlante.
            import io
            import wave
            with wave.open(io.BytesIO(audio_wav), "rb") as w:
                frame = w.readframes(w.getnframes())
                sr = w.getframerate()
            dati = np.frombuffer(frame, dtype=np.int16)
            sd.play(dati, samplerate=sr)
            sd.wait()

    return BackendVocale(
        ascolta=ascolta, trascrivi=trascrivi, sintetizza=sintetizza, riproduci=riproduci
    )


def avvia_voce(agent, *, console=None, wake_word: Optional[str] = None) -> None:
    """
    Comodità per main.py: crea i backend reali e avvia il ciclo vocale.

    Può sollevare RuntimeError se le dipendenze audio mancano (lo gestisce main.py, che
    in quel caso ripiega sulla REPL testuale). `console` (rich) è opzionale, solo per
    mostrare lo stato a schermo: coerente con l'architettura, la logica non stampa da sé.
    """
    backend = crea_backend_reali()  # può sollevare RuntimeError (deps mancanti)

    def on_stato(messaggio: str) -> None:
        if console is not None:
            console.print(f"[dim]🎤 {messaggio}…[/]")

    if console is not None:
        console.print("[bold cyan]Modalità voce attiva.[/] Di' [bold]«esci»[/] per uscire.\n")
    ciclo_vocale(agent, backend, wake_word=wake_word, on_stato=on_stato)
