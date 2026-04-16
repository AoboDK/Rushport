"""
Pydantic models for Rushfiles API responses.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Share(BaseModel):
    """A Rushfiles share from ManagedShares in the fullprofile response."""
    id: str = Field(alias="ShareId")
    name: str = Field(alias="ShareName")
    company_id: str = Field(alias="CompanyId")
    company_name: Optional[str] = Field(default=None, alias="CompanyName")
    # Status: 1 = active, 0/other = inactive/deleted. No explicit IsDeleted field on this payload.
    status: int = Field(default=1, alias="Status")

    model_config = {"populate_by_name": True, "extra": "ignore"}

    @property
    def is_deleted(self) -> bool:
        return self.status != 1


class SharesResponse(BaseModel):
    data: list[Share] = Field(default_factory=list, alias="Data")

    model_config = {"populate_by_name": True}


class VirtualFile(BaseModel):
    """
    A file or directory within a Rushfiles share.

    From /api/shares/{shareId}/virtualfiles/{parentId}/children (NOT the printer endpoint,
    which omits timestamps and uses different field names).

    Real API field names (verified from live response):
      InternalName  — stable item ID used for subdirectory listing and individual lookup
      PublicName    — human-readable filename
      IsFile        — bool (true for files, false for dirs)
      ShareId       — share the item belongs to
      EndOfFile     — file size in bytes (0 for directories)
      ParrentId     — parent folder ID (API has a typo: double-r)
      CreationTime / LastWriteTime — UTC ISO-8601 datetimes; used for date preservation
      UploadName    — version ID for FileCache download URL
      Deleted       — soft-delete flag
    """
    internal_name: str = Field(alias="InternalName")
    share_id: str = Field(alias="ShareId")
    name: str = Field(alias="PublicName")
    is_file_flag: bool = Field(alias="IsFile")
    end_of_file: int = Field(default=0, alias="EndOfFile")
    parent_id: Optional[str] = Field(default=None, alias="ParrentId")  # API typo
    upload_name: Optional[str] = Field(default=None, alias="UploadName")
    last_write_time: Optional[datetime] = Field(default=None, alias="LastWriteTime")
    create_time: Optional[datetime] = Field(default=None, alias="CreationTime")
    deleted: bool = Field(default=False, alias="Deleted")

    model_config = {"populate_by_name": True, "extra": "ignore"}

    @property
    def id(self) -> str:
        return self.internal_name

    @property
    def public_name(self) -> str:
        return self.name

    @property
    def is_file(self) -> bool:
        return self.is_file_flag

    @property
    def is_directory(self) -> bool:
        return not self.is_file_flag

    @property
    def is_deleted(self) -> bool:
        return self.deleted

    @property
    def version_id(self) -> Optional[str]:
        """Version ID for FileCache download URL. UploadName if present, else InternalName."""
        return self.upload_name or self.internal_name

    @property
    def size_bytes(self) -> int:
        return self.end_of_file


class VirtualFilesResponse(BaseModel):
    data: list[VirtualFile] = Field(default_factory=list, alias="Data")

    model_config = {"populate_by_name": True, "extra": "ignore"}


class Company(BaseModel):
    id: str = Field(alias="CompanyId")
    name: str = Field(alias="CompanyName")

    model_config = {"populate_by_name": True, "extra": "ignore"}


class CompaniesResponse(BaseModel):
    data: list[Company] = Field(default_factory=list, alias="Data")

    model_config = {"populate_by_name": True}


class UserProfile(BaseModel):
    user_id: str = Field(alias="UserId")
    email: str = Field(alias="Email")
    name: Optional[str] = Field(default=None, alias="Name")
    primary_domain: Optional[str] = Field(default=None, alias="PrimaryDomain")

    model_config = {"populate_by_name": True, "extra": "ignore"}


class FullProfile(BaseModel):
    """
    Response from GET /api/client/fullprofile?tick=0&api=4&associationRequired=true.

    The API returns `ManagedShares` and `SharingCompanies` — NOT `Shares`/`Companies`.
    `shares` and `companies` are exposed as properties for backward-compatible access.
    """
    user: Optional[UserProfile] = Field(default=None, alias="User")
    managed_shares: list[Share] = Field(default_factory=list, alias="ManagedShares")
    sharing_companies: list[Company] = Field(default_factory=list, alias="SharingCompanies")
    all_sharing_companies: list[Company] = Field(default_factory=list, alias="AllSharingCompanies")
    filecache_urls: list[str] = Field(default_factory=list, alias="FilecacheUrls")

    model_config = {"populate_by_name": True, "extra": "ignore"}

    @property
    def shares(self) -> list[Share]:
        return self.managed_shares

    @property
    def companies(self) -> list[Company]:
        return self.sharing_companies


class FullProfileResponse(BaseModel):
    data: Optional[FullProfile] = Field(default=None, alias="Data")

    model_config = {"populate_by_name": True}
