"""Shared HTTP helpers for API submodules.

Lives in its own module to avoid circular imports between client.py and the
per-API modules (assets, resource_policies, asset_access) that all need
``raise_for_status``.
"""

from __future__ import annotations

from typing import Optional

import httpx

from pkg_media.exceptions import (
    AuthenticationError,
    ConflictError,
    MediaLibraryError,
    NotFoundError,
    ServerError,
    ValidationError,
)


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

    if code == 401 or code == 403:
        raise AuthenticationError(msg, status_code=code, body=body, reason=reason)
    elif code == 404:
        raise NotFoundError(msg, status_code=code, body=body, reason=reason)
    elif code == 409:
        raise ConflictError(msg, status_code=code, body=body, reason=reason)
    elif code == 422:
        raise ValidationError(msg, status_code=code, body=body, reason=reason)
    elif code >= 500:
        raise ServerError(msg, status_code=code, body=body, reason=reason)
    else:
        raise MediaLibraryError(msg, status_code=code, body=body, reason=reason)


def _detail_reason(body: object) -> Optional[str]:
    """``detail.reason`` of a ``{"detail": {"reason": …}}`` body, else None."""
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    if isinstance(detail, dict) and isinstance(detail.get("reason"), str):
        return detail["reason"]
    return None
