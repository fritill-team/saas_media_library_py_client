from __future__ import annotations

from typing import Any, Optional


class MediaLibraryError(Exception):
    """Base exception for all media client errors.

    ``reason`` is the refusal's reason when the server sends one: the HTTP
    body's ``detail.reason`` (e.g. the web-app deploy routes:
    ``deploy_not_found``, ``deploy_deleted``…), or the gRPC status's
    ``ErrorInfo.reason`` (constants in ``pkg_media.reasons``); else None.
    ``domain`` is the ``ErrorInfo.domain`` (the refusing service's full name,
    gRPC only). ``status_code`` is the HTTP status (HTTP only) and
    ``grpc_status`` the ``grpc.StatusCode`` (gRPC only).
    """

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        body: Optional[Any] = None,
        *,
        reason: Optional[str] = None,
        grpc_status: Optional[Any] = None,
        domain: Optional[str] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.reason = reason
        self.grpc_status = grpc_status
        self.domain = domain

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
    CONTEXT_MISMATCH = "context_mismatch"
    ALREADY_ATTACHED = "already_attached"

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


class ConflictError(MediaLibraryError):
    """Conflict error (409)."""


class ServerError(MediaLibraryError):
    """Server-side error (5xx)."""


# ── One class per gRPC status (AD-10) ──────────────────────────────────
# Both transports raise them: HTTP statuses map as the server maps them to
# gRPC (400/422 INVALID_ARGUMENT, 401 UNAUTHENTICATED, 403 PERMISSION_DENIED,
# 404 NOT_FOUND, 409/410 FAILED_PRECONDITION, 429 RESOURCE_EXHAUSTED,
# 503 UNAVAILABLE, other 5xx INTERNAL). Each subclasses the HTTP-era class
# for the same failure, so existing ``except`` clauses keep working.


class InvalidArgumentError(ValidationError):
    """INVALID_ARGUMENT (HTTP 400 or 422): the request is wrong."""


class UnauthenticatedError(AuthenticationError):
    """UNAUTHENTICATED (HTTP 401): the credential is missing or rejected."""


class PermissionDeniedError(AuthenticationError):
    """PERMISSION_DENIED (HTTP 403): the caller may not do this."""


class FailedPreconditionError(ConflictError):
    """FAILED_PRECONDITION (HTTP 409 or 410): not in a state that allows it."""


class ResourceExhaustedError(MediaLibraryError):
    """RESOURCE_EXHAUSTED (HTTP 429): rate limited."""


class UnavailableError(ServerError):
    """UNAVAILABLE (HTTP 503, or the server could not be reached): transient."""


class DeadlineExceededError(ServerError):
    """DEADLINE_EXCEEDED: the call timed out (gRPC only)."""


class UnimplementedError(ServerError):
    """UNIMPLEMENTED: the server does not serve this method (gRPC only)."""


class InternalError(ServerError):
    """INTERNAL (HTTP 500 and other 5xx): a server-side failure."""
