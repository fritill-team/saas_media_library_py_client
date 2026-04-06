from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Dict, List, Optional, Set
from uuid import UUID

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────

class AssetKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    WEB_APP = "web_app"
    OTHER = "other"
    ARCHIVE = "archive"
    FONT = "font"
    MODEL_3D = "model_3d"
    IMAGE_BATCH = "image_batch"


class Visibility(StrEnum):
    RESTRICTED = "restricted"
    PRIVATE = "private"
    PUBLIC = "public"


class AssetStatus(StrEnum):
    PENDING = "pending"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    AWAITING_INPUT = "awaiting_input"
    READY = "ready"
    FAILED = "failed"
    ARCHIVED = "archived"
    DELETED = "deleted"


# ── Resource Policy models ─────────────────────────────────────────────

class FileConstraints(BaseModel):
    allowed_mime_types: Set[str] = Field(alias="allowedMimeTypes")
    max_size_bytes: Optional[int] = Field(None, alias="maxSizeBytes")
    min_size_bytes: Optional[int] = Field(None, alias="minSizeBytes")
    allow_multiple: bool = Field(False, alias="allowMultiple")

    model_config = {"populate_by_name": True}


class PermissionRequirements(BaseModel):
    required_permissions: Set[str] = Field(default_factory=set, alias="requiredPermissions")
    allow_anonymous: bool = Field(False, alias="allowAnonymous")

    model_config = {"populate_by_name": True}


class ProcessingConfig(BaseModel):
    auto_process: bool = Field(False, alias="autoProcess")
    step_options: Optional[Dict[str, Dict[str, Any]]] = Field(None, alias="stepOptions")
    use_filename_as_asset_id: bool = Field(False, alias="useFilenameAsAssetId")

    model_config = {"populate_by_name": True}


class ResourcePolicy(BaseModel):
    id: UUID
    tenant_id: UUID = Field(alias="tenantId")
    resource_type: str = Field(alias="resourceType")
    collection_name: Optional[str] = Field(None, alias="collectionName")
    file_constraints: FileConstraints = Field(alias="fileConstraints")
    permission_requirements: PermissionRequirements = Field(alias="permissionRequirements")
    processing_config: ProcessingConfig = Field(alias="processingConfig")
    kind: AssetKind
    visibility: Visibility
    is_active: bool = Field(alias="isActive")
    version: int
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")

    model_config = {"populate_by_name": True}


# ── Asset models ───────────────────────────────────────────────────────

class Asset(BaseModel):
    id: UUID
    kind: AssetKind
    title: Optional[str] = None
    original_filename: str = Field(alias="originalFilename")
    mime_type: str = Field(alias="mimeType")
    size_bytes: int = Field(alias="sizeBytes")
    hash_sha256: str = Field(alias="hashSha256")
    status: AssetStatus
    visibility: Visibility
    created_at: datetime = Field(alias="createdAt")
    blob_id: Optional[UUID] = Field(None, alias="blobId")
    download_type: str = Field(alias="downloadType")
    download_url: Optional[str] = Field(None, alias="downloadUrl")
    download_url_expires_in: Optional[int] = Field(None, alias="downloadUrlExpiresIn")
    resource_policy_id: Optional[UUID] = Field(None, alias="resourcePolicyId")

    model_config = {"populate_by_name": True}


class DeliveryAsset(BaseModel):
    id: UUID
    kind: AssetKind
    title: Optional[str] = None
    status: AssetStatus
    visibility: Visibility
    mime_type: str = Field(alias="mimeType")
    download_type: str = Field(alias="downloadType")
    download_url: Optional[str] = Field(None, alias="downloadUrl")
    download_url_expires_in: Optional[int] = Field(None, alias="downloadUrlExpiresIn")

    model_config = {"populate_by_name": True}


class BatchResolveResult(BaseModel):
    asset_id: UUID = Field(alias="assetId")
    status: str  # "ok" | "not_found" | "error"
    asset: Optional[Asset] = None
    error: Optional[str] = None

    model_config = {"populate_by_name": True}


class DeliveryBatchResolveResult(BaseModel):
    asset_id: UUID = Field(alias="assetId")
    status: str
    asset: Optional[DeliveryAsset] = None
    error: Optional[str] = None

    model_config = {"populate_by_name": True}


# ── Paginated response ─────────────────────────────────────────────────

class PaginatedAssets(BaseModel):
    items: List[Asset]
    total: int
    page: int
    page_size: int = Field(alias="pageSize")

    model_config = {"populate_by_name": True}


class PaginatedDeliveryAssets(BaseModel):
    items: List[DeliveryAsset]
    total: int
    page: int
    page_size: int = Field(alias="pageSize")

    model_config = {"populate_by_name": True}
