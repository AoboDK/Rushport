"""
Single-file transfer: Rushfiles → OneDrive.

Streams bytes from the FileCache download endpoint directly into an OneDrive
upload session without buffering the entire file to disk.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Optional

from clients.rushfiles import RushfilesClient, RushfilesAPIError
from clients.graph import GraphClient, GraphAPIError
from models.onedrive import DriveItem
from sync.state import StateDB

# ---------------------------------------------------------------------------
# Filename sanitizer
# ---------------------------------------------------------------------------

# Characters OneDrive forbids in file/folder names
_FORBIDDEN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Names reserved by Windows (OneDrive inherits these restrictions)
_RESERVED_NAMES = re.compile(
    r'^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\.|$)', re.IGNORECASE
)


def sanitize_name(name: str) -> str:
    """
    Replace characters forbidden by OneDrive/Windows with '_'.
    Handles reserved names, trailing dots/spaces.
    """
    clean = _FORBIDDEN_CHARS.sub('_', name)
    clean = clean.rstrip('. ')  # trailing dots and spaces are forbidden
    if not clean:
        clean = '_'
    if _RESERVED_NAMES.match(clean):
        clean = '_' + clean
    return clean


def sanitize_path(relative_path: str) -> str:
    """Sanitize every path segment individually."""
    parts = relative_path.replace('\\', '/').split('/')
    return '/'.join(sanitize_name(p) for p in parts if p)


@dataclass
class TransferResult:
    virtual_file_id: str
    relative_path: str
    success: bool
    onedrive_id: Optional[str] = None
    error: Optional[str] = None
    bytes_transferred: int = 0


class FileTransfer:
    """
    Transfers a single file from Rushfiles to OneDrive and updates state.

    This class is intentionally thin — orchestration (concurrency, retry
    scheduling) lives in engine.py.
    """

    def __init__(
        self,
        rf_client: RushfilesClient,
        graph_client: GraphClient,
        state_db: StateDB,
        onedrive_base_path: str = "Rushfiles Migration",
        retry_attempts: int = 3,
        retry_delay_s: float = 5.0,
    ):
        self._rf = rf_client
        self._graph = graph_client
        self._db = state_db
        self._base_path = onedrive_base_path.strip("/")
        self._retry_attempts = retry_attempts
        self._retry_delay_s = retry_delay_s

    async def transfer(
        self,
        share_id: str,
        virtual_file_id: str,
        version_id: str,
        relative_path: str,
        size_bytes: int,
        source_modified_at: Optional[str] = None,
        source_created_at: Optional[str] = None,
    ) -> TransferResult:
        """
        Transfer one file. Marks state as in_progress → done/failed.
        Retries on transient errors up to retry_attempts times.
        """
        await self._db.set_in_progress(share_id, virtual_file_id)

        last_error: Optional[str] = None

        for attempt in range(self._retry_attempts):
            try:
                drive_item = await self._do_transfer(
                    share_id, virtual_file_id, version_id, relative_path, size_bytes,
                    source_modified_at, source_created_at,
                )
                await self._db.set_done(share_id, virtual_file_id, drive_item.id)
                return TransferResult(
                    virtual_file_id=virtual_file_id,
                    relative_path=relative_path,
                    success=True,
                    onedrive_id=drive_item.id,
                    bytes_transferred=size_bytes,
                )
            except (RushfilesAPIError, GraphAPIError, Exception) as exc:
                last_error = str(exc)
                if attempt < self._retry_attempts - 1:
                    await asyncio.sleep(self._retry_delay_s * (attempt + 1))

        await self._db.set_failed(share_id, virtual_file_id, last_error or "Unknown error")
        return TransferResult(
            virtual_file_id=virtual_file_id,
            relative_path=relative_path,
            success=False,
            error=last_error,
        )

    async def _do_transfer(
        self,
        share_id: str,
        virtual_file_id: str,
        version_id: str,
        relative_path: str,
        size_bytes: int,
        source_modified_at: Optional[str] = None,
        source_created_at: Optional[str] = None,
    ) -> DriveItem:
        """
        Core transfer: stream from Rushfiles → upload to OneDrive.
        Returns the OneDrive DriveItem. Raises GraphAPIError on size mismatch.
        """
        # Sanitize path so OneDrive-forbidden characters don't cause failures
        safe_path = sanitize_path(relative_path)

        # Ensure the parent folder exists in OneDrive
        onedrive_path = f"{self._base_path}/{safe_path}" if self._base_path else safe_path
        parent_folder = "/".join(onedrive_path.split("/")[:-1])
        if parent_folder:
            await self._graph.ensure_folder(parent_folder)

        file_name = relative_path.split("/")[-1]

        # Open the FileCache download stream.
        # last_modified is captured from the HTTP response Last-Modified header
        # before any bytes are consumed — no extra round-trip needed.
        async with await self._rf.open_file_download(share_id, version_id, file_name) as dl:
            modified_at = dl.last_modified or source_modified_at
            drive_item = await self._graph.upload_file(
                onedrive_path=onedrive_path,
                data_iterator=dl,
                total_size=size_bytes,
                modified_at=modified_at,
                created_at=source_created_at,
            )

        # Verify OneDrive received the full file — a truncated upload would
        # otherwise be silently marked done.
        if drive_item.size is not None and drive_item.size != size_bytes:
            raise GraphAPIError(
                0,
                f"Size mismatch after upload: expected {size_bytes} bytes, "
                f"OneDrive reports {drive_item.size} bytes",
            )

        return drive_item
