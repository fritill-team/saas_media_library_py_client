"""Access decisions for RESTRICTED assets, made by the consuming app.

Replaces per-user access grants (being retired, see the server's
docs/asset-access/retirement.md): the app decides who may open an asset from
its own data, then hands out links the media library resolved for its service
token. Files are still served by the media library; the app never proxies
bytes.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple, Union
from uuid import UUID

import httpx
import pydantic

from pkg_media.cache import AssetUrlCache, _as_uuid
from pkg_media.delivery import _NO_RESULT_ERROR, thumbnail_url
from pkg_media.exceptions import MediaLibraryError
from pkg_media.models import (
    DeliveryBatchResolveResult,
    ResolveStatus,
)

logger = logging.getLogger(__name__)

# Transport errors (unreachable, timeouts) and bodies that are not the
# expected JSON, raised by the service client's resolve.
_TRANSPORT_OR_DECODE_ERRORS = (httpx.HTTPError, pydantic.ValidationError, json.JSONDecodeError)

CanAccess = Callable[
    [Any, List[UUID]],
    Union[Iterable[UUID | str], Mapping[UUID | str, Any]],
]


class AccessGate:
    """Resolve delivery links for a user after the app's own access check.

    ``can_access(user, asset_ids)`` gets every requested id at once and
    returns the ones ``user`` may open, so the app can answer with a single
    query (enrollments, purchases, roles...). It may also return a mapping
    ``{asset_id: allowed}``; only ids with a truthy value are allowed::

        def can_access(user, asset_ids):
            return db.scalars(
                select(Lesson.asset_id)
                .join(Enrollment, Enrollment.course_id == Lesson.course_id)
                .where(Enrollment.user_id == user.id, Lesson.asset_id.in_(asset_ids))
            )

        gate = AccessGate(service_client, can_access, cache=AssetUrlCache())
        results = gate.resolve(current_user, asset_ids)

    ``client`` is a sync ``MediaClient`` (HTTP or gRPC transport) calling as
    the app's **service** (Keycloak client credentials or a service token):
    only a service gets links for RESTRICTED assets. Otherwise the media
    library answers ``forbidden`` for them (a warning is logged).

    Every id is resolved as the service (through ``cache``, when given,
    scoped to the client's ``tenant_id``), including the ones the app
    denies, which only get the asset's public thumbnail (``thumbnail_url``).
    Results come back one per requested id, in order:

    - allowed: the media library's result (``ok``, ``not_found``, ``error``).
    - denied: ``forbidden`` with ``asset=None`` and at most a
      ``thumbnail_url``; never a download or other preview link. An asset
      that does not exist is still ``not_found``, as on the server, unless
      ``hide_missing`` is set: then every denied id is ``forbidden``, so a
      user cannot tell which ids exist.

    ``resolve_denied=False`` sends only the allowed ids to the media
    library: denied ids come back ``forbidden`` without a thumbnail.

    ``to_payload`` turns the results into the exact JSON of
    ``POST /delivery/assets/resolve``, so a frontend only changes its base
    URL. The gate is thread-safe when ``can_access`` is.

    Every failure of the media library call raises ``MediaLibraryError``:
    error responses as their usual subclass, transport errors and malformed
    responses as ``MediaLibraryError`` with the original as ``__cause__``.
    Exceptions raised by ``can_access`` propagate unchanged, whatever their
    type, so callers can tell the two apart.
    """

    DENIED_ERROR = "Access denied"

    def __init__(
        self,
        client: Any,
        can_access: CanAccess,
        *,
        cache: Optional[AssetUrlCache] = None,
        hide_missing: bool = False,
        resolve_denied: bool = True,
    ):
        self._client = client
        self._can_access = can_access
        self.cache = cache
        self.hide_missing = hide_missing or not resolve_denied
        self.resolve_denied = resolve_denied

    def resolve(
        self,
        user: Any,
        asset_ids: Iterable[UUID | str],
    ) -> List[DeliveryBatchResolveResult]:
        """One result per id in ``asset_ids`` (duplicates included), in order.

        ``user`` is passed to ``can_access`` untouched. An empty input returns
        ``[]`` without calling ``can_access`` or the media library.
        """
        return self._resolve_detailed(user, asset_ids)[0]

    def _resolve_detailed(
        self,
        user: Any,
        asset_ids: Iterable[UUID | str],
    ) -> Tuple[List[DeliveryBatchResolveResult], Dict[UUID, DeliveryBatchResolveResult]]:
        """``resolve``, plus the service's own result per resolved id.

        For ``MediaEmbedder``, which needs the expiry of a denied id's
        thumbnail; the service results must never reach a denied user.
        """
        ids = [_as_uuid(a) for a in asset_ids]
        if not ids:
            return [], {}
        unique = list(dict.fromkeys(ids))

        allowed = self._allowed_ids(user, unique)
        to_resolve = unique if self.resolve_denied else [a for a in unique if a in allowed]
        resolved = self._resolve_with_service(to_resolve) if to_resolve else {}

        by_id: Dict[UUID, DeliveryBatchResolveResult] = {}
        for asset_id in unique:
            item = resolved.get(asset_id)
            if asset_id not in allowed:
                # Built from scratch: nothing of the service result but the
                # thumbnail reaches a denied user.
                if item is not None and item.status == ResolveStatus.NOT_FOUND and not self.hide_missing:
                    by_id[asset_id] = DeliveryBatchResolveResult(
                        asset_id=asset_id, status=ResolveStatus.NOT_FOUND
                    )
                else:
                    thumbnail = thumbnail_url(item) if item is not None else None
                    by_id[asset_id] = _forbidden(asset_id, thumbnail, self.DENIED_ERROR)
            elif item.status == ResolveStatus.FORBIDDEN:
                logger.warning(
                    "Media library denied asset %s to the gate's client; "
                    "AccessGate needs a client calling as the app's service (client credentials or a service token)",
                    asset_id,
                )
                by_id[asset_id] = _forbidden(asset_id, item.thumbnail_url, item.error)
            else:
                by_id[asset_id] = item
        return [by_id[asset_id] for asset_id in ids], resolved

    def resolve_one(self, user: Any, asset_id: UUID | str) -> DeliveryBatchResolveResult:
        """``resolve`` for a single id."""
        return self.resolve(user, [asset_id])[0]

    @staticmethod
    def to_payload(results: Iterable[DeliveryBatchResolveResult]) -> Dict[str, Any]:
        """JSON body of ``POST /delivery/assets/resolve`` for ``results``.

        ``{"items": [{"assetId", "status", "thumbnailUrl", "asset", "error"}],
        "total"}`` with the server's camelCase keys, nulls included.
        """
        items = [r.model_dump(mode="json", by_alias=True) for r in results]
        return {"items": items, "total": len(items)}

    # ── Private helpers ────────────────────────────────────────────────

    def _allowed_ids(self, user: Any, asset_ids: List[UUID]) -> Set[UUID]:
        allowed = self._can_access(user, list(asset_ids))
        if allowed is None or isinstance(allowed, (bool, str, bytes)):
            raise TypeError(
                "can_access must return an iterable of allowed asset ids, "
                f"got {type(allowed).__name__}"
            )
        if isinstance(allowed, Mapping):
            # {id: allowed}: iterating would yield every key, allowed or not.
            return {_as_uuid(a) for a, ok in allowed.items() if ok}
        return {_as_uuid(a) for a in allowed}

    def _resolve_with_service(self, asset_ids: List[UUID]) -> Dict[UUID, DeliveryBatchResolveResult]:
        try:
            if self.cache is not None:
                # Cache entries are scoped to the client's tenant.
                fresh = self._client.delivery.resolve(asset_ids, cache=self.cache)
            else:
                fresh = self._client.delivery.resolve(asset_ids)
        except _TRANSPORT_OR_DECODE_ERRORS as exc:
            raise MediaLibraryError(
                f"Media library resolve failed: {type(exc).__name__}: {exc}"
            ) from exc
        found = {item.asset_id: item for item in fresh}
        for asset_id in asset_ids:
            if asset_id not in found:
                found[asset_id] = DeliveryBatchResolveResult(
                    asset_id=asset_id,
                    status=ResolveStatus.ERROR,
                    error=_NO_RESULT_ERROR,
                )
        return found


def _forbidden(
    asset_id: UUID,
    thumbnail_url: Optional[str],
    error: Optional[str],
) -> DeliveryBatchResolveResult:
    return DeliveryBatchResolveResult(
        asset_id=asset_id,
        status=ResolveStatus.FORBIDDEN,
        thumbnail_url=thumbnail_url,
        asset=None,
        error=error,
    )
