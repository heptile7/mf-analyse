"""Rich console and interactive prompt helpers."""

import re

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table


console = Console()


# ══════════════════════════════════════════════════════════════════════
# RICH UI HELPERS
# ══════════════════════════════════════════════════════════════════════

def fmt_inr(val: float) -> str:
    if val >= 1_00_00_000: return f"₹{val/1_00_00_000:.2f} Cr"
    if val >= 1_00_000:    return f"₹{val/1_00_000:.2f} L"
    return f"₹{val:,.0f}"

def rich_header(step: str, title: str, hint: str = "") -> None:
    body = f"[bold white]{title}[/bold white]"
    if hint:
        body += f"\n[dim]{hint}[/dim]"
    console.print(Panel(body, title=f"[cyan]{step}[/cyan]",
                        border_style="cyan", padding=(0, 2)))

def rich_ask_choice(prompt: str, options: list, hint: str = "", default: int = 0) -> int:
    if hint:
        console.print(f"  [dim]{hint}[/dim]")
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column("n",  style="bold cyan", width=3)
    t.add_column("opt", style="white")
    for i, o in enumerate(options, 1):
        marker = " [dim](default)[/dim]" if (i - 1) == default else ""
        t.add_row(str(i), f"{o}{marker}")
    console.print(t)
    while True:
        raw = Prompt.ask(f"  [green]{prompt}[/green]", default=str(default + 1))
        if raw.strip().isdigit():
            idx = int(raw.strip())
            if 1 <= idx <= len(options):
                return idx - 1
        console.print("  [red]Please enter a valid number.[/red]")

def rich_ask_int(prompt: str, min_val: int, max_val: int, hint: str = "") -> int:
    if hint:
        console.print(f"  [dim]{hint}[/dim]")
    while True:
        raw = Prompt.ask(f"  [green]{prompt}[/green]")
        digits = re.sub(r"[^\d]", "", raw)
        if digits:
            v = int(digits)
            if min_val <= v <= max_val:
                return v
        console.print(f"  [red]Enter a value between {min_val:,} and {max_val:,}.[/red]")

def rich_ask_corpus(hint: str = "") -> float | None:
    """Parse corpus target: '50L', '1Cr', '25,00,000', or blank/skip."""
    if hint:
        console.print(f"  [dim]{hint}[/dim]")
    raw = Prompt.ask("  [green]Target corpus (or press Enter to skip)[/green]",
                     default="").strip().lower()
    if not raw or raw in ("skip", "s", "none", "n"):
        return None
    raw_clean = raw.replace(",", "").replace("₹", "").strip()
    try:
        if raw_clean.endswith("cr"):
            return float(raw_clean[:-2]) * 1e7
        if raw_clean.endswith("l"):
            return float(raw_clean[:-1]) * 1e5
        return float(raw_clean)
    except ValueError:
        console.print("  [red]Could not parse — skipping corpus target.[/red]")
        return None

def rich_summary(items: list) -> None:
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column("k", style="dim", width=22)
    t.add_column("v", style="bold white")
    for k, v in items:
        t.add_row(k, str(v))
    console.print(Panel(t, title="[bold cyan]Summary so far[/bold cyan]",
                        border_style="dim cyan", padding=(0, 1)))


def pct_str(v) -> str: return f"{v:.1f}%" if v is not None else "—"
def val_str(v, dp=2) -> str: return f"{v:.{dp}f}" if v is not None else "—"
