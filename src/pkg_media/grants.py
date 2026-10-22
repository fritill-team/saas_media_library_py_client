"""Media grants: short-lived, scoped tokens for the app's browser users.

The app decides what one of its users may do (its own rules), then asks the
media library for a grant with its **service token** and hands the grant to
the browser. The browser calls the media library's ``/manage`` endpoints with
``Authorization: Bearer mlg_…``; the media library enforces the grant. The
app never re-implements those endpoints. See the server's
docs/security/media-grants.md.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Union
from uuid import UUID

from pydantic import BaseModel, Field

from pkg_media.profiles import ANY_MIME_TYPE, UploadProfile, processing_config_payload


class GrantScope(str, Enum):
    """What a grant allows. ``MANAGE_ALL`` is every ``/manage`` endpoint of
    the tenant (like a service token, except issuing grants)."""

    UPLOAD = "upload"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    PROCESS = "process"
    RENDITIONS = "renditions"
    MANAGE_ALL = "manage:*"

    def __str__(self) -> str:
        return self.value


class MediaGrant(BaseModel):
    """A grant as issued. Give ``grant`` (and ``expires_at``) to the browser;
    it sends ``Authorization: Bearer <grant>``."""

    grant: str
    grant_id: UUID = Field(alias="grantId")
    scopes: List[str]
    expires_in: int = Field(alias="expiresIn")
    expires_at: datetime = Field(alias="expiresAt")

    model_config = {"populate_by_name": True}

    def to_payload(self) -> Dict[str, Any]:
        """The camelCase JSON body to return to the browser."""
        return self.model_dump(mode="json", by_alias=True)


def grant_profile_payload(profile: Union[UploadProfile, Mapping[str, Any]]) -> Dict[str, Any]:
    """The server-side grant profile for an ``UploadProfile``.

    The media library applies it to every upload made with the grant: kind,
    visibility, processing config and collection come from it; the declared
    MIME type and ``expectedSizeBytes`` must fit it. Differences from
    ``UploadProfile.validate_file``:

    - on the server ``"video/*"`` matches every ``video/…`` type (the client
      compares it literally);
    - ``min_size_bytes`` is not sent (the server has no minimum);
    - ``required_permissions`` / ``allow_anonymous`` are the app's own check,
      made before it issues the grant (``UploadProfile.check_permissions``).

    A mapping is passed through as the server's camelCase profile.
    """
    if not isinstance(profile, UploadProfile):
        return dict(profile)
    payload: Dict[str, Any] = {"kind": str(profile.kind), "visibility": str(profile.visibility)}
    if profile.allowed_mime_types is not None and ANY_MIME_TYPE not in profile.allowed_mime_types:
        if not profile.allowed_mime_types:
            raise ValueError(f"upload profile {profile.name or profile.kind} allows no MIME type")
        payload["allowedMimeTypes"] = sorted(profile.allowed_mime_types)
    if profile.max_size_bytes is not None:
        if profile.max_size_bytes == 0:
            raise ValueError(f"upload profile {profile.name or profile.kind} allows no bytes")
        payload["maxSizeBytes"] = profile.max_size_bytes
    if profile.processing_config is not None:
        payload["processingConfig"] = processing_config_payload(profile.processing_config)
    return payload


class GrantsAPI:
    """Issue media grants (HTTP ``POST /manage/grants``, gRPC
    ``itqadem.media.v1.Grants``). Needs a service caller."""

    BASE = "/v1/manage/grants"

    def __init__(self, transport: Any):
        self._transport = transport

    async def read(
        self,
        asset_ids: Iterable[Union[UUID, str]],
        *,
        subject: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
    ) -> MediaGrant:
        """A read grant for assets your app returns to one of its users.

        A backend passes it to ``client.delivery.resolve(ids,
        grants=[...])`` (gRPC ``x-media-grants`` metadata) to resolve
        another service's assets; each asset resolves only if a grant lists
        it. Only list assets your own rules allow this user to see.
        """
        ids = list(asset_ids)
        if not ids:
            raise ValueError("read() needs at least one asset id")
        return await self.issue([GrantScope.READ], asset_ids=ids, subject=subject, ttl_seconds=ttl_seconds)

    async def issue(
        self,
        scopes: Iterable[Union[GrantScope, str]],
        *,
        asset_ids: Optional[Iterable[Union[UUID, str]]] = None,
        profile: Optional[Union[UploadProfile, Mapping[str, Any]]] = None,
        session_ids: Optional[Iterable[Union[UUID, str]]] = None,
        subject: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
        collection_name: Optional[str] = None,
    ) -> MediaGrant:
        """Issue a grant for one of the app's users.

        - ``read`` / ``update`` / ``delete`` / ``process`` / ``renditions``
          need ``asset_ids`` (the assets the user may act on).
        - ``upload`` needs ``profile`` (to create sessions) or
          ``session_ids`` (to continue sessions).
        - ``subject``: the app's user reference, logged by the server.
        - ``ttl_seconds``: default 600 on the server, at most its
          ``GRANT_MAX_TTL`` (3600 by default).

        Only call this after the app's own check. Keep grants short and
        scoped to what the user has on screen: anyone holding one may use it
        until it expires.

        Raises:
            ValidationError: the server rejected the request (422).
            AuthenticationError: not a service token (401/403).
            ServerError: e.g. 503 when the server has no ``GRANT_SIGNING_SECRET``.
        """
        payload: Dict[str, Any] = {"scopes": [str(s) for s in scopes]}
        if asset_ids is not None:
            payload["assetIds"] = [str(a) for a in asset_ids]
        if session_ids is not None:
            payload["sessionIds"] = [str(s) for s in session_ids]
        if profile is not None:
            payload["profile"] = grant_profile_payload(profile)
            if collection_name is not None:
                payload["profile"]["collectionName"] = collection_name
        if subject is not None:
            payload["subject"] = subject
        if ttl_seconds is not None:
            payload["ttlSeconds"] = ttl_seconds
        return await self._transport.issue_grant(payload)
