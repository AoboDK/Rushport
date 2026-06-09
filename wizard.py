"""rushport interactive wizard."""
from __future__ import annotations

import asyncio
from pathlib import Path

import questionary
import yaml
from rich.console import Console

from auth.microsoft import MicrosoftAuth, MicrosoftAuthError
from auth.rushfiles import RushfilesAuth, RushfilesAuthError
from clients.rushfiles import RushfilesClient
from config import Config, ConfigError, load_config
from destinations.onedrive import create_onedrive_client
from sync.engine import SyncEngine, SyncStats, _human_bytes
from sync.state import StateDB
from sync.transfer import sanitize_name

console = Console()
CONFIG_PATH = Path("config.yaml")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _print_step(n: int, title: str) -> None:
    console.print(f"\n[bold cyan]── Step {n}: {title} ──[/]")


def _print_skip(message: str) -> None:
    console.print(f"[dim green]✓ {message}[/]")


def _print_error(message: str, hint: str = "") -> None:
    console.print(f"[red]{message}[/]")
    if hint:
        console.print(f"[dim]{hint}[/]")


def _handle_interrupt() -> None:
    console.print("\n[dim]Wizard cancelled.[/]")
    raise SystemExit(0)


# ── Step stubs (filled in by subsequent tasks) ────────────────────────────────

def step_config_setup() -> Config:
    raise NotImplementedError


async def step_rf_auth(cfg: Config) -> RushfilesAuth:
    raise NotImplementedError


def step_ms_auth(cfg: Config) -> MicrosoftAuth:
    raise NotImplementedError


async def step_pick_share(cfg: Config, rf_auth: RushfilesAuth) -> list[tuple[str, str]]:
    raise NotImplementedError


def step_transfer_options(cfg: Config, shares: list[tuple[str, str]]) -> dict:
    raise NotImplementedError


async def step_run_transfer(cfg: Config, rf_auth: RushfilesAuth, options: dict) -> None:
    raise NotImplementedError


# ── Wizard loop ───────────────────────────────────────────────────────────────

async def _wizard() -> None:
    cfg = step_config_setup()
    rf_auth = await step_rf_auth(cfg)
    step_ms_auth(cfg)

    while True:
        shares = await step_pick_share(cfg, rf_auth)
        options = step_transfer_options(cfg, shares)

        confirmed = questionary.confirm("Start transfer?", default=True).ask()
        if confirmed is None or not confirmed:
            console.print("[dim]Transfer cancelled.[/]")
        else:
            await step_run_transfer(cfg, rf_auth, options)

        again = questionary.confirm("Transfer another share?", default=False).ask()
        if not again:
            console.print("\n[dim]All done. Goodbye![/]")
            break


def main() -> None:
    console.print("[bold cyan]rushport[/] — Rushfiles migration wizard\n")
    try:
        asyncio.run(_wizard())
    except KeyboardInterrupt:
        _handle_interrupt()
