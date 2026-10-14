from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

import httpx

from saas_media_library._http import raise_for_status
from saas_media_library.delivery import DeliveryAPI
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
    WebAppDeployList,
)


class BulkVisibilityResult:
    """Result of a bulk visibility update operation."""

    def __init__(self, items: List[Dict[str, Any]], updated: int, failed: int):
        self.items = items
        self.updated = updated
        self.failed = failed

    def __repr__(self) -> str:
        return f"BulkVisibilityResult(updated={self.updated}, failed={self.failed})"


class AssetsAPI:
    """Access and manage assets.

    Provides both manage (admin) and delivery (public) endpoints.
    """

    MANAGE_BASE = "/v1/manage/assets"
    DELIVERY_BASE = "/v1/delivery/assets"

    def __init__(self, http: httpx.Client):
        self._http = http
        self._delivery = DeliveryAPI(http)

    # ── Manage endpoints (admin) ───────────────────────────────────────

    def list(
        self,
        *,
        kind: Optional[AssetKind | str] = None,
        status: Optional[AssetStatus | str] = None,
        query: Optional[str] = None,
        resource_policy_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 10,
    ) -> PaginatedAssets:
        """List assets with full details (admin)."""
        params = self._build_list_params(kind, status, query, page, page_size, resource_policy_id=resource_policy_id)
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

        Transitions to or from PUBLIC also move the asset's S3-stored files
        between the private and public buckets; if that fails the server
        answers 400 (``MediaLibraryError``).

        Once the server turns on ``RESTRICTED_ASSETS_SERVICE_ONLY`` or
        ``MANAGE_RESTRICTED_PREVIEWS_PRIVILEGED_ONLY``, only a privileged
        caller (a service token, or a trusted user with a privileged role)
        may move a RESTRICTED asset to another visibility; anyone else gets
        403 (``AuthenticationError``).
        """
        resp = self._http.patch(
            f"{self.MANAGE_BASE}/{asset_id}",
            json={"visibility": str(visibility)},
        )
        raise_for_status(resp)
        return Asset.model_validate(resp.json())

    def bulk_update_visibility(
        self,
        asset_ids: List[UUID | str],
        visibility: Visibility | str,
    ) -> BulkVisibilityResult:
        """Change visibility for multiple assets in a single call.

        POST /v1/manage/assets/bulk-visibility

        Assets the change fails for are counted in ``failed``: a failed move
        between buckets, or (with the server's RESTRICTED switches on) a
        RESTRICTED asset changed by a caller who is not privileged, whose
        item carries the error ``forbidden``. See ``update_visibility``.
        """
        payload = {
            "assetIds": [str(a) for a in asset_ids],
            "visibility": str(visibility),
        }
        resp = self._http.post(f"{self.MANAGE_BASE}/bulk-visibility", json=payload)
        raise_for_status(resp)
        data = resp.json()
        return BulkVisibilityResult(
            items=data["items"], updated=data["updated"], failed=data["failed"]
        )

    # ── Web-app deploys (rollback) ─────────────────────────────────────

    def list_deploys(
        self,
        asset_id: UUID | str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> WebAppDeployList:
        """A web app's deploys, newest first (``deleted`` and ``failed`` too).

        GET /v1/manage/assets/{asset_id}/deploys

        ``limit`` is 1 to 200 (the server answers 422 beyond);
        ``next_offset`` is None on the last page. Needs the ``update`` scope
        on the asset (service token, trusted admin, or a grant listing it).

        Raises:
            NotFoundError: ``reason`` is ``asset_not_found`` (unknown or
                deleted asset) or ``not_a_web_app``.
        """
        resp = self._http.get(
            f"{self.MANAGE_BASE}/{asset_id}/deploys",
            params={"limit": limit, "offset": offset},
        )
        raise_for_status(resp)
        return WebAppDeployList.model_validate(resp.json())

    def activate_deploy(
        self,
        asset_id: UUID | str,
        deploy_id: UUID | str,
    ) -> WebAppDeployList:
        """Make a retained deploy the live one (roll back a web app).

        POST /v1/manage/assets/{asset_id}/deploys/{deploy_id}/activate

        Returns the first page of the deploy list. Nothing is deleted.
        Activating the live deploy is a no-op. The next page load serves the
        activated deploy; open pages keep theirs until reloaded. A later
        reprocess of the asset builds from its newest upload and replaces
        the rollback. Needs the ``update`` scope on the asset.

        Raises:
            NotFoundError: ``reason`` is ``asset_not_found``,
                ``not_a_web_app`` or ``deploy_not_found`` (another asset's
                deploy).
            ConflictError: ``reason`` is ``deploy_deleted`` (retired by
                retention), ``deploy_failed``, ``deploy_uploading``,
                ``scan_not_allowed`` (its ZIP is not clean) or
                ``deploy_changed`` (a concurrent deploy; retry).
        """
        resp = self._http.post(
            f"{self.MANAGE_BASE}/{asset_id}/deploys/{deploy_id}/activate"
        )
        raise_for_status(resp)
        return WebAppDeployList.model_validate(resp.json())

    # ── Delivery endpoints (public / client-facing) ────────────────────
    # Kept for compatibility; ``client.delivery`` (DeliveryAPI) is the home
    # of these calls.

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
        return self._delivery.list(
            kind=kind, status=status, query=query, page=page, page_size=page_size
        )

    def delivery_get(self, asset_id: UUID | str) -> DeliveryAsset:
        """Get a single asset with resolved download URL (delivery)."""
        return self._delivery.get(asset_id)

    def delivery_resolve(self, asset_ids: List[UUID | str]) -> List[DeliveryBatchResolveResult]:
        """Batch resolve download URLs (delivery, slim response).

        Same as ``client.delivery.resolve``: any number of ids, in batches.
        """
        return self._delivery.resolve(asset_ids)

    # ── Private helpers ────────────────────────────────────────────────

    @staticmethod
    def _build_list_params(
        kind: Optional[AssetKind | str],
        status: Optional[AssetStatus | str],
        query: Optional[str],
        page: int,
        page_size: int,
        resource_policy_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page": page, "pageSize": page_size}
        if kind is not None:
            params["kind"] = str(kind)
        if status is not None:
            params["status"] = str(status)
        if query is not None:
            params["query"] = query
        if resource_policy_id is not None:
            params["resourcePolicyId"] = str(resource_policy_id)
        return params
