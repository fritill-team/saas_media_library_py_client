from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

import httpx

from saas_media_library._http import raise_for_status
from saas_media_library.models import AssetAccessGrant


class AssetAccessAPI:
    """Manage per-user asset access grants (enrollment-style ACL).

    Grants gate downloads of assets with visibility=RESTRICTED. A grant is
    keyed by (asset_id, user_id); re-granting upserts. ``expires_at=None``
    means the grant never expires.
    """

    BASE = "/v1/manage/assets"

    def __init__(self, http: httpx.Client):
        self._http = http

    def grant(
        self,
        asset_id: UUID | str,
        user_id: UUID | str,
        expires_at: Optional[datetime] = None,
    ) -> AssetAccessGrant:
        """Add or refresh a grant. Idempotent on (asset_id, user_id)."""
        payload: Dict[str, Any] = {"userId": str(user_id)}
        if expires_at is not None:
            payload["expiresAt"] = expires_at.isoformat()
        resp = self._http.post(f"{self.BASE}/{asset_id}/access", json=payload)
        raise_for_status(resp)
        return AssetAccessGrant.model_validate(resp.json())

    def revoke(self, asset_id: UUID | str, user_id: UUID | str) -> None:
        """Remove a grant. Idempotent — succeeds even if no row existed."""
        resp = self._http.delete(f"{self.BASE}/{asset_id}/access/{user_id}")
        raise_for_status(resp)

    def list(self, asset_id: UUID | str) -> List[AssetAccessGrant]:
        """List all (active + expired) grants for an asset."""
        resp = self._http.get(f"{self.BASE}/{asset_id}/access")
        raise_for_status(resp)
        return [AssetAccessGrant.model_validate(i) for i in resp.json()["items"]]
