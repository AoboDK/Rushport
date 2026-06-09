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
    auth = RushfilesAuth(clientgateway_base=cfg.rf_clientgateway_base)

    if await auth.load_cached():
        _print_skip("Already authenticated with RushFiles")
        return auth

    _print_step(2, "RushFiles Authentication")

    while True:
        email = questionary.text(
            "RushFiles email:", default=cfg.rf_email or ""
        ).ask()
        if email is None:
            _handle_interrupt()

        password = questionary.password("RushFiles password:").ask()
        if password is None:
            _handle_interrupt()

        try:
            await auth.login(email, password)
            console.print("[green]RushFiles authentication successful.[/]")
            return auth
        except RushfilesAuthError as e:
            _print_error(f"Authentication failed: {e}", "Please try again.")


def step_ms_auth(cfg: Config) -> MicrosoftAuth:
    ms_auth = MicrosoftAuth(client_id=cfg.ms_client_id, tenant_id=cfg.ms_tenant_id)

    if ms_auth.is_authenticated():
        _print_skip("Already authenticated with OneDrive")
        return ms_auth

    _print_step(3, "OneDrive Authentication")
    console.print("Works with [cyan]personal OneDrive[/] and [cyan]OneDrive for Business[/].\n")

    try:
        ms_auth.ensure_authenticated(console_print=console.print)
        console.print("[green]OneDrive authentication successful.[/]")
        return ms_auth
    except MicrosoftAuthError as e:
        _print_error(
            f"OneDrive authentication failed: {e}",
            "Run `rushport-cli auth onedrive` to retry manually.",
        )
        raise SystemExit(1)


async def step_pick_share(cfg: Config, rf_auth: RushfilesAuth) -> list[tuple[str, str]]:
    _print_step(4, "Choose Share")

    async with RushfilesClient(
        rf_auth,
        clientgateway_base=cfg.rf_clientgateway_base,
        filecache_base=cfg.rf_filecache_base,
    ) as rf:
        shares = await rf.list_shares()

    if not shares:
        console.print(
            "[yellow]No shares found automatically.[/]\n"
            "[dim]Tip: some accounts return empty shares from the profile endpoint.[/]"
        )
        share_id = questionary.text("Enter share ID manually:").ask()
        if share_id is None:
            _handle_interrupt()
        return [(share_id, share_id)]

    choices = [
        questionary.Choice(
            title=f"{s.name}  ({s.id})",
            value=(s.id, sanitize_name(s.name)),
        )
        for s in shares
        if not s.is_deleted
    ]
    choices.append(questionary.Choice(title="Transfer all shares", value="ALL"))

    selection = questionary.select(
        "Choose a share to transfer:", choices=choices
    ).ask()
    if selection is None:
        _handle_interrupt()

    if selection == "ALL":
        return [(s.id, sanitize_name(s.name)) for s in shares if not s.is_deleted]

    return [selection]


def step_transfer_options(cfg: Config, shares: list[tuple[str, str]]) -> dict:
    _print_step(5, "Transfer Options")

    transfer_all = len(shares) > 1

    if not transfer_all:
        share_id, default_folder = shares[0]
        dest = questionary.text(
            "Destination folder in OneDrive:", default=default_folder
        ).ask()
        if dest is None:
            _handle_interrupt()
        shares = [(share_id, dest)]

    dry_run = questionary.confirm(
        "Dry run (scan only, no uploads)?", default=False
    ).ask()
    if dry_run is None:
        _handle_interrupt()

    concurrency_str = questionary.text(
        "Concurrency:", default=str(cfg.concurrency)
    ).ask()
    if concurrency_str is None:
        _handle_interrupt()

    console.print("\n[bold]Summary:[/]")
    if transfer_all:
        console.print(f"  Shares:       {len(shares)} (all)")
    else:
        console.print(f"  Share:        {shares[0][1]}")
        console.print(f"  Destination:  /[cyan]{shares[0][1]}[/] (OneDrive)")
    console.print(f"  Dry run:      {'Yes' if dry_run else 'No'}")
    console.print(f"  Concurrency:  {concurrency_str}")

    return {
        "shares": shares,
        "dry_run": dry_run,
        "concurrency": int(concurrency_str),
    }


async def step_run_transfer(cfg: Config, rf_auth: RushfilesAuth, options: dict) -> None:
    _print_step(6, "Transfer")

    _, graph_client = create_onedrive_client(
        client_id=cfg.ms_client_id,
        tenant_id=cfg.ms_tenant_id,
        chunk_size_mb=cfg.chunk_size_mb,
    )

    async with RushfilesClient(
        rf_auth,
        clientgateway_base=cfg.rf_clientgateway_base,
        filecache_base=cfg.rf_filecache_base,
    ) as rf_client:
        async with StateDB(cfg.db_path) as state_db:
            if options["dry_run"]:
                console.print("[yellow]Dry run — scanning only, no files will be transferred.[/]")
                for share_id, folder in options["shares"]:
                    stats = SyncStats()
                    async for _path, vf in rf_client.walk(share_id):
                        if vf.is_file:
                            stats.scanned += 1
                            stats.total_bytes += vf.size_bytes
                    console.print(
                        f"  [cyan]{folder}[/]: [bold]{stats.scanned}[/] files "
                        f"([bold]{_human_bytes(stats.total_bytes)}[/])"
                    )
                return

            async with graph_client:
                engine = SyncEngine(
                    rf_client=rf_client,
                    graph_client=graph_client,
                    state_db=state_db,
                    concurrency=options["concurrency"],
                    retry_attempts=cfg.retry_attempts,
                    retry_delay_s=cfg.retry_delay_s,
                )
                for share_id, folder in options["shares"]:
                    console.print(f"\n[bold]Share:[/] {folder}")
                    await engine.run(share_id, folder, resume=False)


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
