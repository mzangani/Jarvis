"""
main.py — Interfaccia a riga di comando (REPL) per Jarvis, Fase 1.

REPL = Read-Eval-Print Loop: leggi input, elabora, stampa, ripeti.
Questo file NON conosce l'API Anthropic: parla soltanto con la classe Agent.
Questa separazione ci permetterà, più avanti, di cambiare interfaccia
(voce, GUI, ...) senza toccare il cervello dell'agente.
"""

from dotenv import load_dotenv
from rich.console import Console

from brain import Agent

console = Console()


def main() -> None:
    # Carica le variabili da un file .env (in particolare ANTHROPIC_API_KEY),
    # così non dobbiamo esportarle a mano nel terminale ogni volta.
    load_dotenv()

    agent = Agent()

    console.print(
        "[bold cyan]Jarvis[/] è attivo (Fase 1). "
        "Scrivi [bold]esci[/] per terminare.\n"
    )

    # Il ciclo del REPL: gira finché non decidiamo di uscire.
    while True:
        try:
            user_input = console.input("[bold green]Tu >[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            # Ctrl-D o Ctrl-C: usciamo in modo pulito senza traceback.
            console.print("\n[dim]Arrivederci.[/]")
            break

        if not user_input:
            continue  # riga vuota: ignora e richiedi input
        if user_input.lower() in {"esci", "exit", "quit"}:
            console.print("[dim]Arrivederci.[/]")
            break

        # Deleghiamo tutto il lavoro "intelligente" all'agente.
        reply = agent.chat(user_input)
        console.print(f"[bold cyan]Jarvis >[/] {reply}\n")


if __name__ == "__main__":
    main()
