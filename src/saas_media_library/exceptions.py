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

    def __reduce__(self):
        # The default rebuilds with ``cls(*self.args)``, which fails for
        # subclasses with required keyword-only arguments (``UploadRejected``'s
        # ``reason``). Restore ``args`` and attributes without calling
        # ``__init__`` so every subclass survives pickle and copy.
        return (_rebuild, (type(self), self.args, self.__dict__))


def _rebuild(cls: type, args: tuple, state: dict) -> MediaLibraryError:
    exc = cls.__new__(cls, *args)
    exc.args = args
    exc.__dict__.update(state)
    return exc


class NotFoundError(MediaLibraryError):
    """Resource not found (404)."""


class AuthenticationError(MediaLibraryError):
    """Authentication or authorization failure (401/403)."""


class ValidationError(MediaLibraryError):
    """Request validation error (422)."""


class UploadRejected(ValidationError):
    """A file or asset failed an ``UploadProfile`` check on the client side.

    Raised before anything is sent to the server, so ``status_code`` is None.
    Subclasses ``ValidationError`` so a single ``except ValidationError``
    handles both the app's own upload rules and the server's 422 responses.

    ``reason`` says which rule failed (one of the constants below), so callers
    can map it to their own HTTP status (e.g. 415 for ``MIME_TYPE``, 413 for
    ``TOO_LARGE``).
    """

    MIME_TYPE = "mime_type"
    SIZE_REQUIRED = "size_required"
    TOO_LARGE = "too_large"
    TOO_SMALL = "too_small"
    KIND = "kind"
    # Attaching a browser upload (``UploadsAPI.verify_upload``,
    # ``service_uploads.claim_upload``):
    NOT_FINALIZED = "not_finalized"
    ASSET_MISMATCH = "asset_mismatch"
    VISIBILITY = "visibility"
    UNKNOWN_SESSION = "unknown_session"
    PROFILE_MISMATCH = "profile_mismatch"
    ALREADY_ATTACHED = "already_attached"

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


class ConflictError(MediaLibraryError):
    """Conflict error (409)."""


class ServerError(MediaLibraryError):
    """Server-side error (5xx)."""
