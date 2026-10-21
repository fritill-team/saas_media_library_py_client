"""Refusal reasons of the ``itqadem.media.v1`` contract (AD-10).

A refusal is a gRPC status carrying ``google.rpc.ErrorInfo(domain, reason)``:
``domain`` is the full name of the service that refused, ``reason`` one of the
constants below (the HTTP API sends the same string as ``detail.reason``).
Clients key on ``(domain, reason)``.

``REFUSALS`` is each service's whole vocabulary with its status, exactly the
tables in the ``.proto`` comments (``tests/test_media_contract.py`` compares
them). A reason not listed there must not be sent.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

import grpc

from pkg_media import methods

# ── Common: every service, in its own domain ───────────────────────────
CREDENTIAL_MISSING = "credential_missing"
CREDENTIAL_REJECTED = "credential_rejected"
PERMISSION_DENIED = "permission_denied"
INVALID_ARGUMENT = "invalid_argument"
IDEMPOTENCY_KEY_REQUIRED = "idempotency_key_required"
IDEMPOTENCY_KEY_REUSED = "idempotency_key_reused"
RATE_LIMITED = "rate_limited"
UNAVAILABLE = "unavailable"
INTERNAL_ERROR = "internal_error"

# ── Assets ─────────────────────────────────────────────────────────────
ASSET_NOT_FOUND = "asset_not_found"
NOT_A_WEB_APP = "not_a_web_app"
DEPLOY_NOT_FOUND = "deploy_not_found"
INVALID_ASSET_STATE = "invalid_asset_state"
DEPLOY_DELETED = "deploy_deleted"
DEPLOY_FAILED = "deploy_failed"
DEPLOY_UPLOADING = "deploy_uploading"
SCAN_NOT_ALLOWED = "scan_not_allowed"
DEPLOY_CHANGED = "deploy_changed"
#: Assets, Uploads and Delivery: the app has no R2 storage configured.
TENANT_STORAGE_NOT_FOUND = "tenant_storage_not_found"

# ── Uploads ────────────────────────────────────────────────────────────
SESSION_NOT_FOUND = "session_not_found"
SESSION_EXPIRED = "session_expired"
INVALID_SESSION_STATE = "invalid_session_state"
CHECKSUM_MISMATCH = "checksum_mismatch"
INCOMPLETE_UPLOAD = "incomplete_upload"
FILE_SIZE_MISMATCH = "file_size_mismatch"
FILE_SIZE_LIMIT_EXCEEDED = "file_size_limit_exceeded"
UNSUPPORTED_MIME_TYPE = "unsupported_mime_type"
INVALID_PROCESSING_CONFIG = "invalid_processing_config"
GRANTS_NOT_CONFIGURED = "grants_not_configured"

# ── Permalinks ─────────────────────────────────────────────────────────
PERMALINK_NOT_FOUND = "permalink_not_found"
INVALID_PERMALINK_NAME = "invalid_permalink_name"
ASSET_NOT_PUBLIC = "asset_not_public"

# ── Delivery ───────────────────────────────────────────────────────────
BLOB_NOT_FOUND = "blob_not_found"

_S = grpc.StatusCode

COMMON: Mapping[str, grpc.StatusCode] = MappingProxyType({
    CREDENTIAL_MISSING: _S.UNAUTHENTICATED,
    CREDENTIAL_REJECTED: _S.UNAUTHENTICATED,
    PERMISSION_DENIED: _S.PERMISSION_DENIED,
    INVALID_ARGUMENT: _S.INVALID_ARGUMENT,
    IDEMPOTENCY_KEY_REQUIRED: _S.INVALID_ARGUMENT,
    IDEMPOTENCY_KEY_REUSED: _S.FAILED_PRECONDITION,
    RATE_LIMITED: _S.RESOURCE_EXHAUSTED,
    UNAVAILABLE: _S.UNAVAILABLE,
    INTERNAL_ERROR: _S.INTERNAL,
})


def _with_common(own: dict[str, grpc.StatusCode]) -> Mapping[str, grpc.StatusCode]:
    return MappingProxyType({**COMMON, **own})


#: Service full name (the ErrorInfo domain) -> reason -> status.
REFUSALS: Mapping[str, Mapping[str, grpc.StatusCode]] = MappingProxyType({
    methods.Assets.SERVICE: _with_common({
        ASSET_NOT_FOUND: _S.NOT_FOUND,
        NOT_A_WEB_APP: _S.NOT_FOUND,
        DEPLOY_NOT_FOUND: _S.NOT_FOUND,
        INVALID_ASSET_STATE: _S.FAILED_PRECONDITION,
        DEPLOY_DELETED: _S.FAILED_PRECONDITION,
        DEPLOY_FAILED: _S.FAILED_PRECONDITION,
        DEPLOY_UPLOADING: _S.FAILED_PRECONDITION,
        SCAN_NOT_ALLOWED: _S.FAILED_PRECONDITION,
        DEPLOY_CHANGED: _S.FAILED_PRECONDITION,
        TENANT_STORAGE_NOT_FOUND: _S.NOT_FOUND,
    }),
    methods.Uploads.SERVICE: _with_common({
        SESSION_NOT_FOUND: _S.NOT_FOUND,
        SESSION_EXPIRED: _S.FAILED_PRECONDITION,
        INVALID_SESSION_STATE: _S.FAILED_PRECONDITION,
        CHECKSUM_MISMATCH: _S.FAILED_PRECONDITION,
        INCOMPLETE_UPLOAD: _S.INVALID_ARGUMENT,
        FILE_SIZE_MISMATCH: _S.INVALID_ARGUMENT,
        FILE_SIZE_LIMIT_EXCEEDED: _S.INVALID_ARGUMENT,
        UNSUPPORTED_MIME_TYPE: _S.INVALID_ARGUMENT,
        INVALID_PROCESSING_CONFIG: _S.INVALID_ARGUMENT,
        GRANTS_NOT_CONFIGURED: _S.UNAVAILABLE,
        TENANT_STORAGE_NOT_FOUND: _S.NOT_FOUND,
    }),
    methods.Grants.SERVICE: _with_common({
        GRANTS_NOT_CONFIGURED: _S.UNAVAILABLE,
    }),
    methods.Permalinks.SERVICE: _with_common({
        PERMALINK_NOT_FOUND: _S.NOT_FOUND,
        ASSET_NOT_FOUND: _S.NOT_FOUND,
        INVALID_PERMALINK_NAME: _S.INVALID_ARGUMENT,
        ASSET_NOT_PUBLIC: _S.INVALID_ARGUMENT,
    }),
    methods.Delivery.SERVICE: _with_common({
        ASSET_NOT_FOUND: _S.NOT_FOUND,
        BLOB_NOT_FOUND: _S.NOT_FOUND,
        TENANT_STORAGE_NOT_FOUND: _S.NOT_FOUND,
    }),
    methods.Processing.SERVICE: _with_common({}),
})

#: AD-10's HTTP status -> gRPC status map; anything not listed is INTERNAL.
GRPC_STATUS_FOR_HTTP: Mapping[int, grpc.StatusCode] = MappingProxyType({
    400: _S.INVALID_ARGUMENT,
    422: _S.INVALID_ARGUMENT,
    401: _S.UNAUTHENTICATED,
    403: _S.PERMISSION_DENIED,
    404: _S.NOT_FOUND,
    409: _S.FAILED_PRECONDITION,
    410: _S.FAILED_PRECONDITION,
    429: _S.RESOURCE_EXHAUSTED,
    503: _S.UNAVAILABLE,
})


def grpc_status_for_http(status_code: int) -> grpc.StatusCode:
    """The gRPC status for an HTTP status, per AD-10."""
    return GRPC_STATUS_FOR_HTTP.get(status_code, _S.INTERNAL)
