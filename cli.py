"""
rushport — Rushfiles cloud-to-cloud migration tool

Commands:
  auth rushfiles   Authenticate with Rushfiles (email + password, tokens cached in ~/.rushport/)
  auth onedrive    Authenticate with OneDrive — personal OR business (device code flow)
  list-shares      List all Rushfiles shares accessible to your account
  run              Transfer a Rushfiles share to a destination cloud (OneDrive today)
  resume           Resume a previously interrupted transfer
  status           Show transfer progress for a share
"""

from __future__ import annotations

import asyncio
import getpass
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from config import load_config, ConfigError
from auth.rushfiles import RushfilesAuth, RushfilesAuthError
from auth.microsoft import MicrosoftAuth, MicrosoftAuthError
from clients.rushfiles import RushfilesClient
from destinations.onedrive import create_onedrive_client
from sync.engine import SyncEngine, _human_bytes
from sync.state import StateDB
from sync.transfer import sanitize_name

app = typer.Typer(
    name="rushport",
    help="Rushfiles cloud-to-cloud migration — move files between Rushfiles and other clouds.",
    no_args_is_help=True,
)
auth_app = typer.Typer(help="Authentication commands.", no_args_is_help=True)
app.add_typer(auth_app, name="auth")

console = Console()


# ------------------------------------------------------------------
# Auth commands
# ------------------------------------------------------------------

@auth_app.command("rushfiles")
def auth_rushfiles(
    email: Optional[str] = typer.Option(None, "--email", "-e", help="Rushfiles login email"),
    password: Optional[str] = typer.Option(None, "--password", "-p", help="Password (prompted if omitted)"),
):
    """Authenticate with your Rushfiles account (email + password)."""
    cfg = _load_config_or_exit()

    # Resolve credentials: CLI flag > config file > interactive prompt
    resolved_email = email or cfg.rf_email or typer.prompt("Rushfiles email")
    resolved_password = password or cfg.rf_password or getpass.getpass("Rushfiles password: ")

    async def _run():
        auth = RushfilesAuth(clientgateway_base=cfg.rf_clientgateway_base)
        try:
            console.print(f"Connecting to [cyan]{cfg.rf_clientgateway_base}[/]...")
            await auth.login(resolved_email, resolved_password)
            console.print("[green]Authentication successful.[/]")
            console.print(f"Token cached at [dim]{auth._cache_path}[/]")
        except RushfilesAuthError as e:
            console.print(f"[red]Authentication failed:[/] {e}")
            raise typer.Exit(1)

    asyncio.run(_run())


@auth_app.command("onedrive")
def auth_onedrive():
    """
    Authenticate with Microsoft OneDrive.

    Works for both personal OneDrive (@outlook.com, @hotmail.com, @live.com)
    and OneDrive for Business / Microsoft 365 work or school accounts.
    A browser device code will be shown — visit the URL and enter the code.
    """
    cfg = _load_config_or_exit()
    ms_auth = MicrosoftAuth(client_id=cfg.ms_client_id, tenant_id=cfg.ms_tenant_id)

    console.print("\n[bold]Microsoft OneDrive authentication[/]")
    console.print("Works with [cyan]personal OneDrive[/] and [cyan]OneDrive for Business[/].\n")

    try:
        ms_auth.ensure_authenticated(console_print=console.print)
        console.print("\n[green]OneDrive authentication successful.[/]")
    except MicrosoftAuthError as e:
        console.print(f"[red]Authentication failed:[/] {e}")
        raise typer.Exit(1)


# ------------------------------------------------------------------
# list-shares
# ------------------------------------------------------------------

@app.command("list-shares")
def list_shares():
    """List all Rushfiles shares accessible to your account."""

    async def _run():
        cfg = _load_config_or_exit()
        rf_auth = RushfilesAuth(clientgateway_base=cfg.rf_clientgateway_base)
        if not await rf_auth.load_cached():
            console.print("[red]Not authenticated. Run `rushport auth rushfiles` first.[/]")
            raise typer.Exit(1)

        async with RushfilesClient(
            rf_auth,
            clientgateway_base=cfg.rf_clientgateway_base,
            filecache_base=cfg.rf_filecache_base,
        ) as rf:
            profile = await rf.get_full_profile()

            if not profile.shares:
                console.print("[yellow]No shares found for your account.[/]")
                console.print(
                    "[dim]Tip: some accounts return empty shares from the profile endpoint.\n"
                    "If you know your share ID, run it directly:\n"
                    "  rushport run <share-id>[/]"
                )
                return

            # Build a company name lookup
            company_names = {c.id: c.name for c in profile.companies}

            table = Table(title="Rushfiles Shares", show_lines=True)
            table.add_column("Company", style="cyan")
            table.add_column("Share Name", style="white")
            table.add_column("Share ID", style="dim")

            for share in profile.shares:
                if not share.is_deleted:
                    company_name = company_names.get(share.company_id, share.company_id)
                    table.add_row(company_name, share.name, share.id)

            console.print(table)

    asyncio.run(_run())


# ------------------------------------------------------------------
# list-files
# ------------------------------------------------------------------

@app.command("list-files")
def list_files(
    share_id: str = typer.Argument(..., help="Rushfiles share ID"),
    parent_id: Optional[str] = typer.Option(None, "--parent", "-p", help="Parent virtual file ID (root if omitted)"),
    raw: bool = typer.Option(False, "--raw", help="Dump raw JSON response instead of table"),
):
    """List files and folders in a Rushfiles share (or sub-folder). Useful for verifying the VirtualFile model."""

    async def _run():
        cfg = _load_config_or_exit()
        rf_auth = RushfilesAuth(clientgateway_base=cfg.rf_clientgateway_base)
        if not await rf_auth.load_cached():
            console.print("[red]Not authenticated. Run `rushport auth rushfiles` first.[/]")
            raise typer.Exit(1)

        async with RushfilesClient(
            rf_auth,
            clientgateway_base=cfg.rf_clientgateway_base,
            filecache_base=cfg.rf_filecache_base,
        ) as rf:
            if raw:
                import json
                effective_parent = parent_id or share_id
                data = await rf._cg_get(
                    f"/api/shares/{share_id}/virtualfiles/{effective_parent}/children",
                )
                console.print(json.dumps(data, indent=2, default=str))
                return

            children = await rf.list_children(share_id, parent_id)
            if not children:
                console.print("No files found.")
                return

            table = Table(
                title=f"Share {share_id}" + (f" / {parent_id}" if parent_id else " (root)"),
                show_lines=False,
            )
            table.add_column("Type", width=4)
            table.add_column("Name", style="white")
            table.add_column("Size", justify="right", style="dim")
            table.add_column("Version ID (TransmitId)", style="dim")
            table.add_column("Internal ID", style="dim")

            from sync.engine import _human_bytes
            for vf in children:
                kind = "[blue]DIR[/]" if vf.is_directory else "FILE"
                size = _human_bytes(vf.size_bytes) if vf.is_file else "-"
                table.add_row(kind, vf.public_name, size, vf.version_id or "-", vf.internal_name)

            console.print(table)

    asyncio.run(_run())


# ------------------------------------------------------------------
# run
# ------------------------------------------------------------------

@app.command("run")
def run(
    share_id: Optional[str] = typer.Argument(None, help="Rushfiles share ID to transfer. Omit to transfer all shares."),
    onedrive_path: Optional[str] = typer.Option(
        None,
        "--onedrive-path", "-o",
        help="Destination folder in OneDrive (default: share name). Only used with a single share ID.",
    ),
    concurrency: Optional[int] = typer.Option(None, "--concurrency", "-c"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Scan only, no transfers"),
    reset: bool = typer.Option(False, "--reset", help="Re-transfer all files even if previously done (use after deleting the OneDrive destination)"),
):
    """
    Transfer Rushfiles shares to OneDrive.

    Each share becomes its own folder in OneDrive named after the share.
    Provide a share ID to transfer one share, or omit it to transfer all shares.
    """
    async def _run():
        cfg = _load_config_or_exit()

        rf_auth = RushfilesAuth(clientgateway_base=cfg.rf_clientgateway_base)
        if not await rf_auth.load_cached():
            console.print("[red]Not authenticated with Rushfiles. Run `rushport auth rushfiles` first.[/]")
            raise typer.Exit(1)

        ms_auth, graph_client = create_onedrive_client(
            client_id=cfg.ms_client_id,
            tenant_id=cfg.ms_tenant_id,
            chunk_size_mb=cfg.chunk_size_mb,
        )
        if not ms_auth.is_authenticated():
            console.print("[red]Not authenticated with OneDrive. Run `rushport auth onedrive` first.[/]")
            raise typer.Exit(1)

        eff_concurrency = concurrency or cfg.concurrency

        async with RushfilesClient(
            rf_auth,
            clientgateway_base=cfg.rf_clientgateway_base,
            filecache_base=cfg.rf_filecache_base,
        ) as rf_client:
            # Resolve which shares to transfer
            if share_id:
                # Single share — use explicit --onedrive-path or auto-discover share name
                if onedrive_path:
                    shares_to_run = [(share_id, onedrive_path)]
                else:
                    all_shares = await rf_client.list_shares()
                    match = next((s for s in all_shares if s.id == share_id), None)
                    folder = sanitize_name(match.name) if match else share_id
                    shares_to_run = [(share_id, folder)]
            else:
                # All shares — each gets a folder named after the share
                all_shares = await rf_client.list_shares()
                if not all_shares:
                    console.print("No shares found for your account.")
                    return
                shares_to_run = [(s.id, sanitize_name(s.name)) for s in all_shares]
                console.print(f"Found [bold]{len(shares_to_run)}[/] share(s) to transfer.")

            async with StateDB(cfg.db_path) as state_db:
                if dry_run:
                    console.print("[yellow]Dry run — scanning only, no files will be transferred.[/]")
                    from sync.engine import SyncStats
                    for sid, folder in shares_to_run:
                        stats = SyncStats()
                        async for _path, vf in rf_client.walk(sid):
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
                        concurrency=eff_concurrency,
                        retry_attempts=cfg.retry_attempts,
                        retry_delay_s=cfg.retry_delay_s,
                    )
                    for sid, folder in shares_to_run:
                        if reset:
                            n = await state_db.reset_share(sid)
                            if n:
                                console.print(f"[yellow]Reset {n} file(s) to pending for re-transfer.[/]")
                        console.print(f"\n[bold]Share:[/] {folder}")
                        await engine.run(sid, folder, resume=False)

    asyncio.run(_run())


@app.command("resume")
def resume(
    share_id: str = typer.Argument(..., help="Rushfiles share ID to resume"),
    onedrive_path: Optional[str] = typer.Option(None, "--onedrive-path", "-o", help="OneDrive folder (default: share name)"),
    concurrency: Optional[int] = typer.Option(None, "--concurrency", "-c"),
):
    """Resume a previously interrupted transfer (skips scan phase)."""
    async def _run():
        cfg = _load_config_or_exit()

        rf_auth = RushfilesAuth(clientgateway_base=cfg.rf_clientgateway_base)
        if not await rf_auth.load_cached():
            console.print("[red]Not authenticated with Rushfiles. Run `rushport auth rushfiles` first.[/]")
            raise typer.Exit(1)

        ms_auth, graph_client = create_onedrive_client(
            client_id=cfg.ms_client_id,
            tenant_id=cfg.ms_tenant_id,
            chunk_size_mb=cfg.chunk_size_mb,
        )
        if not ms_auth.is_authenticated():
            console.print("[red]Not authenticated with OneDrive. Run `rushport auth onedrive` first.[/]")
            raise typer.Exit(1)

        eff_concurrency = concurrency or cfg.concurrency

        async with RushfilesClient(
            rf_auth,
            clientgateway_base=cfg.rf_clientgateway_base,
            filecache_base=cfg.rf_filecache_base,
        ) as rf_client:
            folder = onedrive_path
            if not folder:
                all_shares = await rf_client.list_shares()
                match = next((s for s in all_shares if s.id == share_id), None)
                folder = sanitize_name(match.name) if match else share_id

            async with StateDB(cfg.db_path) as state_db:
                async with graph_client:
                    engine = SyncEngine(
                        rf_client=rf_client,
                        graph_client=graph_client,
                        state_db=state_db,
                        concurrency=eff_concurrency,
                        retry_attempts=cfg.retry_attempts,
                        retry_delay_s=cfg.retry_delay_s,
                    )
                    await engine.run(share_id, folder, resume=True)

    asyncio.run(_run())


# ------------------------------------------------------------------
# status
# ------------------------------------------------------------------

@app.command("status")
def status(
    share_id: Optional[str] = typer.Argument(None, help="Filter by share ID"),
    failed: bool = typer.Option(False, "--failed", help="List failed files with error details"),
):
    """Show transfer progress."""

    async def _run():
        cfg = _load_config_or_exit()
        async with StateDB(cfg.db_path) as db:
            if failed:
                rows = await db.get_failed(share_id)
                if not rows:
                    console.print("[green]No failed files.[/]")
                    return
                table = Table(title="Failed Files", show_lines=True)
                table.add_column("Share ID", style="dim")
                table.add_column("Path", style="white")
                table.add_column("Attempts", justify="right", style="dim")
                table.add_column("Error", style="red")
                for row in rows:
                    table.add_row(
                        row["share_id"],
                        row["relative_path"],
                        str(row["attempts"]),
                        row["error"] or "",
                    )
                console.print(table)
                return

            stats = await db.get_stats(share_id)

        if not stats:
            console.print("No transfer data found. Run `rushport run <share-id>` to start.")
            return

        table = Table(title="Transfer Status")
        table.add_column("Status", style="cyan")
        table.add_column("Files", justify="right")
        table.add_column("Size", justify="right")

        status_order = ["done", "pending", "in_progress", "failed", "skipped"]
        colors = {
            "done": "green",
            "pending": "white",
            "in_progress": "yellow",
            "failed": "red",
            "skipped": "dim",
        }

        total_files = sum(v["count"] for v in stats.values())
        total_bytes = sum(v["bytes"] for v in stats.values())

        for s in status_order:
            if s in stats:
                color = colors.get(s, "white")
                table.add_row(
                    f"[{color}]{s}[/]",
                    str(stats[s]["count"]),
                    _human_bytes(stats[s]["bytes"]),
                )

        table.add_section()
        table.add_row("[bold]Total[/]", str(total_files), _human_bytes(total_bytes))
        console.print(table)
        if "failed" in stats and stats["failed"]["count"]:
            console.print(f"[dim]Run `rushport status --failed` to see error details.[/]")

    asyncio.run(_run())


@app.command("gui")
def gui():
    """Launch the guided desktop UI."""
    try:
        from gui import launch_gui
    except Exception as e:
        console.print(f"[red]Could not launch GUI:[/] {e}")
        raise typer.Exit(1)

    launch_gui()


def _load_config_or_exit():
    try:
        return load_config()
    except ConfigError as e:
        console.print(f"[red]Configuration error:[/] {e}")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
