from saas_media_library.client import MediaLibraryClient
from saas_media_library.asset_access import AssetAccessAPI
from saas_media_library.assets import BulkVisibilityResult
from saas_media_library.cache import AssetUrlCache
from saas_media_library.delivery import DeliveryAPI, thumbnail_url
from saas_media_library.gate import AccessGate
from saas_media_library.grants import GrantScope, GrantsAPI, MediaGrant, grant_profile_payload
from saas_media_library.migration import (
    find_profile,
    policy_key,
    profiles_from_policies,
    render_profiles_module,
)
from saas_media_library.permalinks import (
    Permalink,
    PermalinksAPI,
    permalink_url,
    validate_permalink_name,
)
from saas_media_library.processing_options import ProcessingOptionsAPI
from saas_media_library.profiles import UploadProfile
from saas_media_library.uploads import UploadsAPI
from saas_media_library.service_uploads import (
    ClaimedUpload,
    InMemoryUploadStore,
    UploadRecord,
    UploadSessionStore,
    claim_upload,
)
from saas_media_library.models import (
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
from saas_media_library.exceptions import (
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
    __version__ = version("saas-media-library")
except PackageNotFoundError:
    __version__ = "unknown"
