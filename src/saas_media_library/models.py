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
    step_options: Optional[Dict[str, Dict[str, Any]]] = Field(None, alias="stepOptions")
    use_filename_as_asset_id: bool = Field(False, alias="useFilenameAsAssetId")
    enable_transcription: bool = Field(True, alias="enableTranscription")

    # Ignore unknown fields (e.g., legacy `autoProcess`) so older API responses
    # don't break the client.
    model_config = {"populate_by_name": True, "extra": "ignore"}


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


# ── Rendition models ───────────────────────────────────────────────────

class RenditionVariant(BaseModel):
    name: str
    entrypoint: str
    locale: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    bitrate_kbps: Optional[int] = Field(None, alias="bitrateKbps")
    frame_rate: Optional[float] = Field(None, alias="frameRate")
    channels: Optional[int] = None
    blob_ids: List[UUID] = Field(default_factory=list, alias="blobIds")
    extras: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class Rendition(BaseModel):
    id: UUID
    rendition_type: str = Field(alias="renditionType")
    entrypoint: str
    storage_prefix: Optional[str] = Field(None, alias="storagePrefix")
    blob_ids: List[UUID] = Field(default_factory=list, alias="blobIds")
    variants: List[RenditionVariant] = Field(default_factory=list)
    duration_seconds: Optional[float] = Field(None, alias="durationSeconds")
    pages: Optional[int] = None
    channels: Optional[int] = None
    status: str
    extras: Dict[str, Any] = Field(default_factory=dict)
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
    resource_policy_id: Optional[UUID] = Field(None, alias="resourcePolicyId")
    renditions: List[Rendition] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class DeliveryRenditionVariant(BaseModel):
    name: str
    width: Optional[int] = None
    height: Optional[int] = None
    bitrate_kbps: Optional[int] = Field(None, alias="bitrateKbps")
    preview_url: Optional[str] = Field(None, alias="previewUrl")
    preview_url_type: Optional[str] = Field(None, alias="previewUrlType")
    preview_url_expires_in: Optional[int] = Field(None, alias="previewUrlExpiresIn")

    model_config = {"populate_by_name": True}


class DeliveryRendition(BaseModel):
    id: UUID
    rendition_type: str = Field(alias="renditionType")
    status: str
    entrypoint: str
    duration_seconds: Optional[float] = Field(None, alias="durationSeconds")
    variants: List[DeliveryRenditionVariant] = Field(default_factory=list)
    preview_url: Optional[str] = Field(None, alias="previewUrl")
    preview_url_type: Optional[str] = Field(None, alias="previewUrlType")
    preview_url_expires_in: Optional[int] = Field(None, alias="previewUrlExpiresIn")

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
    renditions: List[DeliveryRendition] = Field(default_factory=list)

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


# ── Asset access (per-user grant) models ───────────────────────────────

class AssetAccessGrant(BaseModel):
    user_id: UUID = Field(alias="userId")
    expires_at: Optional[datetime] = Field(None, alias="expiresAt")
    granted_at: Optional[datetime] = Field(None, alias="grantedAt")
    granted_by_actor_type: Optional[str] = Field(None, alias="grantedByActorType")
    granted_by_actor_id: Optional[str] = Field(None, alias="grantedByActorId")

    model_config = {"populate_by_name": True}
