from __future__ import annotations

from typing import Optional

import httpx

from saas_media_library.resource_policies import ResourcePoliciesAPI
from saas_media_library.assets import AssetsAPI
from saas_media_library._http import raise_for_status


class MediaLibraryClient:
    """Python client for the SaaS Media Library API.

    Usage::

        client = MediaLibraryClient(
            base_url="https://api.media-library.example.com",
            tenant_id="my-tenant-id",
            token="service-account-jwt",
        )

        # Resource policies
        policy = client.policies.upsert(...)

        # Asset access
        asset = client.assets.get(asset_id)

        # Always close when done
        client.close()

    Or as a context manager::

        with MediaLibraryClient(...) as client:
            client.policies.upsert(...)
    """

    def __init__(
        self,
        base_url: str,
        tenant_id: str,
        token: Optional[str] = None,
        timeout: float = 30.0,
        http_client: Optional[httpx.Client] = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._tenant_id = tenant_id
        self._token = token

        headers = {"X-Tenant": tenant_id}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        if http_client:
            self._http = http_client
            self._owns_client = False
        else:
            self._http = httpx.Client(
                base_url=self._base_url,
                headers=headers,
                timeout=timeout,
            )
            self._owns_client = True

        self.policies = ResourcePoliciesAPI(self._http)
        self.assets = AssetsAPI(self._http)

    def set_token(self, token: str) -> None:
        """Update the auth token (e.g. after refresh)."""
        self._token = token
        self._http.headers["Authorization"] = f"Bearer {token}"

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> MediaLibraryClient:
        return self

    def __exit__(self, *args) -> None:
        self.close()


# Re-export for backward compatibility
__all__ = ["MediaLibraryClient", "raise_for_status"]
