"""FastAPI routes that serve media links through an ``AccessGate``, issue
media grants for the app's browser users, and start their direct uploads.

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
import httpx
from pydantic import BaseModel, Field

from saas_media_library.exceptions import MediaLibraryError, NotFoundError, UploadRejected, ValidationError
from saas_media_library.gate import AccessGate
from saas_media_library.grants import GrantScope
from saas_media_library.profiles import UploadProfile
from saas_media_library.models import ResolveStatus
from saas_media_library.service_uploads import ClaimedUpload, UploadRecord, UploadSessionStore, claim_upload

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


class StartUploadRequest(BaseModel):
    """Body of ``POST {prefix}``: the file the browser is about to upload.

    ``profile`` names one of the app's upload profiles (optional when the
    router has only one). ``context`` is the app's own data about the upload
    (e.g. ``{"courseId": "…"}``), passed to ``can_start`` and kept in the
    ``UploadRecord``."""

    mime_type: str = Field(..., alias="mimeType", min_length=1, max_length=255)
    size_bytes: int = Field(..., alias="sizeBytes", ge=0)
    filename: Optional[str] = Field(None, max_length=255)
    profile: Optional[str] = Field(None, max_length=255)
    context: Dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class AttachUploadRequest(BaseModel):
    """Body of ``POST {prefix}/{session_id}/attach``: the asset the
    session's finalize returned. ``profile``, when sent, must be the one the
    session was started with."""

    asset_id: UUID = Field(..., alias="assetId")
    profile: Optional[str] = Field(None, max_length=255)

    model_config = {"populate_by_name": True}


_REJECTED_STATUS = {UploadRejected.MIME_TYPE: 415, UploadRejected.TOO_LARGE: 413}
_ATTACH_REJECTED_STATUS = {
    UploadRejected.UNKNOWN_SESSION: 404,
    UploadRejected.CONTEXT_MISMATCH: 403,
    UploadRejected.PROFILE_MISMATCH: 409,
    UploadRejected.ALREADY_ATTACHED: 409,
}


def create_upload_router(
    client: Any,
    can_start: Callable[[Any, StartUploadRequest, UploadProfile], bool],
    get_current_user: Callable[..., Any],
    *,
    profiles: Mapping[str, UploadProfile],
    store: UploadSessionStore,
    get_owner: Callable[[Any], str],
    upload_base_url: Optional[str] = None,
    ttl_seconds: Optional[int] = None,
    prefix: str = "/uploads",
    on_attach: Optional[Callable[[Any, ClaimedUpload], Optional[Mapping[str, Any]]]] = None,
    check_attach: Optional[Callable[[Any, UploadRecord], bool]] = None,
) -> APIRouter:
    """Start browser uploads that go directly to the media library.

    - ``POST {prefix}`` with ``{"mimeType", "sizeBytes", "filename",
      "profile", "context"}``: checks the file against the profile, asks
      ``can_start(user, request, profile)`` (the app's own rule), creates
      the upload session with the profile, saves an ``UploadRecord`` in
      ``store`` and issues an ``upload`` grant for that one session.
      201 ``{"sessionId", "grant", "grantId", "expiresIn", "expiresAt",
      "tenant", "partSizeBytes"}``, plus ``"uploadBaseUrl"`` only when
      ``upload_base_url`` is set.
    - ``POST {prefix}/{session_id}/grant``: a new grant for a session the
      user started and has not attached yet (the browser refreshes before
      ``expiresAt``). 200 with ``{"sessionId", "grant", "grantId",
      "expiresIn", "expiresAt"}``; 404 for an unknown session or another
      user's; 409 once attached.
    - ``POST {prefix}/{session_id}/attach`` with ``{"assetId", "profile"}``
      (only when ``on_attach`` is given): ``claim_upload`` with the
      session's profile, then ``on_attach(user, claimed)``, where the app
      stores ``claimed.asset.id`` (``claimed.record.context`` says where).
      200 ``{"sessionId", "assetId"}`` plus whatever mapping ``on_attach``
      returns. 404 for an unknown session or another user's (or one the
      media library does not have); 403 when ``check_attach(user, record)``
      refuses it; 409 once attached, or for another ``profile``; 422
      ``{"detail", "reason"}`` when the upload fails the check (not
      finalized, another session's asset, visibility…).

    The browser sends ``Authorization: Bearer <grant>`` and ``X-Tenant:
    <tenant>`` to ``{upload base}/manage/uploads/sessions/{sessionId}/…``,
    where the upload base is the frontend's choice (the Nuxt module's
    ``mediaLibrary.serviceUploads``: the public media library, or its
    same-origin pass-through route),
    then posts ``{assetId}`` to ``{prefix}/{session_id}/attach`` (with
    ``on_attach``), or ``{sessionId, assetId}`` to an attach endpoint of the
    app's own that calls ``service_uploads.claim_upload``.

    ``client`` must be a ``MediaLibraryClient`` with the tenant's **service
    token**. ``profiles`` maps the names the browser may send to profiles;
    each profile's ``name`` must be its key (``claim_upload`` compares it).
    ``get_owner(user)`` is the app's user reference, stored in the record
    and sent as the grant's subject. ``upload_base_url`` is optional and
    only for frontends that do not choose an upload base themselves: the
    public address browsers use, with ``/v1``. Leave it unset otherwise.
    The client's own ``base_url`` is a server-side address and is never
    sent to browsers. ``ttl_seconds`` is the grants' lifetime (server
    default 600, at most its ``GRANT_MAX_TTL``).

    ``on_attach`` runs after ``store.mark_attached``: with a database store,
    run both in the request's transaction so a failed ``on_attach`` leaves
    the session unattached. ``check_attach(user, record)`` runs before
    anything is attached, e.g. that ``record.context["courseId"]`` is a
    course the user may still edit.

    Other responses: 403 when ``can_start`` refuses; 413 / 415 / 422 when
    the profile rejects the file (``{"detail", "reason"}``, ``reason`` as
    ``UploadRejected``); 422 for an unknown or missing profile name; 502
    when the media library cannot be reached or refuses the service token.

    Usage::

        store = MyDbUploadStore()  # an UploadSessionStore

        def can_start(user, req, profile):
            return user.can_edit_course(req.context.get("courseId"))

        app.include_router(create_upload_router(
            client, can_start, get_current_user,
            profiles={"course-cover": COURSE_COVER},
            store=store,
            get_owner=lambda user: str(user.id),
            prefix="/courses/uploads",
            on_attach=lambda user, claimed: set_course_cover(
                claimed.record.context["courseId"], claimed.asset.id),
        ))
    """
    known = dict(profiles)
    if not known:
        raise ValueError("create_upload_router() needs at least one upload profile")
    for key, profile in known.items():
        if profile.name != key:
            raise ValueError(f"upload profile {key!r} must have name={key!r} (has {profile.name!r})")
    # Never the client's base_url: that is the server-side address.
    base_url = upload_base_url.rstrip("/") if upload_base_url else None
    router = APIRouter(prefix=prefix)

    def issue(session_id: Any, user: Any) -> Dict[str, Any]:
        with _upload_errors():
            grant = client.grants.issue(
                [GrantScope.UPLOAD],
                session_ids=[session_id],
                subject=get_owner(user),
                ttl_seconds=ttl_seconds,
            )
        return {"sessionId": str(session_id), **grant.to_payload()}

    @router.post("", status_code=201, response_model=None)
    def start_upload(
        payload: StartUploadRequest,
        user: Any = Depends(get_current_user),
    ) -> Any:
        if payload.profile is None:
            if len(known) != 1:
                raise HTTPException(status_code=422, detail="profile is required")
            profile = next(iter(known.values()))
        else:
            profile = known.get(payload.profile)
            if profile is None:
                raise HTTPException(status_code=422, detail=f"Unknown upload profile {payload.profile!r}")
        try:
            profile.validate_file(payload.mime_type, payload.size_bytes)
        except UploadRejected as exc:
            return JSONResponse(
                status_code=_REJECTED_STATUS.get(exc.reason, 422),
                content={"detail": str(exc), "reason": exc.reason},
            )
        if not can_start(user, payload, profile):
            raise HTTPException(status_code=403, detail="Not allowed")

        with _upload_errors():
            session = client.uploads.create_session(
                mime_type=payload.mime_type,
                profile=profile,
                original_filename=payload.filename,
                expected_size_bytes=payload.size_bytes,
                validate=False,
            )
        try:
            store.save(UploadRecord(
                session_id=session.id,
                owner=get_owner(user),
                profile=profile.name,
                context=dict(payload.context),
            ))
            body = issue(session.id, user)
        except Exception:
            _abort_quietly(client, session.id)
            raise
        started = {
            **body,
            "tenant": client.tenant_id,
            "partSizeBytes": session.part_size_bytes,
        }
        if base_url:
            started["uploadBaseUrl"] = base_url
        return started

    @router.post("/{session_id}/grant", response_model=None)
    def refresh_grant(
        session_id: UUID,
        user: Any = Depends(get_current_user),
    ) -> Dict[str, Any]:
        record = store.get(session_id)
        if record is None or record.owner != get_owner(user):
            raise HTTPException(status_code=404, detail=f"Upload session {session_id} not found")
        if record.attached_asset_id is not None:
            raise HTTPException(status_code=409, detail="Upload session is already attached")
        return issue(session_id, user)

    if on_attach is None:
        return router

    @router.post("/{session_id}/attach", response_model=None)
    def attach_upload(
        session_id: UUID,
        payload: AttachUploadRequest,
        user: Any = Depends(get_current_user),
    ) -> Any:
        owner = get_owner(user)
        record = store.get(session_id)
        if record is None or record.owner != owner:
            raise HTTPException(status_code=404, detail=f"Upload session {session_id} not found")
        profile = known.get(payload.profile or record.profile)
        if profile is None:
            raise HTTPException(status_code=422, detail=f"Unknown upload profile {payload.profile!r}")
        check = (lambda r: check_attach(user, r)) if check_attach is not None else None
        with _upload_errors():
            try:
                claimed = claim_upload(
                    client, store, session_id, payload.asset_id,
                    owner=owner, profile=profile, check_record=check,
                )
            except UploadRejected as exc:
                return JSONResponse(
                    status_code=_ATTACH_REJECTED_STATUS.get(exc.reason, 422),
                    content={"detail": str(exc), "reason": exc.reason},
                )
            except NotFoundError as exc:
                raise HTTPException(status_code=404, detail="Upload session or asset not found") from exc
        extra = on_attach(user, claimed) or {}
        return {**extra, "sessionId": str(session_id), "assetId": str(claimed.asset.id)}

    return router


@contextmanager
def _upload_errors() -> Iterator[None]:
    # UploadsAPI and GrantsAPI let httpx's transport errors through (only
    # AccessGate wraps them); a browser should see 502 for those too.
    try:
        with _media_library_errors():
            yield
    except httpx.HTTPError as exc:
        logger.warning("Media library request failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail="Media library request failed") from exc


def _abort_quietly(client: Any, session_id: Any) -> None:
    try:
        client.uploads.abort(session_id)
    except Exception as exc:
        logger.warning("Could not abort upload session %s: %s", session_id, exc)


@contextmanager
def _media_library_errors(passthrough: tuple = ()) -> Iterator[None]:
    try:
        yield
    except passthrough:
        raise
    except MediaLibraryError as exc:
        logger.warning("Media library request failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail="Media library request failed") from exc
