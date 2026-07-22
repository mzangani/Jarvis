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
   provi con dei mock, senza microfono né modelli. Anche la LOGICA di rilevazione del
   silenzio è isolata in una classe pura (`RilevatoreFine`), testabile con delle sequenze
   di energia senza audio. I backend REALI stanno in `crea_backend_reali()` e vanno
   COLLAUDATI IN LOCALE — questo ambiente è headless (niente audio), quindi qui
   verifichiamo solo il flusso e le decisioni, non il suono.
"""

import re
from dataclasses import dataclass
from typing import Callable, Optional

# Frasi che chiudono la sessione vocale (equivalente vocale di "esci" nella REPL testuale).
FRASI_USCITA = {"esci", "exit", "quit", "stop", "ferma", "basta", "arrivederci"}

# Emoji e pittogrammi vari: un sintetizzatore li leggerebbe come nomi ("faccina...") o li
# storpierebbe. Copriamo i blocchi Unicode più comuni (non è esaustivo, ma prende il grosso).
_EMOJI = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # simboli & pittogrammi, emoticon, oggetti, ecc.
    "\U00002600-\U000027BF"  # simboli vari & dingbats
    "\U00002B00-\U00002BFF"  # frecce e simboli
    "\U0001F1E6-\U0001F1FF"  # bandiere (regional indicators)
    "\U0000FE00-\U0000FE0F"  # selettori di variazione
    "\U00002190-\U000021FF"  # frecce
    "\U00002700-\U000027BF"
    "]+",
    flags=re.UNICODE,
)


def pulisci_per_voce(testo: str) -> str:
    """
    Ripulisce il testo PRIMA di darlo al sintetizzatore, così NON legge la formattazione
    "a voce alta". È una funzione PURA (nessun audio): togliamo ciò che ha senso a schermo
    ma è rumore parlato — blocchi di codice, grassetti/asterischi, elenchi puntati, titoli
    markdown, link, emoji — e normalizziamo gli spazi.

    Non pretende di essere un parser markdown completo: è una ripulitura ROBUSTA e onesta
    per la sintesi vocale, pensata per le risposte tipiche di Jarvis.
    """
    if not testo:
        return ""
    t = testo
    # 1) Blocchi di codice ``` ... ```: leggerli a voce è inutile. Via del tutto.
    t = re.sub(r"```.*?```", " ", t, flags=re.DOTALL)
    # 2) Immagini e link markdown: teniamo il testo, buttiamo l'URL. ![alt](u) / [testo](u)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)
    # 3) URL "nudi": in voce non servono (e verrebbero sillabati). Via.
    t = re.sub(r"https?://\S+", " ", t)
    # 4) Codice inline `x`: togliamo solo i backtick, teniamo il contenuto.
    t = t.replace("`", "")
    # 5) Enfasi in coppia **grassetto** / __grassetto__: togliamo i marcatori.
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    t = re.sub(r"__([^_]+)__", r"\1", t)
    # 6) A inizio riga: titoli (#), citazioni (>) e marcatori di elenco (-, *, +, •, "1.").
    #    Diventano frasi normali; il "a capo" fa già da pausa per il sintetizzatore.
    t = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]+", "", t)      # titoli
    t = re.sub(r"(?m)^[ \t]*>[ \t]?", "", t)            # citazioni
    t = re.sub(r"(?m)^[ \t]*[-*+•][ \t]+", "", t)       # elenchi puntati
    t = re.sub(r"(?m)^[ \t]*\d+[.)][ \t]+", "", t)      # elenchi numerati
    # 7) Asterischi/underscore rimasti (enfasi singola, ecc.): a spazio, non incollare parole.
    t = re.sub(r"[*_]+", " ", t)
    # 8) Emoji e pittogrammi.
    t = _EMOJI.sub("", t)
    # 9) Normalizza spazi: niente run di spazi, e max una riga vuota di separazione.
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _scegli_voce_da_elenco(elenco: str) -> Optional[str]:
    """
    Data l'uscita di `say -v '?'`, sceglie una voce ITALIANA (lingua it_*), preferendo
    quelle di qualità superiore ("Premium"/"Enhanced", che suonano molto meglio). Ritorna
    il NOME della voce, o None se non c'è nessuna voce italiana. PURA e testabile: il
    comando `say` vero lo lancia chi la usa, qui parsiamo solo il testo.

    Formato tipico di una riga:  "Alice               it_IT    # Ciao, mi chiamo Alice."
    (il nome può contenere spazi o parentesi, es. "Alice (Enhanced)"): separiamo sul codice
    lingua `xx_XX`, preceduto da almeno due spazi.
    """
    italiane = []
    for riga in elenco.splitlines():
        m = re.match(r"^(?P<nome>.+?)\s{2,}(?P<lang>[a-z]{2}_[A-Z]{2})\b", riga)
        if not m:
            continue
        if m.group("lang").startswith("it"):
            italiane.append(m.group("nome").strip())
    if not italiane:
        return None
    # Preferiamo una voce di qualità superiore, se c'è; altrimenti la prima italiana.
    for voce in italiane:
        if re.search(r"premium|enhanced", voce, flags=re.IGNORECASE):
            return voce
    return italiane[0]


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
    """Sintetizza e riproduce una frase, dopo averla RIPULITA per la voce (niente
    markdown/emoji letti a voce). Salta se non resta nulla da dire."""
    testo = pulisci_per_voce(testo)
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


class RilevatoreFine:
    """
    Rilevazione del silenzio (VAD "a energia"): decide QUANDO smettere di registrare,
    così una frase non viene tagliata a metà né costringe ad aspettare una finestra fissa.

    È PURA e TESTABILE: consuma una sequenza di valori di ENERGIA (RMS per blocco audio) e
    non tocca l'hardware — la cattura vera dei blocchi la fa `ascolta()`. Macchina a due
    stati:

      - ATTESA: aspettiamo che la voce COMINCI. Se non arriva entro `attesa_inizio`,
        chiudiamo comunque (nessun parlato → frase vuota → il ciclo riascolta).
      - PARLATO: stiamo registrando. Ogni blocco "silenzioso" consecutivo avvicina la
        fine; `silenzio_fine` secondi di silenzio di fila chiudono la frase. Un blocco
        "parlato" AZZERA il conteggio del silenzio, così le pause brevi non tagliano.

    In entrambi gli stati, `durata_massima` è un tetto assoluto (mai registrare all'infinito).
    Le durate (in secondi) sono convertite in numero di blocchi tramite `blocco` (durata di
    un blocco). Un'istanza serve per UNA frase: `ascolta()` ne crea una nuova a ogni giro.
    """

    def __init__(self, *, soglia: float, blocco: float, silenzio_fine: float,
                 attesa_inizio: float, durata_massima: float) -> None:
        self.soglia = soglia
        # Da secondi a numero di blocchi (almeno 1, per non degenerare con blocchi grandi).
        self._blocchi_silenzio_fine = max(1, round(silenzio_fine / blocco))
        self._blocchi_attesa = max(1, round(attesa_inizio / blocco))
        self._blocchi_massimi = max(1, round(durata_massima / blocco))
        self._parlato_iniziato = False
        self._silenzio_consecutivi = 0
        self._totali = 0

    @property
    def parlato_iniziato(self) -> bool:
        """True se a un certo punto la voce è cominciata (utile a chi chiama per capire
        se la frase è 'vuota' — solo silenzio — o reale)."""
        return self._parlato_iniziato

    def considera(self, energia: float) -> bool:
        """Registra un blocco data la sua energia RMS. Ritorna True quando si deve
        SMETTERE di registrare (fine frase, timeout d'attesa, o tetto massimo)."""
        self._totali += 1
        parlato = energia >= self.soglia

        if not self._parlato_iniziato:
            if parlato:
                self._parlato_iniziato = True
            elif self._totali >= self._blocchi_attesa:
                return True  # nessuno ha parlato entro l'attesa: chiudiamo (frase vuota)

        if self._parlato_iniziato:
            if parlato:
                self._silenzio_consecutivi = 0
            else:
                self._silenzio_consecutivi += 1
                if self._silenzio_consecutivi >= self._blocchi_silenzio_fine:
                    return True  # abbastanza silenzio dopo il parlato: fine frase

        if self._totali >= self._blocchi_massimi:
            return True  # tetto assoluto: non registrare più a lungo di così
        return False


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

    # Rilevazione del silenzio (VAD) per l'ASCOLTO: attiva di default, disattivabile con
    # JARVIS_VAD=0 (ripiego sulla finestra fissa). La SOGLIA dipende dal microfono/rumore
    # di fondo: se Jarvis parte a registrare da solo (troppo sensibile) alzala, se non ti
    # sente (troppo alta) abbassala. Le altre durate hanno default sensati.
    usa_vad = os.environ.get("JARVIS_VAD", "1").strip().lower() not in {"0", "false", "no", "off"}
    vad_soglia = float(os.environ.get("JARVIS_VAD_SOGLIA", "0.015"))
    vad_blocco = 0.03  # durata di un blocco audio (s): 30 ms, granularità comoda per il VAD

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

    def _ascolta_finestra_fissa():
        # RIPIEGO (JARVIS_VAD=0): registriamo una finestra di durata FISSA. Semplice ma
        # taglia le frasi lunghe e fa aspettare su quelle corte.
        audio = sd.rec(
            int(secondi_ascolto * sample_rate),
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
        )
        sd.wait()  # blocca finché la registrazione non è finita
        return audio.reshape(-1)  # array mono 1-D, il formato che si aspetta whisper

    def _ascolta_con_vad():
        # Leggiamo a BLOCCHI e ci fermiamo quando il rilevatore dice "fine frase" (silenzio
        # dopo il parlato), evitando la finestra fissa. Il rilevatore è nuovo a ogni frase
        # (lo stato non va riusato). La logica di DECISIONE è pura (RilevatoreFine, testata);
        # qui c'è solo la cattura dei blocchi dal microfono e il calcolo dell'energia.
        dim_blocco = max(1, int(vad_blocco * sample_rate))  # campioni per blocco
        rilevatore = RilevatoreFine(
            soglia=vad_soglia, blocco=vad_blocco,
            silenzio_fine=0.8, attesa_inizio=4.0, durata_massima=15.0,
        )
        blocchi = []
        with sd.InputStream(samplerate=sample_rate, channels=1,
                            dtype="float32", blocksize=dim_blocco) as stream:
            while True:
                dati, _overflow = stream.read(dim_blocco)  # blocco (dim_blocco, 1) float32
                mono = dati.reshape(-1)
                blocchi.append(mono)
                energia = float(np.sqrt(np.mean(mono ** 2))) if mono.size else 0.0
                if rilevatore.considera(energia):
                    break
        if not blocchi:
            return np.zeros(0, dtype="float32")
        return np.concatenate(blocchi)

    def ascolta():
        return _ascolta_con_vad() if usa_vad else _ascolta_finestra_fissa()

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
        # Se l'utente non ha scelto una voce, proviamo a trovarne una ITALIANA (la voce di
        # sistema è spesso inglese e pronuncia male l'italiano). Best-effort: se `say -v ?`
        # non è interrogabile, restiamo sulla voce di sistema senza far fallire nulla.
        if not voce_say:
            import subprocess
            try:
                elenco = subprocess.run(["say", "-v", "?"], capture_output=True,
                                        timeout=5).stdout.decode("utf-8", errors="replace")
                voce_say = _scegli_voce_da_elenco(elenco) or ""
            except (OSError, subprocess.SubprocessError):
                voce_say = ""  # nessuna scelta automatica: voce di sistema

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

    `wake_word`: se non passata, si legge da JARVIS_WAKE_WORD (es. "jarvis"): con la
    wake word attiva, solo le frasi che INIZIANO con quella parola arrivano al modello
    — TV, altre persone e conversazioni di sottofondo vengono ignorate. Le frasi
    d'uscita ("esci") funzionano comunque anche senza prefisso.
    """
    if wake_word is None:
        import os
        wake_word = os.environ.get("JARVIS_WAKE_WORD", "").strip() or None

    backend = crea_backend_reali()  # può sollevare RuntimeError (deps mancanti)

    # Diciamo all'agente che ora parla a VOCE: risponderà breve e senza formattazione
    # (il registro "da schermo" verrebbe letto malissimo). In testo questo resta False.
    agent.modalita_voce = True

    def on_stato(messaggio: str) -> None:
        if console is not None:
            console.print(f"[dim]🎤 {messaggio}…[/]")

    if console is not None:
        console.print("[bold cyan]Modalità voce attiva.[/] Di' [bold]«esci»[/] per uscire.\n")
    ciclo_vocale(agent, backend, wake_word=wake_word, on_stato=on_stato)
