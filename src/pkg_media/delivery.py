from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional
from uuid import UUID

import httpx
from pydantic import BaseModel

from pkg_media._http import raise_for_status
from pkg_media.cache import AssetUrlCache, _as_uuid
from pkg_media.models import (
    AssetKind,
    AssetStatus,
    DeliveryAsset,
    DeliveryBatchResolveResult,
    PaginatedDeliveryAssets,
    ResolveStatus,
)

_THUMBNAIL_RENDITION_TYPE = "thumbnail"
_NO_RESULT_ERROR = "The media library returned no result for this asset"


class _ResolveResponse(BaseModel):
    """Body of ``POST /delivery/assets/resolve``; a body without ``items``
    fails validation instead of raising ``KeyError``."""

    items: List[DeliveryBatchResolveResult]


class DeliveryAPI:
    """Client-facing delivery endpoints (slim responses with download URLs).

    With a service token, ``resolve`` also returns links for RESTRICTED
    assets; the consuming app decides which of its users may receive them.
    """

    BASE = "/v1/delivery/assets"
    # Server limit on assetIds per POST /delivery/assets/resolve.
    RESOLVE_BATCH_SIZE = 100
    # Server limit on grants per request (X-Media-Grants).
    MAX_GRANTS = 10
    GRANTS_HEADER = "X-Media-Grants"

    def __init__(self, http: httpx.Client, *, tenant_id: Optional[str] = None):
        self._http = http
        # Scopes ``resolve(..., cache=...)`` entries (the X-Tenant sent).
        self.tenant_id = tenant_id

    def list(
        self,
        *,
        kind: Optional[AssetKind | str] = None,
        status: Optional[AssetStatus | str] = None,
        query: Optional[str] = None,
        page: int = 1,
        page_size: int = 10,
    ) -> PaginatedDeliveryAssets:
        """List assets (slim response, no download URLs)."""
        params: Dict[str, Any] = {"page": page, "pageSize": page_size}
        if kind is not None:
            params["kind"] = str(kind)
        if status is not None:
            params["status"] = str(status)
        if query is not None:
            params["query"] = query
        resp = self._http.get(self.BASE, params=params)
        raise_for_status(resp)
        return PaginatedDeliveryAssets.model_validate(resp.json())

    def get(self, asset_id: UUID | str, *, grants: Optional[Iterable[str]] = None) -> DeliveryAsset:
        """Get a single asset with its download URL and renditions.

        ``grants``: media grants (``mlg_…``) that may list the asset, as for
        ``resolve``.
        """
        resp = self._http.get(f"{self.BASE}/{asset_id}", headers=_grant_headers(grants))
        raise_for_status(resp)
        return DeliveryAsset.model_validate(resp.json())

    def resolve(
        self,
        asset_ids: Iterable[UUID | str],
        *,
        cache: Optional[AssetUrlCache] = None,
        grants: Optional[Iterable[str]] = None,
    ) -> List[DeliveryBatchResolveResult]:
        """Batch resolve download URLs, one result per id in input order.

        Any number of ids: requests are split into batches of
        ``RESOLVE_BATCH_SIZE``. An empty input returns ``[]`` without a
        request. Each item's ``status`` is a ``ResolveStatus`` value
        (``ok`` / ``not_found`` / ``forbidden`` / ``error``); see
        ``DeliveryBatchResolveResult``.

        With ``cache``, ids with a live entry for this client's tenant are
        served from it and only the others are requested (each once, even
        when repeated); cacheable results are stored. The results still come
        one per input id, duplicates included, in order; an id the server
        returned nothing for gets an ``error`` result.

        Cache entries are keyed by tenant and asset id, not by principal, and
        a hit is served without a request. Only pass ``cache`` on a client
        created with the tenant's **service token**, and never share that
        cache with user-token clients (or keep it across ``set_token`` to a
        different principal): they would get the service token's results,
        RESTRICTED links included, where the server answers ``forbidden``.

        ``grants``: read grants (``mlg_…``, at most ``MAX_GRANTS``), e.g. one
        from each service whose assets a page shows. An asset listed by any
        of them resolves as for its owning service (PRIVATE and RESTRICTED
        included); the others as for this client's own token. Sent as
        ``X-Media-Grants``; the server ignores invalid or expired grants
        instead of failing the batch. Not combinable with ``cache``.
        """
        headers = _grant_headers(grants)
        if headers and cache is not None:
            raise ValueError("resolve() takes either cache or grants, not both")
        if cache is None:
            return self._resolve([str(a) for a in asset_ids], headers)

        ids = [_as_uuid(a) for a in asset_ids]
        found = cache.get_many(ids, tenant_id=self.tenant_id)
        missing = [a for a in dict.fromkeys(ids) if a not in found]
        if missing:
            fresh = self._resolve([str(a) for a in missing])
            cache.put_many(fresh, tenant_id=self.tenant_id)
            found.update((item.asset_id, item) for item in fresh)
        return [found[a] if a in found else _no_result(a) for a in ids]

    def _resolve(self, ids: List[str], headers: Optional[Dict[str, str]] = None) -> List[DeliveryBatchResolveResult]:
        results: List[DeliveryBatchResolveResult] = []
        for start in range(0, len(ids), self.RESOLVE_BATCH_SIZE):
            payload = {"assetIds": ids[start:start + self.RESOLVE_BATCH_SIZE]}
            resp = self._http.post(f"{self.BASE}/resolve", json=payload, headers=headers or None)
            raise_for_status(resp)
            results.extend(_ResolveResponse.model_validate(resp.json()).items)
        return results


def thumbnail_url(result: DeliveryBatchResolveResult) -> Optional[str]:
    """The asset's public thumbnail link, for a locked teaser.

    The preview link of the asset's ``thumbnail`` rendition when the result
    carries the asset, else the result's ``thumbnail_url`` (which the server
    also takes from that rendition, and sends for ``forbidden`` and ``error``
    items too). Never a download or other preview link.
    """
    if result.asset is not None:
        for rendition in result.asset.renditions:
            if rendition.rendition_type == _THUMBNAIL_RENDITION_TYPE and rendition.preview_url:
                return rendition.preview_url
    return result.thumbnail_url


def _grant_headers(grants: Optional[Iterable[str]]) -> Dict[str, str]:
    """``X-Media-Grants`` for ``grants`` (``MediaGrant`` objects or ``mlg_…`` strings)."""
    if grants is None:
        return {}
    if isinstance(grants, str):
        grants = [grants]
    tokens = [getattr(g, "grant", g) for g in grants]
    tokens = [t for t in dict.fromkeys(tokens) if t]
    if len(tokens) > DeliveryAPI.MAX_GRANTS:
        raise ValueError(f"at most {DeliveryAPI.MAX_GRANTS} grants per request")
    return {DeliveryAPI.GRANTS_HEADER: ", ".join(tokens)} if tokens else {}


def _no_result(asset_id: UUID) -> DeliveryBatchResolveResult:
    return DeliveryBatchResolveResult(
        asset_id=asset_id,
        status=ResolveStatus.ERROR,
        error=_NO_RESULT_ERROR,
    )
