"""
Microsoft Graph API client for OneDrive operations.

Handles:
  - Folder creation (mkdir -p style)
  - Small file upload (< 4 MB, single PUT)
  - Large file upload (>= 4 MB, resumable upload session with chunked PUT)
  - Path resolution and conflict handling
  - Automatic 429 rate-limit back-off (Retry-After header respected)
"""

from __future__ import annotations

import asyncio
from typing import Optional, AsyncIterator

import httpx

from auth.microsoft import MicrosoftAuth
from models.onedrive import DriveItem, UploadSession

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Files smaller than this use simple PUT; larger use upload sessions
_SIMPLE_UPLOAD_MAX = 4 * 1024 * 1024  # 4 MB

# Upload session chunk size — must be a multiple of 320 KiB
_DEFAULT_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB

# How many times to retry after a 429 before giving up
_MAX_RATE_LIMIT_RETRIES = 8


class GraphAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: {message}")


class GraphClient:
    """
    Async Microsoft Graph client scoped to OneDrive operations.

    Usage:
        async with GraphClient(auth) as client:
            await client.ensure_folder("/Rushfiles Migration/ProjectA")
            await client.upload_file("/Rushfiles Migration/doc.pdf", stream, size)
    """

    def __init__(
        self,
        auth: MicrosoftAuth,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
        timeout: float = 120.0,
    ):
        self._auth = auth
        self._chunk_size = chunk_size
        self._timeout = timeout
        self._http: Optional[httpx.AsyncClient] = None
        # Cache folder paths we've already created this session
        self._created_folders: set[str] = set()

    async def __aenter__(self) -> "GraphClient":
        self._http = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *_) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------------
    # Folder management
    # ------------------------------------------------------------------

    async def ensure_folder(self, path: str) -> str:
        """
        Create all folders in path if they don't exist (mkdir -p).
        Returns the normalised folder path.
        """
        path = path.strip("/")
        if path in self._created_folders:
            return path

        parts = path.split("/") if path else []
        current_path = ""

        for part in parts:
            current_path = f"{current_path}/{part}".lstrip("/")
            if current_path not in self._created_folders:
                await self._create_folder_if_missing(current_path, part)
                self._created_folders.add(current_path)

        return path

    async def _create_folder_if_missing(self, full_path: str, folder_name: str) -> None:
        """Create a single folder, ignoring conflict if it already exists."""
        parent_path = "/".join(full_path.split("/")[:-1])

        if parent_path:
            url = f"{_GRAPH_BASE}/me/drive/root:/{parent_path}:/children"
        else:
            url = f"{_GRAPH_BASE}/me/drive/root/children"

        body = {
            "name": folder_name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        }

        headers = self._auth.auth_headers()
        headers["Content-Type"] = "application/json"

        response = await self._request("POST", url, headers=headers, json=body)

        # 409 Conflict = folder already exists, that's fine
        if response.status_code not in (201, 409):
            await self._raise_for_status(response)

    # ------------------------------------------------------------------
    # File upload
    # ------------------------------------------------------------------

    async def upload_file(
        self,
        onedrive_path: str,
        data_iterator: AsyncIterator[bytes],
        total_size: int,
        modified_at: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> DriveItem:
        """
        Upload a file to OneDrive at the given path.

        onedrive_path: full path from drive root, e.g. "Rushfiles Migration/docs/report.pdf"
        data_iterator: async generator yielding bytes chunks
        total_size: total file size in bytes (required for chunked uploads)
        modified_at / created_at: ISO-8601 strings from the source system; set on the
            OneDrive item so the document keeps its original timestamp.

        When dates are provided, always uses an upload session (even for small files)
        because fileSystemInfo embedded in the session body is the only reliable way
        to preserve timestamps — a simple PUT followed by a PATCH is not reliable.
        When no dates are provided and the file is small, uses the faster simple PUT.
        """
        if total_size < _SIMPLE_UPLOAD_MAX and not (modified_at or created_at):
            return await self._simple_upload(onedrive_path, data_iterator, total_size)
        else:
            return await self._chunked_upload(
                onedrive_path, data_iterator, total_size, modified_at, created_at
            )

    async def _simple_upload(
        self,
        path: str,
        data_iterator: AsyncIterator[bytes],
        total_size: int,
    ) -> DriveItem:
        """Single PUT for small files with no timestamp requirements (fast path)."""
        chunks = []
        async for chunk in data_iterator:
            chunks.append(chunk)
        data = b"".join(chunks)

        encoded_path = path.replace("#", "%23").replace("?", "%3F")
        url = f"{_GRAPH_BASE}/me/drive/root:/{encoded_path}:/content"
        headers = self._auth.auth_headers()
        headers["Content-Type"] = "application/octet-stream"

        response = await self._request("PUT", url, headers=headers, content=data)
        await self._raise_for_status(response)
        return DriveItem.model_validate(response.json())

    async def _chunked_upload(
        self,
        path: str,
        data_iterator: AsyncIterator[bytes],
        total_size: int,
        modified_at: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> DriveItem:
        """Resumable upload session for files >= 4 MB (or any file when dates provided)."""
        # 1. Create upload session (dates embedded here, no PATCH needed)
        session = await self._create_upload_session(path, modified_at, created_at)

        # 2. Stream chunks to the session URL
        offset = 0
        buffer = b""

        async for chunk in data_iterator:
            buffer += chunk
            while len(buffer) >= self._chunk_size:
                to_send = buffer[: self._chunk_size]
                buffer = buffer[self._chunk_size :]
                result = await self._put_chunk(
                    session.upload_url, to_send, offset, total_size
                )
                offset += len(to_send)
                if result and "id" in result:
                    return DriveItem.model_validate(result)

        # Send any remaining bytes
        if buffer:
            result = await self._put_chunk(
                session.upload_url, buffer, offset, total_size
            )
            offset += len(buffer)
            if result and "id" in result:
                return DriveItem.model_validate(result)

        raise GraphAPIError(0, "Upload completed but no DriveItem returned.")

    async def _create_upload_session(
        self,
        path: str,
        modified_at: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> UploadSession:
        encoded_path = path.replace("#", "%23").replace("?", "%3F")
        url = f"{_GRAPH_BASE}/me/drive/root:/{encoded_path}:/createUploadSession"
        headers = self._auth.auth_headers()
        headers["Content-Type"] = "application/json"

        item: dict = {"@microsoft.graph.conflictBehavior": "replace"}
        if modified_at or created_at:
            fs_info: dict = {}
            if modified_at:
                fs_info["lastModifiedDateTime"] = modified_at
            if created_at:
                fs_info["createdDateTime"] = created_at
            item["fileSystemInfo"] = fs_info

        body = {"item": item}
        response = await self._request("POST", url, headers=headers, json=body)
        await self._raise_for_status(response)
        return UploadSession.model_validate(response.json())

    async def patch_item_dates(
        self,
        path: str,
        modified_at: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> None:
        """
        PATCH fileSystemInfo on an existing OneDrive item (file or folder) to set
        the source timestamps.  Safe to call on folders after all their contents
        have been uploaded — OneDrive won't reset the date again after this point.
        """
        if not (modified_at or created_at):
            return
        encoded_path = path.replace("#", "%23").replace("?", "%3F")
        url = f"{_GRAPH_BASE}/me/drive/root:/{encoded_path}"
        headers = self._auth.auth_headers()
        headers["Content-Type"] = "application/json"
        fs_info: dict = {}
        if modified_at:
            fs_info["lastModifiedDateTime"] = modified_at
        if created_at:
            fs_info["createdDateTime"] = created_at
        response = await self._request("PATCH", url, headers=headers, json={"fileSystemInfo": fs_info})
        await self._raise_for_status(response)

    async def _put_chunk(
        self,
        upload_url: str,
        data: bytes,
        offset: int,
        total_size: int,
    ) -> Optional[dict]:
        """PUT a single chunk to an upload session URL, retrying on 429."""
        end = offset + len(data) - 1
        headers = {
            "Content-Range": f"bytes {offset}-{end}/{total_size}",
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(data)),
        }
        # Upload session URLs are pre-authenticated — no Authorization header needed.
        # Use a generous timeout (300 s) since large chunks over slow links take time.
        async with httpx.AsyncClient(timeout=300) as client:
            for _ in range(_MAX_RATE_LIMIT_RETRIES):
                response = await client.put(upload_url, headers=headers, content=data)
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", "10"))
                    await asyncio.sleep(retry_after)
                    continue
                break

        if response.status_code in (200, 201):
            return response.json()
        elif response.status_code == 202:
            return None
        else:
            await self._raise_for_status(response)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """
        Execute an HTTP request through self._http.
        On 429 Too Many Requests, sleeps for Retry-After seconds and retries
        up to _MAX_RATE_LIMIT_RETRIES times before returning the last response.
        Rate-limit retries are invisible to callers and don't count against
        the file-level retry budget in FileTransfer.
        """
        for _ in range(_MAX_RATE_LIMIT_RETRIES):
            response = await self._http.request(method, url, **kwargs)
            if response.status_code != 429:
                return response
            retry_after = int(response.headers.get("Retry-After", "10"))
            await asyncio.sleep(retry_after)
        return response  # return last response if all retries exhausted

    @staticmethod
    async def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code >= 400:
            try:
                body = response.json()
                error = body.get("error", {})
                message = error.get("message") or str(body)
            except Exception:
                message = response.text[:200]
            raise GraphAPIError(response.status_code, message)
