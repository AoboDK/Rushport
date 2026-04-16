"""
SQLite-backed transfer state tracker.

Tracks every file discovered in a Rushfiles share so that:
  - Transfers can be resumed after interruption
  - Already-transferred files are skipped
  - Failed files can be retried or reported on

Schema:
  files(
    id               -- auto increment
    share_id         -- Rushfiles share ID
    virtual_file_id  -- Rushfiles InternalName (stable ID)
    version_id       -- Rushfiles TransmitId (the content version to download)
    relative_path    -- path within the share, e.g. "docs/report.pdf"
    size_bytes       -- file size
    status           -- pending | in_progress | done | failed | skipped
    onedrive_id      -- OneDrive DriveItem ID (set on success)
    error            -- last error message (if failed)
    attempts         -- retry count
    created_at       -- Unix timestamp
    updated_at       -- Unix timestamp
  )
"""

from __future__ import annotations

import asyncio
import time
from enum import Enum
from pathlib import Path
from typing import Optional

import aiosqlite

_DEFAULT_DB_PATH = Path("~/.rushport/state.db").expanduser()

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS files (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    share_id            TEXT NOT NULL,
    virtual_file_id     TEXT NOT NULL,
    version_id          TEXT,
    relative_path       TEXT NOT NULL,
    size_bytes          INTEGER DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'pending',
    onedrive_id         TEXT,
    error               TEXT,
    attempts            INTEGER DEFAULT 0,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL,
    source_modified_at  TEXT,
    source_created_at   TEXT,
    UNIQUE(share_id, virtual_file_id)
);
CREATE INDEX IF NOT EXISTS idx_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_share   ON files(share_id);
"""

# Columns added after the initial schema — applied via ALTER TABLE so existing DBs migrate cleanly.
_MIGRATIONS = [
    "ALTER TABLE files ADD COLUMN source_modified_at TEXT",
    "ALTER TABLE files ADD COLUMN source_created_at  TEXT",
]


class FileStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class FileRecord:
    __slots__ = (
        "id", "share_id", "virtual_file_id", "version_id", "relative_path",
        "size_bytes", "status", "onedrive_id", "error", "attempts",
        "created_at", "updated_at",
    )

    def __init__(self, row: aiosqlite.Row):
        (
            self.id, self.share_id, self.virtual_file_id, self.version_id,
            self.relative_path, self.size_bytes, self.status, self.onedrive_id,
            self.error, self.attempts, self.created_at, self.updated_at,
        ) = row


class StateDB:
    """
    Async SQLite state store.

    Usage:
        async with StateDB() as db:
            await db.upsert_file(share_id, vf)
            pending = await db.get_pending(share_id)
    """

    def __init__(self, db_path: Path = _DEFAULT_DB_PATH):
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def __aenter__(self) -> "StateDB":
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_CREATE_TABLE)
        await self._conn.commit()
        await self._apply_migrations()
        return self

    async def _apply_migrations(self) -> None:
        """Run ALTER TABLE statements that add columns added after the initial schema.
        SQLite raises OperationalError if the column already exists — that's fine, skip it."""
        for sql in _MIGRATIONS:
            try:
                await self._conn.execute(sql)
                await self._conn.commit()
            except Exception:
                pass  # Column already exists

    async def __aexit__(self, *_) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def upsert_file(
        self,
        share_id: str,
        virtual_file_id: str,
        relative_path: str,
        size_bytes: int = 0,
        version_id: Optional[str] = None,
        source_modified_at: Optional[str] = None,
        source_created_at: Optional[str] = None,
    ) -> None:
        """
        Insert a file record if it doesn't exist.
        If it exists but was previously done, leave it (skip re-transfer).
        If it exists and failed, reset to pending for retry.
        source_modified_at / source_created_at are ISO-8601 strings from Rushfiles.
        """
        now = time.time()
        await self._conn.execute(
            """
            INSERT INTO files
                (share_id, virtual_file_id, version_id, relative_path, size_bytes,
                 status, created_at, updated_at, source_modified_at, source_created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            ON CONFLICT(share_id, virtual_file_id) DO UPDATE SET
                version_id         = excluded.version_id,
                relative_path      = excluded.relative_path,
                size_bytes         = excluded.size_bytes,
                source_modified_at = excluded.source_modified_at,
                source_created_at  = excluded.source_created_at,
                updated_at         = excluded.updated_at
            WHERE files.status NOT IN ('done', 'skipped')
            """,
            (share_id, virtual_file_id, version_id, relative_path, size_bytes,
             now, now, source_modified_at, source_created_at),
        )
        await self._conn.commit()

    async def set_in_progress(self, share_id: str, virtual_file_id: str) -> None:
        await self._update_status(share_id, virtual_file_id, FileStatus.IN_PROGRESS)

    async def set_done(
        self,
        share_id: str,
        virtual_file_id: str,
        onedrive_id: str,
    ) -> None:
        now = time.time()
        await self._conn.execute(
            """
            UPDATE files SET status='done', onedrive_id=?, error=NULL, updated_at=?
            WHERE share_id=? AND virtual_file_id=?
            """,
            (onedrive_id, now, share_id, virtual_file_id),
        )
        await self._conn.commit()

    async def set_failed(
        self,
        share_id: str,
        virtual_file_id: str,
        error: str,
    ) -> None:
        now = time.time()
        await self._conn.execute(
            """
            UPDATE files
            SET status='failed', error=?, attempts=attempts+1, updated_at=?
            WHERE share_id=? AND virtual_file_id=?
            """,
            (error, now, share_id, virtual_file_id),
        )
        await self._conn.commit()

    async def reset_in_progress(self) -> int:
        """Reset any in_progress records back to pending (crash recovery)."""
        now = time.time()
        cursor = await self._conn.execute(
            "UPDATE files SET status='pending', updated_at=? WHERE status='in_progress'",
            (now,),
        )
        await self._conn.commit()
        return cursor.rowcount

    async def reset_share(self, share_id: str) -> int:
        """
        Reset all records for a share back to pending so they will be re-transferred.
        Used when the destination was deleted and you want to start fresh.
        """
        now = time.time()
        cursor = await self._conn.execute(
            "UPDATE files SET status='pending', onedrive_id=NULL, error=NULL, attempts=0, updated_at=? WHERE share_id=?",
            (now, share_id),
        )
        await self._conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def get_pending(
        self,
        share_id: str,
        limit: int = 0,
    ) -> list[aiosqlite.Row]:
        """Fetch pending (and failed-retryable) files for a share."""
        sql = """
            SELECT * FROM files
            WHERE share_id=? AND status IN ('pending', 'failed')
            ORDER BY id
        """
        params = [share_id]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        cursor = await self._conn.execute(sql, params)
        return await cursor.fetchall()

    async def get_failed(self, share_id: Optional[str] = None) -> list:
        """Fetch all failed files with path and error message."""
        if share_id:
            cursor = await self._conn.execute(
                "SELECT share_id, relative_path, error, attempts, size_bytes "
                "FROM files WHERE share_id=? AND status='failed' ORDER BY relative_path",
                (share_id,),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT share_id, relative_path, error, attempts, size_bytes "
                "FROM files WHERE status='failed' ORDER BY share_id, relative_path"
            )
        return await cursor.fetchall()

    async def get_stats(self, share_id: Optional[str] = None) -> dict:
        """Return counts per status."""
        if share_id:
            cursor = await self._conn.execute(
                "SELECT status, COUNT(*) as n, SUM(size_bytes) as bytes "
                "FROM files WHERE share_id=? GROUP BY status",
                (share_id,),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT status, COUNT(*) as n, SUM(size_bytes) as bytes "
                "FROM files GROUP BY status"
            )
        rows = await cursor.fetchall()
        return {row["status"]: {"count": row["n"], "bytes": row["bytes"] or 0} for row in rows}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _update_status(
        self,
        share_id: str,
        virtual_file_id: str,
        status: FileStatus,
    ) -> None:
        now = time.time()
        await self._conn.execute(
            "UPDATE files SET status=?, updated_at=? WHERE share_id=? AND virtual_file_id=?",
            (status.value, now, share_id, virtual_file_id),
        )
        await self._conn.commit()
