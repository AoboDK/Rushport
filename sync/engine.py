"""
Sync engine — orchestrates the full transfer pipeline.

Steps:
  1. Walk the Rushfiles share tree and populate the state DB (scan phase)
  2. Pull pending files from DB and transfer them concurrently (transfer phase)

The scan is always run first so that the DB has a complete picture before
any transfers begin. This means `status` shows realistic progress numbers.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from clients.rushfiles import RushfilesClient
from clients.graph import GraphClient
from sync.state import StateDB, FileStatus
from sync.transfer import FileTransfer, TransferResult, sanitize_path

console = Console()


@dataclass
class SyncStats:
    scanned: int = 0
    transferred: int = 0
    skipped: int = 0
    failed: int = 0
    total_bytes: int = 0
    transferred_bytes: int = 0
    errors: list[str] = field(default_factory=list)


class SyncEngine:
    """
    Coordinates a full Rushfiles share → OneDrive sync.

    Usage:
        engine = SyncEngine(rf_client, graph_client, state_db, config)
        stats = await engine.run(share_id, onedrive_base_path)
    """

    def __init__(
        self,
        rf_client: RushfilesClient,
        graph_client: GraphClient,
        state_db: StateDB,
        concurrency: int = 4,
        retry_attempts: int = 3,
        retry_delay_s: float = 5.0,
    ):
        self._rf = rf_client
        self._graph = graph_client
        self._db = state_db
        self._concurrency = concurrency
        self._retry_attempts = retry_attempts
        self._retry_delay_s = retry_delay_s

    async def run(
        self,
        share_id: str,
        onedrive_base_path: str = "Rushfiles Migration",
        resume: bool = False,
    ) -> SyncStats:
        """
        Run a full sync of share_id into onedrive_base_path.
        If resume=True, skips the scan phase and uses existing DB state.
        """
        stats = SyncStats()

        # Recover any interrupted in_progress records from a previous run
        recovered = await self._db.reset_in_progress()
        if recovered:
            console.print(f"[yellow]Recovered {recovered} interrupted transfer(s) to pending.[/]")

        # Phase 1: Scan
        folder_dates: dict[str, tuple] = {}
        if not resume:
            console.print(f"\n[bold]Scanning Rushfiles share:[/] {share_id}")
            folder_dates = await self._scan_phase(share_id, stats)
            console.print(
                f"[green]Scan complete.[/] {stats.scanned} files "
                f"({_human_bytes(stats.total_bytes)})"
            )
        else:
            console.print("[cyan]Resuming — skipping scan phase.[/]")

        # Phase 2: Transfer
        console.print(f"\n[bold]Transferring to OneDrive:[/] /{onedrive_base_path}")
        await self._transfer_phase(share_id, onedrive_base_path, stats)

        # Phase 3: Patch folder dates (after all files are in place)
        if folder_dates:
            await self._apply_folder_dates_phase(onedrive_base_path, folder_dates)

        return stats

    # ------------------------------------------------------------------
    # Phase 1: Scan the share tree into the state DB
    # ------------------------------------------------------------------

    async def _scan_phase(self, share_id: str, stats: SyncStats) -> dict[str, tuple]:
        """
        Walk the share tree, upsert every file into the state DB, and collect
        folder dates for later patching.  Returns a dict of
        {relative_path: (modified_at_iso, created_at_iso)} for all directories.
        """
        folder_dates: dict[str, tuple] = {}

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Scanning...", total=None)

            async for relative_path, vf in self._rf.walk(share_id):
                if vf.is_file:
                    await self._db.upsert_file(
                        share_id=share_id,
                        virtual_file_id=vf.internal_name,
                        relative_path=relative_path,
                        size_bytes=vf.size_bytes,
                        version_id=vf.version_id,
                        source_modified_at=vf.last_write_time.isoformat() if vf.last_write_time else None,
                        source_created_at=vf.create_time.isoformat() if vf.create_time else None,
                    )
                    stats.scanned += 1
                    stats.total_bytes += vf.size_bytes
                    progress.update(
                        task,
                        description=f"Scanning... {stats.scanned} files found",
                    )
                else:
                    # Collect folder dates — applied after transfer so OneDrive
                    # doesn't reset them when files are added to the folder.
                    m = vf.last_write_time.isoformat() if vf.last_write_time else None
                    c = vf.create_time.isoformat() if vf.create_time else None
                    if m or c:
                        folder_dates[relative_path] = (m, c)

        return folder_dates

    # ------------------------------------------------------------------
    # Phase 2: Concurrent file transfers
    # ------------------------------------------------------------------

    async def _transfer_phase(
        self,
        share_id: str,
        onedrive_base_path: str,
        stats: SyncStats,
    ) -> None:
        pending = await self._db.get_pending(share_id)
        total_files = len(pending)

        if total_files == 0:
            console.print("[green]Nothing to transfer — all files already done.[/]")
            return

        transferrer = FileTransfer(
            rf_client=self._rf,
            graph_client=self._graph,
            state_db=self._db,
            onedrive_base_path=onedrive_base_path,
            retry_attempts=self._retry_attempts,
            retry_delay_s=self._retry_delay_s,
        )

        semaphore = asyncio.Semaphore(self._concurrency)
        total_bytes = sum(row["size_bytes"] for row in pending)
        files_done = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task(
                f"Transferring [0/{total_files} files]",
                total=max(total_bytes, 1),
            )

            async def bounded_transfer(row) -> TransferResult:
                nonlocal files_done
                async with semaphore:
                    result = await transferrer.transfer(
                        share_id=row["share_id"],
                        virtual_file_id=row["virtual_file_id"],
                        version_id=row["version_id"],
                        relative_path=row["relative_path"],
                        size_bytes=row["size_bytes"],
                        source_modified_at=row["source_modified_at"],
                        source_created_at=row["source_created_at"],
                    )
                    files_done += 1
                    progress.update(
                        task_id,
                        advance=result.bytes_transferred,
                        description=f"Transferring [{files_done}/{total_files} files]",
                    )
                    return result

            tasks = [bounded_transfer(row) for row in pending]
            results: list[TransferResult] = await asyncio.gather(*tasks, return_exceptions=False)

        # Tally results
        for result in results:
            if result.success:
                stats.transferred += 1
                stats.transferred_bytes += result.bytes_transferred
            else:
                stats.failed += 1
                if result.error:
                    stats.errors.append(f"{result.relative_path}: {result.error}")

        # Print summary
        console.print(f"\n[bold green]Transfer complete.[/]")
        console.print(f"  Transferred: {stats.transferred} files ({_human_bytes(stats.transferred_bytes)})")
        if stats.failed:
            console.print(f"  [red]Failed:      {stats.failed} files[/]")
            for err in stats.errors[:10]:
                console.print(f"    [dim]{err}[/]")
            if len(stats.errors) > 10:
                console.print(f"    [dim]... and {len(stats.errors) - 10} more. Run `rushport status` for full list.[/]")

    # ------------------------------------------------------------------
    # Phase 3: Apply original dates to OneDrive folders
    # ------------------------------------------------------------------

    async def _apply_folder_dates_phase(
        self,
        onedrive_base_path: str,
        folder_dates: dict[str, tuple],
    ) -> None:
        """
        PATCH fileSystemInfo on every OneDrive folder to restore its Rushfiles date.
        Must run after all file transfers so OneDrive doesn't reset folder dates
        when new files are added.
        """
        console.print(f"\n[bold]Applying original dates to {len(folder_dates)} folder(s)...[/]")
        errors = 0
        for relative_path, (modified_at, created_at) in folder_dates.items():
            safe_path = sanitize_path(relative_path)
            onedrive_path = f"{onedrive_base_path}/{safe_path}" if onedrive_base_path else safe_path
            try:
                # ensure_folder creates it if it doesn't exist yet (empty folders
                # never get created during the file transfer phase)
                await self._graph.ensure_folder(onedrive_path)
                await self._graph.patch_item_dates(onedrive_path, modified_at, created_at)
            except Exception as exc:
                errors += 1
                console.print(f"  [yellow]Warning: could not set date on {onedrive_path}: {exc}[/]")
        if errors:
            console.print(f"  [yellow]{errors} folder(s) could not be dated.[/]")
        else:
            console.print(f"[green]Folder dates applied.[/]")


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"
