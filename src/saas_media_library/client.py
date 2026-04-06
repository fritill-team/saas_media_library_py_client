from __future__ import annotations

from typing import Optional

import httpx

from saas_media_library.exceptions import (
    AuthenticationError,
    ConflictError,
    MediaLibraryError,
    NotFoundError,
    ServerError,
    ValidationError,
)
from saas_media_library.resource_policies import ResourcePoliciesAPI
from saas_media_library.assets import AssetsAPI


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


def raise_for_status(response: httpx.Response) -> None:
    """Raise a typed exception for non-2xx responses."""
    if response.is_success:
        return

    try:
        body = response.json()
    except Exception:
        body = response.text

    msg = str(body.get("detail", body) if isinstance(body, dict) else body)
    code = response.status_code

    if code == 401 or code == 403:
        raise AuthenticationError(msg, status_code=code, body=body)
    elif code == 404:
        raise NotFoundError(msg, status_code=code, body=body)
    elif code == 409:
        raise ConflictError(msg, status_code=code, body=body)
    elif code == 422:
        raise ValidationError(msg, status_code=code, body=body)
    elif code >= 500:
        raise ServerError(msg, status_code=code, body=body)
    else:
        raise MediaLibraryError(msg, status_code=code, body=body)
