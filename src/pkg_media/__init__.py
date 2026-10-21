from pkg_media.client import MediaLibraryClient
from pkg_media.asset_access import AssetAccessAPI
from pkg_media.assets import BulkVisibilityResult
from pkg_media.cache import AssetUrlCache
from pkg_media.delivery import DeliveryAPI, thumbnail_url
from pkg_media.gate import AccessGate
from pkg_media.grants import GrantScope, GrantsAPI, MediaGrant, grant_profile_payload
from pkg_media.migration import (
    find_profile,
    policy_key,
    profiles_from_policies,
    render_profiles_module,
)
from pkg_media.permalinks import (
    Permalink,
    PermalinksAPI,
    permalink_url,
    validate_permalink_name,
)
from pkg_media.processing_options import ProcessingOptionsAPI
from pkg_media.profiles import UploadProfile
from pkg_media.uploads import UploadsAPI
from pkg_media.service_uploads import (
    ClaimedUpload,
    InMemoryUploadStore,
    UploadRecord,
    UploadSessionStore,
    claim_upload,
)
from pkg_media.models import (
    AssetKind,
    AssetStatus,
    ResolveStatus,
    DownloadType,
    Visibility,
    FileConstraints,
    PermissionRequirements,
    ProcessingConfig,
    ResourcePolicy,
    Asset,
    AssetAccessGrant,
    Rendition,
    RenditionVariant,
    DeliveryRenditionVariant,
    DeliveryRendition,
    DeliveryAsset,
    BatchResolveResult,
    DeliveryBatchResolveResult,
    PaginatedAssets,
    PaginatedDeliveryAssets,
    WebAppDeploy,
    WebAppDeployList,
    UploadSession,
    UploadPartResult,
    UploadFinalizeResult,
    UploadAbortResult,
)
from pkg_media.exceptions import (
    MediaLibraryError,
    NotFoundError,
    AuthenticationError,
    ValidationError,
    ConflictError,
    ServerError,
    UploadRejected,
)

__all__ = [
    "MediaLibraryClient",
    "AssetAccessAPI",
    "BulkVisibilityResult",
    "AccessGate",
    "GrantScope",
    "GrantsAPI",
    "MediaGrant",
    "grant_profile_payload",
    "Permalink",
    "PermalinksAPI",
    "permalink_url",
    "validate_permalink_name",
    "AssetUrlCache",
    "DeliveryAPI",
    "ProcessingOptionsAPI",
    "UploadsAPI",
    "UploadProfile",
    "ClaimedUpload",
    "InMemoryUploadStore",
    "UploadRecord",
    "UploadSessionStore",
    "claim_upload",
    "thumbnail_url",
    "find_profile",
    "policy_key",
    "profiles_from_policies",
    "render_profiles_module",
    "AssetKind",
    "AssetStatus",
    "ResolveStatus",
    "DownloadType",
    "Visibility",
    "FileConstraints",
    "PermissionRequirements",
    "ProcessingConfig",
    "ResourcePolicy",
    "Asset",
    "AssetAccessGrant",
    "Rendition",
    "RenditionVariant",
    "DeliveryRenditionVariant",
    "DeliveryRendition",
    "DeliveryAsset",
    "BatchResolveResult",
    "DeliveryBatchResolveResult",
    "PaginatedAssets",
    "PaginatedDeliveryAssets",
    "WebAppDeploy",
    "WebAppDeployList",
    "UploadSession",
    "UploadPartResult",
    "UploadFinalizeResult",
    "UploadAbortResult",
    "MediaLibraryError",
    "NotFoundError",
    "AuthenticationError",
    "ValidationError",
    "ConflictError",
    "ServerError",
    "UploadRejected",
]

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pkg-media")
except PackageNotFoundError:
    __version__ = "unknown"
