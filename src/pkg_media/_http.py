"""Shared HTTP helpers for API submodules.

Lives in its own module to avoid circular imports between client.py and the
per-API modules (assets, asset_access) that all need
``raise_for_status``.
"""

from __future__ import annotations

from typing import Optional

import httpx

from pkg_media.exceptions import (
    FailedPreconditionError,
    InternalError,
    InvalidArgumentError,
    MediaLibraryError,
    NotFoundError,
    PermissionDeniedError,
    ResourceExhaustedError,
    UnauthenticatedError,
    UnavailableError,
)

# HTTP status -> exception, as the server maps HTTP statuses to gRPC (AD-10).
_ERRORS = {
    400: InvalidArgumentError,
    422: InvalidArgumentError,
    401: UnauthenticatedError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: FailedPreconditionError,
    410: FailedPreconditionError,
    429: ResourceExhaustedError,
    503: UnavailableError,
}


def raise_for_status(response: httpx.Response) -> None:
    """Raise a typed exception for non-2xx responses."""
    if response.is_success:
        return

    try:
        body = response.json()
    except Exception:
        body = response.text

    msg = str(body.get("detail", body) if isinstance(body, dict) else body)
    code = response.status_code
    reason = _detail_reason(body)

    cls = _ERRORS.get(code) or (InternalError if code >= 500 else MediaLibraryError)
    raise cls(msg, status_code=code, body=body, reason=reason)


def _detail_reason(body: object) -> Optional[str]:
    """``detail.reason`` of a ``{"detail": {"reason": …}}`` body, else None."""
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    if isinstance(detail, dict) and isinstance(detail.get("reason"), str):
        return detail["reason"]
    return None
