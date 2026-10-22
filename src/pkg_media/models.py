from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Dict, List, Optional
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


class DownloadType(StrEnum):
    """How a delivery link is served (``DeliveryAsset.download_type`` and
    every ``preview_url_type``).

    The fields stay plain ``str`` so a newer server value never breaks
    parsing; compare them against these values.
    """
    GATE = "gate"                  # R2 object behind the delivery gate: /o/… with a
                                   # token (expires), or tokenless when the edge may
                                   # cache it (no expiry); the URL is None when the
                                   # gate would refuse the object
    UNAVAILABLE = "unavailable"    # no URL: the bytes are not in the tenant's
                                   # R2 bucket (``download_url`` is None)


# ── Upload processing config ───────────────────────────────────────

class ProcessingConfig(BaseModel):
    step_options: Optional[Dict[str, Dict[str, Any]]] = Field(None, alias="stepOptions")
    use_filename_as_asset_id: bool = Field(False, alias="useFilenameAsAssetId")
    # False, as the server's upload default.
    enable_transcription: bool = Field(False, alias="enableTranscription")

    # Ignore unknown fields (e.g., legacy `autoProcess`) so older API responses
    # don't break the client.
    model_config = {"populate_by_name": True, "extra": "ignore"}


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
    preview_url: Optional[str] = Field(None, alias="previewUrl")
    preview_url_type: Optional[str] = Field(None, alias="previewUrlType")
    preview_url_expires_in: Optional[int] = Field(None, alias="previewUrlExpiresIn")

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
    preview_url: Optional[str] = Field(None, alias="previewUrl")
    preview_url_type: Optional[str] = Field(None, alias="previewUrlType")
    preview_url_expires_in: Optional[int] = Field(None, alias="previewUrlExpiresIn")

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
    # The upload session that created the asset. None from servers that
    # don't return it, and for assets not created by an upload session.
    upload_session_id: Optional[UUID] = Field(None, alias="uploadSessionId")
    renditions: List[Rendition] = Field(default_factory=list)
    # A gate link to the file (``preview_url_type`` is a ``DownloadType``);
    # None when there is none. ``preview_url_expires_in`` is None for a link
    # that does not expire.
    preview_url: Optional[str] = Field(None, alias="previewUrl")
    preview_url_type: Optional[str] = Field(None, alias="previewUrlType")
    preview_url_expires_in: Optional[int] = Field(None, alias="previewUrlExpiresIn")
    # HLS/DASH entrypoints on the stream Worker, for R2 video/audio with a
    # ready CMAF rendition, and their token's lifetime in seconds. None from
    # servers that don't send them, and when there is no stream.
    hls_url: Optional[str] = Field(None, alias="hlsUrl")
    dash_url: Optional[str] = Field(None, alias="dashUrl")
    stream_url_expires_in: Optional[int] = Field(None, alias="streamUrlExpiresIn")

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
    # HLS/DASH entrypoints on the stream Worker, for R2 video/audio with a
    # ready CMAF rendition, and their token's lifetime in seconds. None from
    # servers that don't send them, and when there is no stream.
    hls_url: Optional[str] = Field(None, alias="hlsUrl")
    dash_url: Optional[str] = Field(None, alias="dashUrl")
    stream_url_expires_in: Optional[int] = Field(None, alias="streamUrlExpiresIn")

    model_config = {"populate_by_name": True}


class BatchResolveResult(BaseModel):
    asset_id: UUID = Field(alias="assetId")
    status: str  # "ok" | "not_found" | "error"
    asset: Optional[Asset] = None
    error: Optional[str] = None

    model_config = {"populate_by_name": True}


class DeliveryBatchResolveResult(BaseModel):
    """One item of a delivery resolve (gRPC ``ResolveDeliveryAssets``).

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


# ── Web-app deploy models ──────────────────────────────────────────────

class WebAppDeploy(BaseModel):
    """One deploy of a web app (``GET /manage/assets/{id}/deploys``).

    ``status`` stays a plain ``str`` (``live``, ``ready``, ``uploading``,
    ``failed``, ``deleted``…) so a newer server status never breaks parsing.
    """
    id: UUID
    status: str
    created_at: datetime = Field(alias="createdAt")
    is_live: bool = Field(alias="isLive")
    blob_id: Optional[UUID] = Field(None, alias="blobId")
    scan_status: Optional[str] = Field(None, alias="scanStatus")
    file_count: int = Field(alias="fileCount")

    model_config = {"populate_by_name": True}


class WebAppDeployList(BaseModel):
    """A page of a web app's deploys, newest first.

    ``next_offset`` is None on the last page.
    """
    live_deploy_id: Optional[UUID] = Field(None, alias="liveDeployId")
    deploys: List[WebAppDeploy] = Field(default_factory=list)
    limit: int
    offset: int
    next_offset: Optional[int] = Field(None, alias="nextOffset")

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


