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
        reply = agent.chat(user_input, on_tool=_mostra_tool, on_note=_mostra_nota)
        console.print(f"[bold cyan]Jarvis >[/] {reply}\n")


if __name__ == "__main__":
    main()
