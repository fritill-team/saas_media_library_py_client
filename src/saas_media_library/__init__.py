from saas_media_library.client import MediaLibraryClient
from saas_media_library.asset_access import AssetAccessAPI
from saas_media_library.models import (
    AssetKind,
    AssetStatus,
    Visibility,
    FileConstraints,
    PermissionRequirements,
    ProcessingConfig,
    ResourcePolicy,
    Asset,
    AssetAccessGrant,
    Rendition,
    RenditionVariant,
    DeliveryAsset,
    BatchResolveResult,
    PaginatedAssets,
)
from saas_media_library.exceptions import (
    MediaLibraryError,
    NotFoundError,
    AuthenticationError,
    ValidationError,
)

__all__ = [
    "MediaLibraryClient",
    "AssetAccessAPI",
    "AssetKind",
    "AssetStatus",
    "Visibility",
    "FileConstraints",
    "PermissionRequirements",
    "ProcessingConfig",
    "ResourcePolicy",
    "Asset",
    "AssetAccessGrant",
    "Rendition",
    "RenditionVariant",
    "DeliveryAsset",
    "BatchResolveResult",
    "PaginatedAssets",
    "MediaLibraryError",
    "NotFoundError",
    "AuthenticationError",
    "ValidationError",
]

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("saas-media-library")
except PackageNotFoundError:
    __version__ = "unknown"
