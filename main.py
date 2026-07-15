"""
main.py — Interfaccia a riga di comando (REPL) per Jarvis, Fase 2.

REPL = Read-Eval-Print Loop: leggi input, elabora, stampa, ripeti.
Questo file NON conosce l'API Anthropic: parla soltanto con la classe Agent.
Questa separazione ci permetterà, più avanti, di cambiare interfaccia
(voce, GUI, ...) senza toccare il cervello dell'agente.
"""

from dotenv import load_dotenv
from rich.console import Console

from brain import Agent

console = Console()


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

    console.print(
        "[bold cyan]Jarvis[/] è attivo (Fase 2). "
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
