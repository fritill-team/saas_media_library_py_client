from __future__ import annotations

import warnings
from typing import Any, Dict, List, Optional, Set
from uuid import UUID

import httpx

from saas_media_library._http import raise_for_status
from saas_media_library.exceptions import NotFoundError
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
        step_options: Optional[Dict[str, Dict[str, Any]]] = None,
        use_filename_as_asset_id: bool = False,
        enable_transcription: bool = True,
        is_active: bool = True,
        **deprecated_kwargs: Any,
    ) -> ResourcePolicy:
        """Create or update a resource policy.

        If a policy already exists for (tenant, resource_type, collection_name),
        it will be updated and its version incremented.

        Note:
            ``auto_process`` was removed. Processing always starts on upload
            finalization. Configurable steps without pre-configured options
            in ``step_options`` will pause for user input (PENDING_INPUT).
        """
        if "auto_process" in deprecated_kwargs:
            warnings.warn(
                "auto_process is deprecated and ignored. Processing always starts "
                "on upload finalization. Use step_options to pre-configure steps "
                "so they run automatically; otherwise they will pause for user input.",
                DeprecationWarning,
                stacklevel=2,
            )
            deprecated_kwargs.pop("auto_process")
        if deprecated_kwargs:
            raise TypeError(
                f"upsert() got unexpected keyword arguments: {list(deprecated_kwargs)}"
            )

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
                "stepOptions": step_options,
                "useFilenameAsAssetId": use_filename_as_asset_id,
                "enableTranscription": enable_transcription,
            },
        }
        payload["isActive"] = is_active
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

    def get_by_name(
        self,
        resource_type: str,
        collection_name: Optional[str] = None,
    ) -> ResourcePolicy:
        """Get a resource policy by its (resource_type, collection_name) pair.

        This is the natural composite key used by ``upsert``. Easier for
        consumer services to remember than UUIDs.

        Raises:
            NotFoundError: if no policy matches.
        """
        if collection_name is not None:
            matches = self.list(
                resource_type=resource_type,
                collection_name=collection_name,
            )
        else:
            # Default-collection (NULL) policy: server can't filter on NULL via
            # the equality query param, so fetch by resource_type and pick the
            # one with no collection_name.
            matches = [
                p for p in self.list(resource_type=resource_type)
                if p.collection_name is None
            ]

        if not matches:
            raise NotFoundError(
                f"No resource policy found for resource_type={resource_type!r}, "
                f"collection_name={collection_name!r}",
                status_code=404,
                body=None,
            )
        return matches[0]

    def list(
        self,
        *,
        resource_type: Optional[str] = None,
        collection_name: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> List[ResourcePolicy]:
        """List resource policies for the current tenant."""
        params: Dict[str, Any] = {}
        if resource_type is not None:
            params["resource_type"] = resource_type
        if collection_name is not None:
            params["collection_name"] = collection_name
        if is_active is not None:
            params["is_active"] = str(is_active).lower()

        resp = self._http.get(self.BASE, params=params)
        raise_for_status(resp)
        return [ResourcePolicy.model_validate(p) for p in resp.json()]

    def _resolve_id(
        self,
        policy_id: Optional[UUID | str],
        resource_type: Optional[str],
        collection_name: Optional[str],
    ) -> UUID | str:
        """Return policy_id directly, or look it up by (resource_type, collection_name).

        Exactly one of (policy_id) or (resource_type) must be provided.
        """
        if policy_id is not None:
            if resource_type is not None:
                raise TypeError(
                    "Provide either policy_id or resource_type/collection_name, not both."
                )
            return policy_id
        if resource_type is None:
            raise TypeError(
                "Must provide either policy_id or resource_type (with optional collection_name)."
            )
        return self.get_by_name(resource_type, collection_name).id

    def update(
        self,
        policy_id: Optional[UUID | str] = None,
        *,
        resource_type: Optional[str] = None,
        collection_name: Optional[str] = None,
        kind: Optional[AssetKind | str] = None,
        visibility: Optional[Visibility | str] = None,
        file_constraints: Optional[FileConstraints] = None,
        permission_requirements: Optional[PermissionRequirements] = None,
        processing_config: Optional[ProcessingConfig] = None,
        is_active: Optional[bool] = None,
    ) -> ResourcePolicy:
        """Partially update a resource policy.

        Identify the policy either by ``policy_id`` (UUID) or by the
        ``resource_type`` / ``collection_name`` name pair.
        """
        target_id = self._resolve_id(policy_id, resource_type, collection_name)

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

        resp = self._http.put(f"{self.BASE}/{target_id}", json=payload)
        raise_for_status(resp)
        return ResourcePolicy.model_validate(resp.json())

    def delete(
        self,
        policy_id: Optional[UUID | str] = None,
        *,
        resource_type: Optional[str] = None,
        collection_name: Optional[str] = None,
        hard: bool = False,
    ) -> None:
        """Delete a resource policy.

        Identify the policy either by ``policy_id`` (UUID) or by the
        ``resource_type`` / ``collection_name`` name pair.

        By default performs a soft delete (deactivation).
        Pass hard=True to permanently remove.
        """
        target_id = self._resolve_id(policy_id, resource_type, collection_name)
        params = {"hard": "true"} if hard else {}
        resp = self._http.delete(f"{self.BASE}/{target_id}", params=params)
        raise_for_status(resp)

    # ── Convenience helpers ────────────────────────────────────────────

    def activate(
        self,
        policy_id: Optional[UUID | str] = None,
        *,
        resource_type: Optional[str] = None,
        collection_name: Optional[str] = None,
    ) -> ResourcePolicy:
        """Reactivate a soft-deleted policy (by id or by name pair)."""
        return self.update(
            policy_id,
            resource_type=resource_type,
            collection_name=collection_name,
            is_active=True,
        )

    def deactivate(
        self,
        policy_id: Optional[UUID | str] = None,
        *,
        resource_type: Optional[str] = None,
        collection_name: Optional[str] = None,
    ) -> ResourcePolicy:
        """Soft-delete (deactivate) a policy (by id or by name pair)."""
        return self.update(
            policy_id,
            resource_type=resource_type,
            collection_name=collection_name,
            is_active=False,
        )

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

    def sync_policies(
        self,
        policies: List[Dict[str, Any]],
        *,
        delete_stale: bool = True,
        hard_delete: bool = False,
    ) -> List[ResourcePolicy]:
        """Declarative sync: ensure the tenant has exactly these policies.

        Compares the desired ``policies`` list against the existing policies
        by their ``(resource_type, collection_name)`` composite key:

        - **Match** → upsert (update) the existing policy
        - **New** → create the policy
        - **Stale** (exists remotely but not in the list) → delete

        Args:
            policies: List of dicts, each passed as kwargs to ``upsert()``.
            delete_stale: If True (default), policies that exist on the server
                but are not in the list will be deleted.
            hard_delete: If True, stale policies are hard-deleted (permanent).
                If False (default), they are soft-deleted (deactivated).

        Returns:
            List of upserted ``ResourcePolicy`` objects (does not include deleted ones).
        """
        # Normalize keys the same way the server does
        # (domain/entities/resource_policy.py: lower().strip()) so that case or
        # whitespace differences between caller specs and stored values don't
        # cause sync_policies to flag just-upserted policies as stale.
        def _norm(s: Optional[str]) -> Optional[str]:
            return s.lower().strip() if isinstance(s, str) else s

        desired_keys = {
            (_norm(spec["resource_type"]), _norm(spec.get("collection_name")))
            for spec in policies
        }

        # Fetch all existing policies for this tenant
        existing = self.list()
        existing_by_key = {
            (_norm(p.resource_type), _norm(p.collection_name)): p
            for p in existing
        }

        # Upsert desired policies (create or update)
        results = []
        for spec in policies:
            results.append(self.upsert(**spec))

        # Delete stale policies not in the desired list
        if delete_stale:
            for key, policy in existing_by_key.items():
                if key not in desired_keys:
                    self.delete(policy_id=policy.id, hard=hard_delete)

        return results
