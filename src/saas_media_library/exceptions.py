from __future__ import annotations

from typing import Any, Optional


class MediaLibraryError(Exception):
    """Base exception for all Media Library client errors."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        body: Optional[Any] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class NotFoundError(MediaLibraryError):
    """Resource not found (404)."""


class AuthenticationError(MediaLibraryError):
    """Authentication or authorization failure (401/403)."""


class ValidationError(MediaLibraryError):
    """Request validation error (422)."""


class ConflictError(MediaLibraryError):
    """Conflict error (409)."""


class ServerError(MediaLibraryError):
    """Server-side error (5xx)."""
