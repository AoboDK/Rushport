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
    try:
        cfg = load_config()
        _print_skip("Config already set up")
        return cfg
    except ConfigError:
        pass

    _print_step(1, "RushFiles Setup")

    existing: dict = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            existing = yaml.safe_load(f) or {}

    rf = existing.get("rushfiles", {})
    ms = existing.get("microsoft", {})
    tr = existing.get("transfer", {})
    st = existing.get("state", {})

    clientgateway = questionary.text(
        "RushFiles clientgateway URL:", default=rf.get("clientgateway_base", "")
    ).ask()
    if clientgateway is None:
        _handle_interrupt()

    filecache = questionary.text(
        "RushFiles filecache URL (leave blank to auto-discover):",
        default=rf.get("filecache_base", ""),
    ).ask()
    if filecache is None:
        _handle_interrupt()

    app_mode = questionary.select(
        "Microsoft app mode:",
        choices=["Shared rushport app (recommended)", "My own Entra app"],
        default="Shared rushport app (recommended)",
    ).ask()
    if app_mode is None:
        _handle_interrupt()

    client_id = ""
    tenant_id = "common"
    if app_mode == "My own Entra app":
        client_id = questionary.text(
            "Microsoft Entra client ID:", default=ms.get("client_id", "")
        ).ask()
        if client_id is None:
            _handle_interrupt()
        tenant_id = questionary.text(
            "Tenant ID:", default=ms.get("tenant_id", "common")
        ).ask()
        if tenant_id is None:
            _handle_interrupt()

    concurrency = questionary.text(
        "Concurrent uploads:", default=str(tr.get("concurrency", 4))
    ).ask()
    if concurrency is None:
        _handle_interrupt()

    chunk_size_mb = questionary.text(
        "Chunk size (MB):", default=str(tr.get("chunk_size_mb", 10))
    ).ask()
    if chunk_size_mb is None:
        _handle_interrupt()

    data = {
        "rushfiles": {
            "email": rf.get("email", ""),
            "password": rf.get("password", ""),
            "clientgateway_base": clientgateway,
            "filecache_base": filecache,
        },
        "microsoft": {
            "client_id": client_id,
            "tenant_id": tenant_id,
        },
        "transfer": {
            "concurrency": int(concurrency),
            "chunk_size_mb": int(chunk_size_mb),
            "retry_attempts": int(tr.get("retry_attempts", 3)),
            "retry_delay_s": float(tr.get("retry_delay_s", 5)),
        },
        "state": {
            "db_path": st.get("db_path", "~/.rushport/state.db"),
        },
    }

    with open(CONFIG_PATH, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    console.print("[green]Config saved to config.yaml[/]")
    return load_config()


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
