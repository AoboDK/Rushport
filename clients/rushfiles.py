"""
Rushfiles API client — wraps ClientGateway and FileCache APIs.

ClientGateway: companies, shares, user profile, file tree
FileCache: binary file content (download/upload)

Key endpoints used:
  GET  /api/authority                                  -> discover auth server
  GET  /api/client/fullprofile                         -> user's companies + shares
  GET  /api/printer/{shareId}/virtualFileChildren      -> list files/folders in a share/folder
  GET  /api/shares/{shareId}/files/{versionId}         -> download file (FileCache)
"""

from __future__ import annotations

from email.utils import parsedate_to_datetime
from typing import AsyncIterator, Optional

import httpx

from auth.rushfiles import RushfilesAuth
from models.rushfiles import (
    Company,
    FullProfile,
    FullProfileResponse,
    Share,
    VirtualFile,
    VirtualFilesResponse,
)

_CLIENTGATEWAY_BASE = "https://clientgateway.your-domain.com"

# Download chunk size for streaming (512 KiB)
_DOWNLOAD_CHUNK_SIZE = 512 * 1024


class RushfilesAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: {message}")


class RushfilesClient:
    """
    Async Rushfiles API client.

    Usage:
        async with RushfilesClient(auth) as client:
            profile = await client.get_full_profile()
            for share in profile.shares:
                async for path, vf in client.walk(share.id):
                    ...
    """

    def __init__(
        self,
        auth: RushfilesAuth,
        clientgateway_base: str = _CLIENTGATEWAY_BASE,
        filecache_base: str = "",
        timeout: float = 60.0,
    ):
        self._auth = auth
        self._cg_base = clientgateway_base.rstrip("/")
        self._fc_base = filecache_base.rstrip("/")
        self._timeout = timeout
        self._http: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "RushfilesClient":
        self._http = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *_) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------------
    # User profile
    # ------------------------------------------------------------------

    async def get_full_profile(self) -> FullProfile:
        """
        Fetch the authenticated user's full profile: user + ManagedShares + SharingCompanies.
        Requires query params tick/api/associationRequired — omitting them returns JSON 404.
        """
        data = await self._cg_get(
            "/api/client/fullprofile",
            params={"tick": 0, "api": "4", "associationRequired": "true"},
        )
        return FullProfileResponse.model_validate(data).data or FullProfile()

    # ------------------------------------------------------------------
    # Companies & Shares
    # ------------------------------------------------------------------

    async def list_companies(self) -> list[Company]:
        profile = await self.get_full_profile()
        return profile.companies

    async def list_shares(self, company_id: Optional[str] = None) -> list[Share]:
        profile = await self.get_full_profile()
        shares = [s for s in profile.shares if not s.is_deleted]
        if company_id:
            shares = [s for s in shares if s.company_id == company_id]
        return shares

    # ------------------------------------------------------------------
    # Virtual file listing
    # ------------------------------------------------------------------

    async def list_children(
        self,
        share_id: str,
        parent_id: Optional[str] = None,
    ) -> list[VirtualFile]:
        """
        List files and directories under a given parent in a share.

        Uses /api/shares/{shareId}/virtualfiles/{parentId}/children which returns
        full metadata including CreationTime, LastWriteTime, InternalName, UploadName.
        When parent_id is None (root listing), uses share_id as the parent.
        """
        effective_parent = parent_id or share_id
        data = await self._cg_get(
            f"/api/shares/{share_id}/virtualfiles/{effective_parent}/children",
        )
        items = VirtualFilesResponse.model_validate(data).data
        return [item for item in items if not item.deleted]

    async def walk(
        self,
        share_id: str,
        parent_id: Optional[str] = None,
        path_prefix: str = "",
    ) -> AsyncIterator[tuple[str, VirtualFile]]:
        """Recursively walk a share tree, yielding (relative_path, VirtualFile)."""
        children = await self.list_children(share_id, parent_id)
        for item in children:
            item_path = f"{path_prefix}/{item.public_name}".lstrip("/")
            yield item_path, item
            if item.is_directory:
                async for sub_path, sub_item in self.walk(
                    share_id, item.internal_name, item_path
                ):
                    yield sub_path, sub_item

    # ------------------------------------------------------------------
    # FileCache URL discovery
    # ------------------------------------------------------------------

    async def _ensure_filecache_url(self) -> None:
        """Populate _fc_base from FullProfile.FilecacheUrls if not already set."""
        if self._fc_base:
            return
        profile = await self.get_full_profile()
        if not profile.filecache_urls:
            raise RushfilesAPIError(0, "No FilecacheUrls in fullprofile — cannot download files.")
        self._fc_base = profile.filecache_urls[0].rstrip("/")

    # ------------------------------------------------------------------
    # File download (FileCache)
    # ------------------------------------------------------------------

    async def open_file_download(
        self,
        share_id: str,
        version_id: str,
        file_name: str = "",
    ) -> "FileDownloadStream":
        """
        Open a streaming download from FileCache.
        Returns a FileDownloadStream context manager whose .last_modified
        attribute is populated from the response Last-Modified header
        before any bytes are read.

        Usage:
            async with await rf.open_file_download(share_id, version_id) as dl:
                await graph.upload_file(..., data_iterator=dl, modified_at=dl.last_modified)
        """
        await self._ensure_filecache_url()
        url = f"{self._fc_base}/api/shares/{share_id}/files/{version_id}"
        params = {"fileName": file_name} if file_name else {}
        auth_headers = await self._auth.auth_headers()
        return FileDownloadStream(url, auth_headers, params)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _cg_get(self, path: str, params: dict | None = None) -> dict:
        return await self._request("GET", self._cg_base + path, params=params)

    async def _request(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        json: dict | None = None,
        retries: int = 1,
    ) -> dict:
        if self._http is None:
            raise RuntimeError("Client not started — use as async context manager.")

        headers = await self._auth.auth_headers()

        for attempt in range(retries + 1):
            response = await self._http.request(
                method, url, headers=headers, params=params, json=json
            )
            if response.status_code == 401 and attempt < retries:
                headers = await self._auth.auth_headers()
                continue
            await self._raise_for_status(response)
            return response.json()

    @staticmethod
    async def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code >= 400:
            try:
                body = response.json()
                message = body.get("Message") or body.get("error_description") or str(body)
            except Exception:
                message = response.text[:200]
            raise RushfilesAPIError(response.status_code, message)


class FileDownloadStream:
    """
    Async context manager for streaming a file from FileCache.

    Attributes:
        last_modified: ISO-8601 string parsed from the HTTP Last-Modified response
                       header, or None if the server did not send it. Available
                       immediately after __aenter__, before any bytes are read.
    """

    def __init__(self, url: str, headers: dict, params: dict):
        self._url = url
        self._headers = headers
        self._params = params
        self.last_modified: Optional[str] = None
        self._http: Optional[httpx.AsyncClient] = None
        self._stream_cm = None
        self._response: Optional[httpx.Response] = None

    async def __aenter__(self) -> "FileDownloadStream":
        self._http = httpx.AsyncClient(timeout=None)
        await self._http.__aenter__()
        self._stream_cm = self._http.stream(
            "GET", self._url, headers=self._headers, params=self._params
        )
        self._response = await self._stream_cm.__aenter__()

        if self._response.status_code >= 400:
            try:
                body = self._response.json()
                msg = body.get("Message") or str(body)
            except Exception:
                msg = f"HTTP {self._response.status_code}"
            raise RushfilesAPIError(self._response.status_code, msg)

        # Parse Last-Modified (RFC 7231) -> ISO 8601 for the Graph API
        raw_lm = self._response.headers.get("Last-Modified")
        if raw_lm:
            try:
                self.last_modified = parsedate_to_datetime(raw_lm).isoformat()
            except Exception:
                self.last_modified = None

        return self

    async def __aexit__(self, *args) -> None:
        if self._stream_cm is not None:
            await self._stream_cm.__aexit__(*args)
        if self._http is not None:
            await self._http.__aexit__(*args)

    def __aiter__(self):
        return self._response.aiter_bytes(_DOWNLOAD_CHUNK_SIZE).__aiter__()
