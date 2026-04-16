"""
OneDrive destination adapter.

Thin wrapper that wires together MicrosoftAuth + GraphClient.
Future destinations (Google Drive, S3, Dropbox) should follow the same pattern:
  - accept auth config
  - expose ensure_folder() and upload_file() with the same signatures as GraphClient
"""

from __future__ import annotations

from pathlib import Path

from auth.microsoft import MicrosoftAuth
from clients.graph import GraphClient


def create_onedrive_client(
    client_id: str,
    tenant_id: str = "common",
    chunk_size_mb: int = 10,
    cache_path: Path | None = None,
) -> tuple[MicrosoftAuth, GraphClient]:
    """
    Factory: returns (MicrosoftAuth, GraphClient) ready for use.

    Caller is responsible for calling auth.ensure_authenticated() before
    using the GraphClient, and for using GraphClient as async context manager.
    """
    kwargs = {}
    if cache_path:
        kwargs["cache_path"] = cache_path

    auth = MicrosoftAuth(client_id=client_id, tenant_id=tenant_id, **kwargs)
    chunk_size = chunk_size_mb * 1024 * 1024

    # Ensure chunk_size is a multiple of 320 KiB (Graph API requirement)
    alignment = 320 * 1024
    chunk_size = (chunk_size // alignment) * alignment
    if chunk_size == 0:
        chunk_size = alignment

    graph = GraphClient(auth=auth, chunk_size=chunk_size)
    return auth, graph
