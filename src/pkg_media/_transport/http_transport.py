"""The HTTP transport: media's REST API through an ``httpx.AsyncClient``.

No delivery: media removed HTTP ``/delivery`` (``client.delivery`` is gRPC
only).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Generator, List, Optional, Tuple

import httpx

from pkg_media._http import raise_for_status
from pkg_media.grants import MediaGrant
from pkg_media.models import (
    Asset,
    BatchResolveResult,
    PaginatedAssets,
    UploadAbortResult,
    UploadFinalizeResult,
    UploadPartResult,
    UploadSession,
    WebAppDeployList,
)
from pkg_media.permalinks import Permalink

TokenSource = Callable[[], Awaitable[str]]

ASSETS = "/v1/manage/assets"
UPLOADS = "/v1/manage/uploads/sessions"
GRANTS = "/v1/manage/grants"
PERMALINKS = "/v1/manage/permalinks"
PROCESSING = "/v1/manage/processing-options"


class BearerAuth(httpx.Auth):
    """``Authorization: Bearer <token>`` from a token source, per request.

    ``sync_token`` serves sync clients (the deprecated access API).
    """

    def __init__(self, token: TokenSource, sync_token: Optional[Callable[[], str]] = None):
        self._token = token
        self._sync_token = sync_token

    async def async_auth_flow(self, request: httpx.Request):  # type: ignore[override]
        request.headers["Authorization"] = f"Bearer {await self._token()}"
        yield request

    def sync_auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        if self._sync_token is None:
            raise RuntimeError("this credential has no sync token source")
        request.headers["Authorization"] = f"Bearer {self._sync_token()}"
        yield request


class HttpTransport:
    """Media's HTTP API. Sends no ``X-Tenant`` (media serves one app)."""

    name = "http"

    def __init__(self, http: httpx.AsyncClient, *, owns_client: bool = True, auth: Optional[httpx.Auth] = None):
        self._http = http
        self._owns_client = owns_client
        # Per request, so a caller's own AsyncClient gets the credential too.
        self.auth = auth

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    # ── Assets ─────────────────────────────────────────────────────────

    async def list_assets(self, params: Dict[str, Any]) -> PaginatedAssets:
        return PaginatedAssets.model_validate(await self._json("GET", ASSETS, params=params))

    async def get_asset(self, asset_id: str) -> Asset:
        return Asset.model_validate(await self._json("GET", f"{ASSETS}/{asset_id}"))

    async def resolve_assets(self, asset_ids: List[str]) -> List[BatchResolveResult]:
        data = await self._json("POST", f"{ASSETS}/resolve", json={"assetIds": asset_ids})
        return [BatchResolveResult.model_validate(i) for i in data["items"]]

    async def delete_asset(self, asset_id: str) -> None:
        await self._send("DELETE", f"{ASSETS}/{asset_id}")

    async def update_asset_visibility(self, asset_id: str, visibility: str) -> Asset:
        data = await self._json("PATCH", f"{ASSETS}/{asset_id}", json={"visibility": visibility})
        return Asset.model_validate(data)

    async def bulk_update_asset_visibility(self, asset_ids: List[str], visibility: str) -> Dict[str, Any]:
        payload = {"assetIds": asset_ids, "visibility": visibility}
        return await self._json("POST", f"{ASSETS}/bulk-visibility", json=payload)

    async def list_deploys(self, asset_id: str, limit: int, offset: int) -> WebAppDeployList:
        data = await self._json("GET", f"{ASSETS}/{asset_id}/deploys", params={"limit": limit, "offset": offset})
        return WebAppDeployList.model_validate(data)

    async def activate_deploy(self, asset_id: str, deploy_id: str) -> WebAppDeployList:
        data = await self._json("POST", f"{ASSETS}/{asset_id}/deploys/{deploy_id}/activate")
        return WebAppDeployList.model_validate(data)

    # ── Uploads ────────────────────────────────────────────────────────

    async def create_session(self, payload: Dict[str, Any]) -> UploadSession:
        return UploadSession.model_validate(await self._json("POST", UPLOADS, json=payload))

    async def open_session(
        self,
        payload: Dict[str, Any],
        grant_payload: Dict[str, Any],
    ) -> Tuple[UploadSession, MediaGrant]:
        """Create the session, then an ``upload`` grant for it. When the grant
        cannot be issued the session is aborted, as ``Uploads.OpenSession``
        leaves no session open on failure."""
        session = await self.create_session(payload)
        try:
            grant = await self.issue_grant({**grant_payload, "sessionIds": [str(session.id)]})
        except BaseException:
            try:
                await self.abort_session(str(session.id))
            except Exception:
                pass
            raise
        return session, grant

    async def get_session(self, session_id: str) -> UploadSession:
        return UploadSession.model_validate(await self._json("GET", f"{UPLOADS}/{session_id}"))

    async def upload_part(self, session_id: str, part_number: int, body: bytes, headers: Dict[str, str]) -> UploadPartResult:
        data = await self._json("PUT", f"{UPLOADS}/{session_id}/parts/{part_number}", content=body, headers=headers)
        return UploadPartResult.model_validate(data)

    async def finalize_session(self, session_id: str, expected_sha256_hex: Optional[str]) -> UploadFinalizeResult:
        params = {"expected_sha256_hex": expected_sha256_hex} if expected_sha256_hex else {}
        data = await self._json("POST", f"{UPLOADS}/{session_id}/finalize", params=params)
        return UploadFinalizeResult.model_validate(data)

    async def abort_session(self, session_id: str) -> UploadAbortResult:
        return UploadAbortResult.model_validate(await self._json("POST", f"{UPLOADS}/{session_id}/abort"))

    # ── Grants ─────────────────────────────────────────────────────────

    async def issue_grant(self, payload: Dict[str, Any]) -> MediaGrant:
        return MediaGrant.model_validate(await self._json("POST", GRANTS, json=payload))

    # ── Permalinks ─────────────────────────────────────────────────────

    async def set_permalink(self, name: str, asset_id: str) -> Permalink:
        return Permalink.model_validate(await self._json("PUT", f"{PERMALINKS}/{name}", json={"assetId": asset_id}))

    async def get_permalink(self, name: str) -> Permalink:
        return Permalink.model_validate(await self._json("GET", f"{PERMALINKS}/{name}"))

    async def delete_permalink(self, name: str) -> None:
        await self._send("DELETE", f"{PERMALINKS}/{name}")

    # ── Processing ─────────────────────────────────────────────────────

    async def get_processing_schema(self) -> Dict[str, Any]:
        return await self._json("GET", f"{PROCESSING}/schema")

    async def get_processing_pipelines(self) -> Dict[str, Any]:
        return await self._json("GET", f"{PROCESSING}/pipelines")

    # ── Private helpers ────────────────────────────────────────────────

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self.auth is not None:
            kwargs["auth"] = self.auth
        resp = await self._http.request(method, url, **kwargs)
        raise_for_status(resp)
        return resp

    async def _json(self, method: str, url: str, **kwargs: Any) -> Any:
        return (await self._send(method, url, **kwargs)).json()
