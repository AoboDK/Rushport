"""
Pydantic models for Microsoft Graph / OneDrive API responses.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class DriveItem(BaseModel):
    """A file or folder in OneDrive."""
    id: str
    name: str
    size: Optional[int] = None
    web_url: Optional[str] = Field(default=None, alias="webUrl")
    etag: Optional[str] = None

    model_config = {"populate_by_name": True}


class UploadSession(BaseModel):
    """
    Response from creating a large-file upload session.
    uploadUrl is a pre-authenticated URL valid for ~10 minutes (refreshed per chunk).
    """
    upload_url: str = Field(alias="uploadUrl")
    expiration_date_time: Optional[str] = Field(default=None, alias="expirationDateTime")

    model_config = {"populate_by_name": True}


class UploadSessionRequest(BaseModel):
    """Request body for creating an upload session."""
    item: dict = Field(
        default_factory=lambda: {
            "@microsoft.graph.conflictBehavior": "replace",
        }
    )
