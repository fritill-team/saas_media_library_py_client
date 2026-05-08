from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

import httpx

from saas_media_library._http import raise_for_status
from saas_media_library.models import (
    Asset,
    AssetKind,
    AssetStatus,
    BatchResolveResult,
    DeliveryAsset,
    DeliveryBatchResolveResult,
    PaginatedAssets,
    PaginatedDeliveryAssets,
    Visibility,
)


class AssetsAPI:
    """Access and manage assets.

    Provides both manage (admin) and delivery (public) endpoints.
    """

    MANAGE_BASE = "/v1/manage/assets"
    DELIVERY_BASE = "/v1/delivery/assets"

    def __init__(self, http: httpx.Client):
        self._http = http

    # ── Manage endpoints (admin) ───────────────────────────────────────

    def list(
        self,
        *,
        kind: Optional[AssetKind | str] = None,
        status: Optional[AssetStatus | str] = None,
        query: Optional[str] = None,
        page: int = 1,
        page_size: int = 10,
    ) -> PaginatedAssets:
        """List assets with full details (admin)."""
        params = self._build_list_params(kind, status, query, page, page_size)
        resp = self._http.get(self.MANAGE_BASE, params=params)
        raise_for_status(resp)
        return PaginatedAssets.model_validate(resp.json())

    def get(self, asset_id: UUID | str) -> Asset:
        """Get a single asset with resolved download URL (admin)."""
        resp = self._http.get(f"{self.MANAGE_BASE}/{asset_id}")
        raise_for_status(resp)
        return Asset.model_validate(resp.json())

    def resolve(self, asset_ids: List[UUID | str]) -> List[BatchResolveResult]:
        """Batch resolve download URLs for up to 100 assets (admin)."""
        payload = {"assetIds": [str(a) for a in asset_ids]}
        resp = self._http.post(f"{self.MANAGE_BASE}/resolve", json=payload)
        raise_for_status(resp)
        return [BatchResolveResult.model_validate(i) for i in resp.json()["items"]]

    def delete(self, asset_id: UUID | str) -> None:
        """Delete an asset and all related records."""
        resp = self._http.delete(f"{self.MANAGE_BASE}/{asset_id}")
        raise_for_status(resp)

    def update_visibility(
        self,
        asset_id: UUID | str,
        visibility: Visibility | str,
    ) -> Asset:
        """Change an asset's visibility tier.

        v1 supports PRIVATE <-> RESTRICTED only. PUBLIC transitions raise
        ``ValidationError``.
        """
        resp = self._http.patch(
            f"{self.MANAGE_BASE}/{asset_id}",
            json={"visibility": str(visibility)},
        )
        raise_for_status(resp)
        return Asset.model_validate(resp.json())

    # ── Delivery endpoints (public / client-facing) ────────────────────

    def delivery_list(
        self,
        *,
        kind: Optional[AssetKind | str] = None,
        status: Optional[AssetStatus | str] = None,
        query: Optional[str] = None,
        page: int = 1,
        page_size: int = 10,
    ) -> PaginatedDeliveryAssets:
        """List assets with slim response (delivery)."""
        params = self._build_list_params(kind, status, query, page, page_size)
        resp = self._http.get(self.DELIVERY_BASE, params=params)
        raise_for_status(resp)
        return PaginatedDeliveryAssets.model_validate(resp.json())

    def delivery_get(self, asset_id: UUID | str) -> DeliveryAsset:
        """Get a single asset with resolved download URL (delivery)."""
        resp = self._http.get(f"{self.DELIVERY_BASE}/{asset_id}")
        raise_for_status(resp)
        return DeliveryAsset.model_validate(resp.json())

    def delivery_resolve(self, asset_ids: List[UUID | str]) -> List[DeliveryBatchResolveResult]:
        """Batch resolve download URLs (delivery, slim response)."""
        payload = {"assetIds": [str(a) for a in asset_ids]}
        resp = self._http.post(f"{self.DELIVERY_BASE}/resolve", json=payload)
        raise_for_status(resp)
        return [DeliveryBatchResolveResult.model_validate(i) for i in resp.json()["items"]]

    # ── Private helpers ────────────────────────────────────────────────

    @staticmethod
    def _build_list_params(
        kind: Optional[AssetKind | str],
        status: Optional[AssetStatus | str],
        query: Optional[str],
        page: int,
        page_size: int,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page": page, "pageSize": page_size}
        if kind is not None:
            params["kind"] = str(kind)
        if status is not None:
            params["status"] = str(status)
        if query is not None:
            params["query"] = query
        return params
