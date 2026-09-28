from __future__ import annotations

from typing import Optional

import httpx

from saas_media_library._http import raise_for_status
from saas_media_library.resource_policies import ResourcePoliciesAPI
from saas_media_library.assets import AssetsAPI
from saas_media_library.asset_access import AssetAccessAPI
from saas_media_library.delivery import DeliveryAPI
from saas_media_library.processing_options import ProcessingOptionsAPI
from saas_media_library.uploads import UploadsAPI

__all__ = ["MediaLibraryClient", "raise_for_status"]


class MediaLibraryClient:
    """Python client for the SaaS Media Library API.

    Usage::

        client = MediaLibraryClient(
            base_url="https://api.media-library.example.com",
            tenant_id="my-tenant-id",
            token="service-account-jwt",
        )

        # Uploads, with rules defined in the app (UploadProfile)
        result = client.uploads.upload_file("cover.png", profile=COURSE_COVER)

        # Delivery links (a service token also resolves RESTRICTED assets),
        # optionally through a cache shared by every user of the app
        items = client.delivery.resolve(asset_ids, cache=url_cache)

        # RESTRICTED assets: the app decides who gets the links
        gate = AccessGate(client, can_access, cache=AssetUrlCache())
        items = gate.resolve(current_user, asset_ids)

        # Processing options metadata (kinds, MIME types, pipeline steps)
        schema = client.processing.get_schema()

        # Assets
        asset = client.assets.get(asset_id)
        client.assets.update_visibility(asset_id, Visibility.RESTRICTED)

        # Always close when done
        client.close()

    Or as a context manager::

        with MediaLibraryClient(...) as client:
            client.uploads.upload_file(...)

    ``client.policies`` (resource policies) and ``client.access`` (access
    grants) are deprecated and warn on every call; use ``UploadProfile`` with
    ``client.uploads``, and ``AccessGate``, instead.
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
        self.access = AssetAccessAPI(self._http)
        self.uploads = UploadsAPI(self._http)
        self.delivery = DeliveryAPI(self._http, tenant_id=tenant_id)
        self.processing = ProcessingOptionsAPI(self._http)

    @property
    def tenant_id(self) -> str:
        """The tenant this client works in (sent as ``X-Tenant``)."""
        return self._tenant_id

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
