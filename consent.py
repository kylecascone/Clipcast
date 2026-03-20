"""
consent.py
==========
First-run consent screen for ClipCast Studio.

Displays a brief, friendly summary of the Terms of Service key points and
requires the user to type 'agree' to continue. Consent is recorded in the
database with a timestamp, so the screen only ever appears once per user.

This is designed to feel helpful and transparent, not scary. The goal is to
make sure users understand they're responsible for the content they use and
that original creators should always be credited.

Integration:
    Call check_consent() near the start of any live command in main.py.
    It returns immediately and silently if the user has already consented.

Test:
    python consent.py
"""

import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Bump this string whenever the Terms materially change to require re-confirmation.
CONSENT_VERSION = "1.0"


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def check_consent(user_id: int = 1) -> None:
    """
    Ensure the user has accepted the Terms of Service.

    If consent for the current version is already recorded in the database,
    this function returns immediately without printing anything.

    If consent has not been given, shows the consent screen. Exits the
    process gracefully if the user declines.

    Args:
        user_id: User ID (default 1 for single-user mode).
    """
    import database
    database.initialize_database()

    if database.has_consented(user_id=user_id, version=CONSENT_VERSION):
        return

    _show_consent_screen(user_id=user_id)


# ══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

def _show_consent_screen(user_id: int = 1) -> None:
    """
    Display the consent panel and prompt. Exits if the user declines.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt

    console = Console()

    console.print()
    console.print(
        Panel(
            "[bold cyan]Welcome to ClipCast Studio[/bold cyan]\n\n"
            "Before we get started, here are the key things to know — in plain English:\n\n"
            "  [green]✓[/green]  [bold]You're responsible for what you post.[/bold] Only use clips from\n"
            "     streamers who are okay with their content being shared.\n\n"
            "  [green]✓[/green]  [bold]Creator credit is automatic and cannot be turned off.[/bold]\n"
            "     Every video will include the original creator's name.\n\n"
            "  [green]✓[/green]  [bold]Follow each platform's rules.[/bold] ClipCast posts to TikTok,\n"
            "     YouTube Shorts, and Instagram on your behalf — their Terms apply.\n\n"
            "  [green]✓[/green]  [bold]If a creator asks you to take something down, please do.[/bold]\n"
            "     We have an opt-out list for this reason.\n\n"
            "  [green]✓[/green]  [bold]You must be 18 or older to use ClipCast Studio.[/bold]\n\n"
            "  [green]✓[/green]  [bold]ClipCast is a tool — you're the operator.[/bold] All responsibility\n"
            "     for content decisions rests with you.\n\n"
            "These terms protect both you and the creators whose work you're building on.\n"
            "Full details in [bold]legal/TERMS_OF_SERVICE.md[/bold].",
            title="[bold yellow]A Quick Note Before We Begin[/bold yellow]",
            border_style="cyan",
            padding=(1, 2),
        )
    )

    console.print()
    response = Prompt.ask(
        "Type [bold green]agree[/bold green] to accept and continue"
    ).strip().lower()

    if response != "agree":
        console.print()
        console.print(
            "[yellow]No problem. ClipCast Studio requires acceptance of the Terms of Service\n"
            "to operate — this protects both you and the creators whose content you'd be\n"
            "working with. You can read the full terms in [bold]legal/TERMS_OF_SERVICE.md[/bold]\n"
            "and run any ClipCast command again when you're ready.[/yellow]"
        )
        console.print()
        sys.exit(0)

    import database
    database.record_consent(user_id=user_id, version=CONSENT_VERSION)

    console.print()
    console.print("[green]✓ Terms accepted. Let's get started.[/green]")
    console.print()


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    print("Testing consent.py...")
    print("This will show the consent screen on first run.")
    print("After agreeing, run again — the screen should be skipped automatically.\n")

    import database
    database.initialize_database()
    check_consent(user_id=1)
    print("Consent check passed. System ready.")
