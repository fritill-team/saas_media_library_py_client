from __future__ import annotations

from typing import Any, Dict, List, Optional, Set
from uuid import UUID

import httpx

from saas_media_library.client import raise_for_status
from saas_media_library.models import (
    AssetKind,
    FileConstraints,
    PermissionRequirements,
    ProcessingConfig,
    ResourcePolicy,
    Visibility,
)


class ResourcePoliciesAPI:
    """Manage resource policies for a tenant.

    Resource policies define upload constraints, processing pipelines,
    and visibility for each resource type (e.g. "courses", "products").
    """

    BASE = "/v1/manage/resource-policies"

    def __init__(self, http: httpx.Client):
        self._http = http

    # ── CRUD ───────────────────────────────────────────────────────────

    def upsert(
        self,
        resource_type: str,
        kind: AssetKind | str,
        visibility: Visibility | str,
        allowed_mime_types: Set[str],
        *,
        collection_name: Optional[str] = None,
        max_size_bytes: Optional[int] = None,
        min_size_bytes: Optional[int] = None,
        allow_multiple: bool = False,
        required_permissions: Optional[Set[str]] = None,
        allow_anonymous: bool = False,
        auto_process: bool = False,
        step_options: Optional[Dict[str, Dict[str, Any]]] = None,
        use_filename_as_asset_id: bool = False,
    ) -> ResourcePolicy:
        """Create or update a resource policy.

        If a policy already exists for (tenant, resource_type, collection_name),
        it will be updated and its version incremented.
        """
        payload: Dict[str, Any] = {
            "resourceType": resource_type,
            "kind": str(kind),
            "visibility": str(visibility),
            "fileConstraints": {
                "allowedMimeTypes": sorted(allowed_mime_types),
                "maxSizeBytes": max_size_bytes,
                "minSizeBytes": min_size_bytes,
                "allowMultiple": allow_multiple,
            },
            "permissionRequirements": {
                "requiredPermissions": sorted(required_permissions or []),
                "allowAnonymous": allow_anonymous,
            },
            "processingConfig": {
                "autoProcess": auto_process,
                "stepOptions": step_options,
                "useFilenameAsAssetId": use_filename_as_asset_id,
            },
        }
        if collection_name is not None:
            payload["collectionName"] = collection_name

        resp = self._http.post(self.BASE, json=payload)
        raise_for_status(resp)
        return ResourcePolicy.model_validate(resp.json())

    def get(self, policy_id: UUID | str) -> ResourcePolicy:
        """Get a resource policy by ID."""
        resp = self._http.get(f"{self.BASE}/{policy_id}")
        raise_for_status(resp)
        return ResourcePolicy.model_validate(resp.json())

    def list(
        self,
        *,
        resource_type: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> List[ResourcePolicy]:
        """List resource policies for the current tenant."""
        params: Dict[str, Any] = {}
        if resource_type is not None:
            params["resource_type"] = resource_type
        if is_active is not None:
            params["is_active"] = str(is_active).lower()

        resp = self._http.get(self.BASE, params=params)
        raise_for_status(resp)
        return [ResourcePolicy.model_validate(p) for p in resp.json()]

    def update(
        self,
        policy_id: UUID | str,
        *,
        kind: Optional[AssetKind | str] = None,
        visibility: Optional[Visibility | str] = None,
        file_constraints: Optional[FileConstraints] = None,
        permission_requirements: Optional[PermissionRequirements] = None,
        processing_config: Optional[ProcessingConfig] = None,
        is_active: Optional[bool] = None,
    ) -> ResourcePolicy:
        """Partially update a resource policy."""
        payload: Dict[str, Any] = {}
        if kind is not None:
            payload["kind"] = str(kind)
        if visibility is not None:
            payload["visibility"] = str(visibility)
        if file_constraints is not None:
            payload["fileConstraints"] = file_constraints.model_dump(by_alias=True)
        if permission_requirements is not None:
            payload["permissionRequirements"] = permission_requirements.model_dump(by_alias=True)
        if processing_config is not None:
            payload["processingConfig"] = processing_config.model_dump(by_alias=True)
        if is_active is not None:
            payload["isActive"] = is_active

        resp = self._http.put(f"{self.BASE}/{policy_id}", json=payload)
        raise_for_status(resp)
        return ResourcePolicy.model_validate(resp.json())

    def delete(self, policy_id: UUID | str, *, hard: bool = False) -> None:
        """Delete a resource policy.

        By default performs a soft delete (deactivation).
        Pass hard=True to permanently remove.
        """
        params = {"hard": "true"} if hard else {}
        resp = self._http.delete(f"{self.BASE}/{policy_id}", params=params)
        raise_for_status(resp)

    # ── Convenience helpers ────────────────────────────────────────────

    def activate(self, policy_id: UUID | str) -> ResourcePolicy:
        """Reactivate a soft-deleted policy."""
        return self.update(policy_id, is_active=True)

    def deactivate(self, policy_id: UUID | str) -> ResourcePolicy:
        """Soft-delete (deactivate) a policy."""
        return self.update(policy_id, is_active=False)

    # ── Schema / metadata ──────────────────────────────────────────────

    def get_schema(self) -> Dict[str, Any]:
        """Get the full metadata schema (kinds, visibilities, MIME types, defaults)."""
        resp = self._http.get(f"{self.BASE}/schema")
        raise_for_status(resp)
        return resp.json()

    def get_available_steps(self) -> Dict[str, Any]:
        """Get available processing steps and options per AssetKind."""
        resp = self._http.get(f"{self.BASE}/pipelines/available-steps")
        raise_for_status(resp)
        return resp.json()

    # ── Bulk helpers (init container use case) ─────────────────────────

    def sync_policies(self, policies: List[Dict[str, Any]]) -> List[ResourcePolicy]:
        """Upsert multiple policies at once.

        Each dict in the list is passed as kwargs to upsert().
        Useful for init container jobs that declare all policies at startup.

        Example::

            client.policies.sync_policies([
                {
                    "resource_type": "courses",
                    "collection_name": "cover",
                    "kind": "image",
                    "visibility": "public",
                    "allowed_mime_types": {"image/jpeg", "image/png", "image/webp"},
                    "max_size_bytes": 5_000_000,
                    "auto_process": True,
                    "step_options": {
                        "image_derive": {"formats": ["webp"], "sizes": [256, 512, 1024]}
                    },
                },
                {
                    "resource_type": "courses",
                    "collection_name": "video",
                    "kind": "video",
                    "visibility": "restricted",
                    "allowed_mime_types": {"video/mp4", "video/quicktime"},
                    "max_size_bytes": 2_000_000_000,
                    "auto_process": True,
                },
            ])
        """
        results = []
        for spec in policies:
            results.append(self.upsert(**spec))
        return results
