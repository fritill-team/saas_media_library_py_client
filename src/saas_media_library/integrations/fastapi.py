"""FastAPI routes that serve media links through an ``AccessGate``, and
issue media grants for the app's browser users.

Needs the ``fastapi`` extra: ``pip install "saas-media-library[fastapi]"``.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional
from uuid import UUID

try:
    from fastapi import APIRouter, Depends, HTTPException
    from fastapi.responses import JSONResponse
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError(
        "saas_media_library.integrations.fastapi requires FastAPI. "
        'Install it with: pip install "saas-media-library[fastapi]"'
    ) from exc
from pydantic import BaseModel, Field

from saas_media_library.exceptions import MediaLibraryError, ValidationError
from saas_media_library.gate import AccessGate
from saas_media_library.grants import GrantScope
from saas_media_library.profiles import UploadProfile
from saas_media_library.models import ResolveStatus

logger = logging.getLogger(__name__)

# Same limit as the server's POST /delivery/assets/resolve.
MAX_RESOLVE_IDS = 100


class ResolveRequest(BaseModel):
    """Body of ``POST {prefix}/resolve``, as the server's delivery endpoint."""

    asset_ids: List[UUID] = Field(..., alias="assetIds", min_length=1, max_length=MAX_RESOLVE_IDS)

    model_config = {"populate_by_name": True}


def create_media_router(
    gate: AccessGate,
    get_current_user: Callable[..., Any],
    *,
    prefix: str = "/media",
) -> APIRouter:
    """Routes that mirror the media library's delivery API for RESTRICTED assets.

    - ``POST {prefix}/resolve`` with ``{"assetIds": [...]}`` (1 to 100 ids):
      the same body as ``POST /delivery/assets/resolve``.
    - ``GET {prefix}/{asset_id}``: the asset, as ``GET /delivery/assets/{id}``.
      403 with ``{"detail", "thumbnailUrl"}`` when denied, 404 when not found,
      502 when the media library could not produce a link.

    ``get_current_user`` is the app's FastAPI dependency; whatever it returns
    is passed to ``gate``'s ``can_access``. Failures talking to the media
    library (error responses, connection errors, timeouts, malformed
    responses) answer 502 with a generic detail, so an expired service token
    is not mistaken for the user's own 401. The gate raises all of them as
    ``MediaLibraryError``; other exceptions from ``can_access`` and all
    exceptions from ``get_current_user`` are not caught here (500, or the
    app's own handlers). A ``MediaLibraryError`` raised by ``can_access`` is
    answered 502 like the gate's own. Endpoints are sync and run in
    FastAPI's threadpool.

    Usage::

        app.include_router(create_media_router(gate, get_current_user))
    """
    router = APIRouter(prefix=prefix)

    @router.post("/resolve", response_model=None)
    def resolve_assets(
        payload: ResolveRequest,
        user: Any = Depends(get_current_user),
    ) -> Dict[str, Any]:
        with _media_library_errors():
            results = gate.resolve(user, payload.asset_ids)
        return gate.to_payload(results)

    @router.get("/{asset_id}", response_model=None)
    def get_asset(
        asset_id: UUID,
        user: Any = Depends(get_current_user),
    ) -> Any:
        with _media_library_errors():
            result = gate.resolve_one(user, asset_id)

        if result.status == ResolveStatus.OK and result.asset is not None:
            return result.asset.model_dump(mode="json", by_alias=True)
        if result.status == ResolveStatus.NOT_FOUND:
            raise HTTPException(status_code=404, detail=f"Asset {asset_id} not found")
        if result.status == ResolveStatus.FORBIDDEN:
            status_code, detail = 403, result.error or AccessGate.DENIED_ERROR
        else:
            status_code, detail = 502, result.error or "Could not resolve the asset's link"
        return JSONResponse(
            status_code=status_code,
            content={"detail": detail, "thumbnailUrl": result.thumbnail_url},
        )

    return router


class GrantRequest(BaseModel):
    """Body of ``POST {prefix}/grants``: what the browser asks to do.

    ``profile`` is the name of one of the app's upload profiles (never a
    profile definition: the browser must not choose kind, visibility or
    processing)."""

    scopes: List[GrantScope] = Field(..., min_length=1)
    asset_ids: List[UUID] = Field(default_factory=list, alias="assetIds", max_length=MAX_RESOLVE_IDS)
    session_ids: List[UUID] = Field(default_factory=list, alias="sessionIds", max_length=100)
    profile: Optional[str] = None

    model_config = {"populate_by_name": True}


def create_grant_router(
    client: Any,
    authorize: Callable[[Any, GrantRequest, Optional[UploadProfile]], bool],
    get_current_user: Callable[..., Any],
    *,
    profiles: Optional[Mapping[str, UploadProfile]] = None,
    get_subject: Optional[Callable[[Any], Optional[str]]] = None,
    ttl_seconds: Optional[int] = None,
    prefix: str = "/media",
) -> APIRouter:
    """``POST {prefix}/grants``: the browser asks, the app decides, the media
    library signs.

    ``client`` must be a ``MediaLibraryClient`` with the tenant's **service
    token**. ``authorize(user, request, profile)`` is the app's own rule (its
    enrollments, ownership, roles…): return ``True`` to issue exactly what
    was asked. It is called for every request; ``profile`` is the
    ``UploadProfile`` named by ``request.profile`` (looked up in
    ``profiles``), or ``None``. Check ``profile.check_permissions(...)`` there
    when the profile sets permissions.

    Responses: 201 ``{"grant", "grantId", "scopes", "expiresIn",
    "expiresAt"}``; 403 when ``authorize`` refuses; 422 for an unknown
    profile name, ``manage:*`` (never issued to browsers here), or a request
    the media library rejects; 502 when the media library cannot be reached
    or refuses the service token.

    Usage::

        def authorize(user, req, profile):
            if req.profile:
                return profile.check_permissions(user.permissions)
            return all(user.can_edit(a) for a in req.asset_ids)

        app.include_router(create_grant_router(
            client, authorize, get_current_user,
            profiles={"course-cover": COURSE_COVER},
            get_subject=lambda user: str(user.id),
        ))
    """
    router = APIRouter(prefix=prefix)
    known = dict(profiles or {})

    @router.post("/grants", status_code=201, response_model=None)
    def issue_grant(
        payload: GrantRequest,
        user: Any = Depends(get_current_user),
    ) -> Dict[str, Any]:
        if GrantScope.MANAGE_ALL in payload.scopes:
            raise HTTPException(status_code=422, detail="manage:* is not issued to browsers")
        profile = None
        if payload.profile is not None:
            profile = known.get(payload.profile)
            if profile is None:
                raise HTTPException(status_code=422, detail=f"Unknown upload profile {payload.profile!r}")
        if not authorize(user, payload, profile):
            raise HTTPException(status_code=403, detail="Not allowed")
        try:
            with _media_library_errors(passthrough=(ValidationError,)):
                grant = client.grants.issue(
                    payload.scopes,
                    asset_ids=payload.asset_ids or None,
                    session_ids=payload.session_ids or None,
                    profile=profile,
                    subject=get_subject(user) if get_subject else None,
                    ttl_seconds=ttl_seconds,
                )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return grant.to_payload()

    return router


@contextmanager
def _media_library_errors(passthrough: tuple = ()) -> Iterator[None]:
    try:
        yield
    except passthrough:
        raise
    except MediaLibraryError as exc:
        logger.warning("Media library request failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail="Media library request failed") from exc
