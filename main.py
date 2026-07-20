"""
main.py — Interfaccia a riga di comando (REPL) per Jarvis, Fase 2.

REPL = Read-Eval-Print Loop: leggi input, elabora, stampa, ripeti.
Questo file NON conosce l'API Anthropic: parla soltanto con la classe Agent.
Questa separazione ci permetterà, più avanti, di cambiare interfaccia
(voce, GUI, ...) senza toccare il cervello dell'agente.
"""

import os
import sys

from dotenv import load_dotenv
from rich.console import Console

from brain import Agent

console = Console()


def _voce_richiesta() -> bool:
    """
    True se l'utente ha chiesto la modalità VOCE (Fase 6): flag `--voce` sulla riga di
    comando oppure variabile d'ambiente `JARVIS_VOICE` a un valore "vero". Di default è
    disattivata: Jarvis parte in modalità testo e il core non tocca le dipendenze audio.
    """
    if "--voce" in sys.argv:
        return True
    return os.environ.get("JARVIS_VOICE", "").strip().lower() in {"1", "true", "si", "sì", "yes", "on"}


def _mostra_tool(name: str, tool_input: dict) -> None:
    """Callback: mostra a schermo quando Jarvis usa un tool (solo estetica)."""
    console.print(f"[dim]🔧 uso {name}({tool_input})[/]")


def _mostra_nota(messaggio: str) -> None:
    """Callback: mostra le note interne di Jarvis, es. la compattazione della cronologia."""
    console.print(f"[dim]🧠 {messaggio}[/]")


def main() -> None:
    # Carica le variabili da un file .env (in particolare ANTHROPIC_API_KEY).
    load_dotenv()

    agent = Agent()

    # MODALITÀ VOCE (Fase 6, OPZIONALE). È un guscio attorno allo stesso `agent`: il loop
    # non cambia. Le dipendenze audio sono pesanti e opzionali, quindi importiamo `voice`
    # solo qui, su richiesta. Se mancano, `avvia_voce` solleva un RuntimeError chiaro: lo
    # mostriamo e RIPIEGHIAMO sulla REPL testuale, invece di far morire il programma.
    if _voce_richiesta():
        import voice  # import locale: `voice` non tira dentro l'audio finché non si avvia
        try:
            voice.avvia_voce(agent, console=console)
            return  # sessione vocale conclusa (l'utente ha detto "esci"): fine.
        except RuntimeError as e:
            console.print(f"[yellow]Voce non disponibile:[/] {e}")
            console.print("[dim]Continuo in modalità testo.[/]\n")

    console.print(
        "[bold cyan]Jarvis[/] è attivo. "
        "Prova: [italic]quanto spazio ho sul disco?[/] — Scrivi [bold]esci[/] per terminare.\n"
    )

    while True:
        try:
            user_input = console.input("[bold green]Tu >[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Arrivederci.[/]")
            break

        if not user_input:
            continue
        if user_input.lower() in {"esci", "exit", "quit"}:
            console.print("[dim]Arrivederci.[/]")
            break

        # Passiamo le callback: così vediamo i tool usati e le note interne
        # (es. quando Jarvis compatta la cronologia troppo lunga).
        #
        # RETE DI SICUREZZA (difesa in profondità, Fase 7b): brain.chat() gestisce già i
        # guasti dell'API senza crashare e, qualunque cosa vada storta a metà turno,
        # ripristina una cronologia coerente PRIMA di propagare (vedi il checkpoint in
        # chat()). Questo except è l'ultima linea: cattura QUALSIASI imprevisto non-API
        # (un bug nostro, un EOFError da una conferma, un OSError...) e tiene viva la
        # sessione mostrando l'errore. È LARGO ma NON è un `except: pass`: stampa
        # l'errore (fail loud) e prosegue. NON cattura KeyboardInterrupt (è BaseException,
        # non Exception): così Ctrl-C durante una chiamata lunga interrompe ancora il
        # programma, come è giusto.
        try:
            reply = agent.chat(user_input, on_tool=_mostra_tool, on_note=_mostra_nota)
        except Exception as e:
            console.print(f"[bold red]⚠ Errore imprevisto:[/] {e}")
            console.print("[dim]La sessione resta attiva; puoi continuare.[/]\n")
            continue
        console.print(f"[bold cyan]Jarvis >[/] {reply}\n")


if __name__ == "__main__":
    main()
