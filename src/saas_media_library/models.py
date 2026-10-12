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


class ResolveStatus(StrEnum):
    """Per-item status of a delivery batch resolve.

    ``DeliveryBatchResolveResult.status`` stays a plain ``str`` so a newer
    server status never breaks parsing; compare it against these values.
    """
    OK = "ok"                # asset + download URL returned
    NOT_FOUND = "not_found"  # no such asset in the tenant
    FORBIDDEN = "forbidden"  # caller may not open it (RESTRICTED); thumbnailUrl may be set
    ERROR = "error"          # asset exists but its URL could not be resolved


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
    # The upload session that created the asset. None from servers that
    # don't return it, and for assets not created by an upload session.
    upload_session_id: Optional[UUID] = Field(None, alias="uploadSessionId")
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
    language: Optional[str] = None
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
    """One item of ``POST /delivery/assets/resolve``.

    ``status`` is one of ``ResolveStatus``: ``ok`` (``asset`` is set),
    ``not_found``, ``forbidden`` or ``error`` (``error`` explains why).
    ``thumbnail_url`` is a public thumbnail link, also sent for ``forbidden``
    and ``error`` items so a locked asset can still show a preview.
    """
    asset_id: UUID = Field(alias="assetId")
    status: str
    thumbnail_url: Optional[str] = Field(None, alias="thumbnailUrl")
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


# ── Upload session models ──────────────────────────────────────────────
# The upload endpoints answer in snake_case, so these models have no aliases.

class UploadSession(BaseModel):
    id: UUID
    state: str  # "initiated" | "in_progress" | "finalized" | "failed" | "expired" | "aborted"
    part_size_bytes: int
    parts_expected: Optional[int] = None
    parts_received: int
    received_parts: List[int] = Field(default_factory=list)
    expires_at: datetime
    checksum_verified: bool
    final_blob_id: Optional[UUID] = None


class UploadPartResult(BaseModel):
    session_id: UUID
    part_number: int
    size_bytes: int
    sha256_hex: str
    verified: bool
    parts_received: int


class UploadFinalizeResult(BaseModel):
    session_id: UUID
    final_blob_id: UUID
    asset_id: UUID
    checksum_verified: bool
    sha256_hex: str


class UploadAbortResult(BaseModel):
    session_id: UUID
    aborted: bool


# ── Asset access (per-user grant) models ───────────────────────────────

class AssetAccessGrant(BaseModel):
    user_id: UUID = Field(alias="userId")
    expires_at: Optional[datetime] = Field(None, alias="expiresAt")
    granted_at: Optional[datetime] = Field(None, alias="grantedAt")
    granted_by_actor_type: Optional[str] = Field(None, alias="grantedByActorType")
    granted_by_actor_id: Optional[str] = Field(None, alias="grantedByActorId")

    model_config = {"populate_by_name": True}
