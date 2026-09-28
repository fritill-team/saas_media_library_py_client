from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional
from uuid import UUID

import httpx

from saas_media_library._http import raise_for_status
from saas_media_library.models import (
    AssetKind,
    AssetStatus,
    DeliveryAsset,
    DeliveryBatchResolveResult,
    PaginatedDeliveryAssets,
)


class DeliveryAPI:
    """Client-facing delivery endpoints (slim responses with download URLs).

    With a service token, ``resolve`` also returns links for RESTRICTED
    assets; the consuming app decides which of its users may receive them.
    """

    BASE = "/v1/delivery/assets"
    # Server limit on assetIds per POST /delivery/assets/resolve.
    RESOLVE_BATCH_SIZE = 100

    def __init__(self, http: httpx.Client):
        self._http = http

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

    def get(self, asset_id: UUID | str) -> DeliveryAsset:
        """Get a single asset with its download URL and renditions."""
        resp = self._http.get(f"{self.BASE}/{asset_id}")
        raise_for_status(resp)
        return DeliveryAsset.model_validate(resp.json())

    def resolve(self, asset_ids: Iterable[UUID | str]) -> List[DeliveryBatchResolveResult]:
        """Batch resolve download URLs, one result per id in input order.

        Any number of ids: requests are split into batches of
        ``RESOLVE_BATCH_SIZE``. An empty input returns ``[]`` without a
        request. Each item's ``status`` is a ``ResolveStatus`` value
        (``ok`` / ``not_found`` / ``forbidden`` / ``error``); see
        ``DeliveryBatchResolveResult``.
        """
        ids = [str(a) for a in asset_ids]
        results: List[DeliveryBatchResolveResult] = []
        for start in range(0, len(ids), self.RESOLVE_BATCH_SIZE):
            payload = {"assetIds": ids[start:start + self.RESOLVE_BATCH_SIZE]}
            resp = self._http.post(f"{self.BASE}/resolve", json=payload)
            raise_for_status(resp)
            results.extend(
                DeliveryBatchResolveResult.model_validate(i) for i in resp.json()["items"]
            )
        return results
