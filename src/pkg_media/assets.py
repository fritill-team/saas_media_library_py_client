from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from pkg_media.models import (
    Asset,
    AssetKind,
    AssetStatus,
    BatchResolveResult,
    PaginatedAssets,
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
    """Manage assets (HTTP ``/manage/assets``, gRPC ``fritill.media.v1.Assets``).
    Delivery links are ``client.delivery``."""

    MANAGE_BASE = "/v1/manage/assets"

    def __init__(self, transport: Any):
        self._transport = transport

    # ── Manage endpoints (admin) ───────────────────────────────────────

    async def list(
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
        return await self._transport.list_assets(params)

    async def get(self, asset_id: UUID | str) -> Asset:
        """Get a single asset with resolved download URL (admin)."""
        return await self._transport.get_asset(str(asset_id))

    async def resolve(self, asset_ids: List[UUID | str]) -> List[BatchResolveResult]:
        """Batch resolve download URLs for up to 100 assets (admin)."""
        return await self._transport.resolve_assets([str(a) for a in asset_ids])

    async def delete(self, asset_id: UUID | str) -> None:
        """Delete an asset and all related records."""
        await self._transport.delete_asset(str(asset_id))

    async def update_visibility(
        self,
        asset_id: UUID | str,
        visibility: Visibility | str,
    ) -> Asset:
        """Change an asset's visibility tier.

        Visibility is only data: no bytes move (every asset stays in the
        tenant's R2 bucket). Leaving PUBLIC purges the asset's edge-cached
        copies.

        Once the server turns on ``RESTRICTED_ASSETS_SERVICE_ONLY`` or
        ``MANAGE_RESTRICTED_PREVIEWS_PRIVILEGED_ONLY``, only a privileged
        caller (a service token, or a trusted user with a privileged role)
        may move a RESTRICTED asset to another visibility; anyone else gets
        403 (``AuthenticationError``).
        """
        return await self._transport.update_asset_visibility(str(asset_id), str(visibility))

    async def bulk_update_visibility(
        self,
        asset_ids: List[UUID | str],
        visibility: Visibility | str,
    ) -> BulkVisibilityResult:
        """Change visibility for multiple assets in a single call.

        POST /v1/manage/assets/bulk-visibility

        Assets the change fails for are counted in ``failed``: an unknown
        asset (error ``not_found``), a server error (``internal_error``), or
        (with the server's RESTRICTED switches on) a RESTRICTED asset changed
        by a caller who is not privileged (``forbidden``). See
        ``update_visibility``.
        """
        data = await self._transport.bulk_update_asset_visibility(
            [str(a) for a in asset_ids], str(visibility)
        )
        return BulkVisibilityResult(
            items=data["items"], updated=data["updated"], failed=data["failed"]
        )

    # ── Web-app deploys (rollback) ─────────────────────────────────────

    async def list_deploys(
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
        return await self._transport.list_deploys(str(asset_id), limit, offset)

    async def activate_deploy(
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
        return await self._transport.activate_deploy(str(asset_id), str(deploy_id))

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
