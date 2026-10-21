from __future__ import annotations

from typing import Any, Literal, Optional

import grpc
import httpx

from pkg_media._http import raise_for_status
from pkg_media._sync import LoopThread, SyncAPI
from pkg_media._transport.grpc_transport import GrpcTransport
from pkg_media._transport.http_transport import BearerAuth, HttpTransport
from pkg_media.asset_access import AssetAccessAPI
from pkg_media.assets import AssetsAPI
from pkg_media.credentials import ClientCredentials
from pkg_media.delivery import DeliveryAPI
from pkg_media.exceptions import MediaLibraryError
from pkg_media.grants import GrantsAPI
from pkg_media.permalinks import PermalinksAPI
from pkg_media.processing_options import ProcessingOptionsAPI
from pkg_media.resource_policies import ResourcePoliciesAPI
from pkg_media.uploads import UploadsAPI

__all__ = ["AsyncMediaClient", "MediaClient", "MediaLibraryClient", "raise_for_status"]

Transport = Literal["http", "grpc"]


class AsyncMediaClient:
    """Async client for the media library, over HTTP or gRPC.

    The same methods on both transports::

        # gRPC, as a Keycloak client of the Itqadem realm (audience media-api)
        client = AsyncMediaClient(
            transport="grpc",
            target="media-library-grpc.media-library:50051",
            token_url="https://sso.example.com/realms/Itqadem/protocol/openid-connect/token",
            client_id="courses-service",
            client_secret=os.environ["MEDIA_CLIENT_SECRET"],
        )

        # HTTP, with the same credentials (or a media service token)
        client = AsyncMediaClient(transport="http", base_url="https://media.example.com",
                                  token_url=..., client_id=..., client_secret=...)

        asset = await client.assets.get(asset_id)
        items = await client.delivery.resolve(asset_ids)
        opened = await client.uploads.open_session(mime_type="image/png", profile=COVER,
                                                   subject=str(user.id))
        await client.aclose()

    Credentials: ``token_url`` + ``client_id`` + ``client_secret`` (or a
    ``ClientCredentials``) fetch and cache a client-credentials token,
    refreshed before it expires; ``token`` is a fixed bearer token instead
    (e.g. a media service token over HTTP). Sent as ``authorization`` (gRPC
    metadata) or ``Authorization`` (HTTP header). No ``X-Tenant`` is sent.

    gRPC: mutating calls carry an ``idempotency-key``; UNAVAILABLE is retried
    ``retries`` times (read-only and keyed calls only). Errors raise one
    exception class per status (``pkg_media.exceptions``) with ``reason``.
    File bytes never travel over gRPC (``uploads.upload_file`` and
    ``upload_part`` need ``transport="http"``).

    ``tenant_id`` is never sent; it only scopes ``AssetUrlCache`` entries
    and is echoed by the upload router (older browsers send it as
    ``X-Tenant``).
    """

    def __init__(
        self,
        transport: Transport = "http",
        *,
        base_url: Optional[str] = None,
        target: Optional[str] = None,
        token: Optional[str] = None,
        credentials: Optional[ClientCredentials] = None,
        token_url: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        tenant_id: Optional[str] = None,
        timeout: float = 30.0,
        retries: int = 2,
        secure: bool = False,
        http_client: Optional[httpx.AsyncClient] = None,
        channel: Optional[grpc.aio.Channel] = None,
    ):
        if any(v is not None for v in (token_url, client_id, client_secret)):
            if credentials is not None:
                raise ValueError("pass credentials or token_url/client_id/client_secret, not both")
            if not (token_url and client_id and client_secret):
                raise ValueError("client credentials need token_url, client_id and client_secret")
            credentials = ClientCredentials(token_url, client_id, client_secret)
        if credentials is not None and token is not None:
            raise ValueError("pass either token or client credentials, not both")
        self._token = token
        self.credentials = credentials
        self._tenant_id = tenant_id

        if transport == "http":
            if not base_url:
                raise ValueError('transport="http" needs base_url')
            if http_client is not None and not isinstance(http_client, httpx.AsyncClient):
                raise TypeError("http_client must be an httpx.AsyncClient")
            self.base_url: Optional[str] = base_url.rstrip("/")
            owns = http_client is None
            http = http_client or httpx.AsyncClient(base_url=self.base_url, timeout=timeout)
            self._transport: Any = HttpTransport(http, owns_client=owns, auth=self._auth())
        elif transport == "grpc":
            if not target:
                raise ValueError('transport="grpc" needs target (host:port)')
            self.base_url = None
            self._transport = GrpcTransport(
                target,
                token=self._token_source() if (token or credentials) else None,
                on_rejected_token=credentials.invalidate if credentials is not None else None,
                timeout=timeout,
                retries=retries,
                secure=secure,
                channel=channel,
            )
        else:
            raise ValueError(f'transport must be "http" or "grpc", got {transport!r}')
        self.transport: Transport = transport

        self.assets = AssetsAPI(self._transport)
        self.delivery = DeliveryAPI(self._transport, tenant_id=tenant_id)
        self.uploads = UploadsAPI(self._transport)
        self.grants = GrantsAPI(self._transport)
        self.permalinks = PermalinksAPI(self._transport)
        self.processing = ProcessingOptionsAPI(self._transport)

    @property
    def tenant_id(self) -> Optional[str]:
        """Scopes ``AssetUrlCache`` entries; never sent to the server."""
        return self._tenant_id

    def set_token(self, token: str) -> None:
        """Replace the fixed bearer token (e.g. after a rotation)."""
        if self.credentials is not None:
            raise ValueError("this client uses client credentials; tokens are fetched automatically")
        self._token = token
        if isinstance(self._transport, HttpTransport):
            self._transport.auth = self._auth()

    async def aclose(self) -> None:
        await self._transport.aclose()
        if self.credentials is not None:
            await self.credentials.aclose()

    async def __aenter__(self) -> AsyncMediaClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    # ── Credentials ────────────────────────────────────────────────────

    def _token_source(self):
        async def token() -> str:
            if self.credentials is not None:
                return await self.credentials.token()
            assert self._token is not None
            return self._token

        return token

    def _auth(self) -> Optional[BearerAuth]:
        if self.credentials is None and self._token is None:
            return None
        return BearerAuth(self._token_source())


class MediaClient:
    """Sync client for the media library, over HTTP or gRPC.

    Takes ``AsyncMediaClient``'s arguments and exposes the same APIs with
    blocking methods (``client.assets.get(asset_id)``…), running the async
    client on a private event-loop thread. Safe to share between threads.
    Close it (or use it as a context manager) to stop that thread. In async
    code, use ``AsyncMediaClient`` instead.

    ``client.policies`` and ``client.access`` (deprecated) are HTTP-only and
    warn on every call; use ``UploadProfile`` with ``client.uploads``, and
    ``AccessGate``, instead.
    """

    def __init__(self, transport: Transport = "http", **kwargs: Any):
        self._runner = LoopThread()
        try:
            self.aio = AsyncMediaClient(transport, **kwargs)
        except BaseException:
            self._runner.stop()
            raise
        self._timeout = kwargs.get("timeout", 30.0)
        self._legacy_http: Optional[httpx.Client] = None
        self.assets: Any = SyncAPI(self.aio.assets, self._runner)
        self.delivery: Any = SyncAPI(self.aio.delivery, self._runner)
        self.uploads: Any = SyncAPI(self.aio.uploads, self._runner)
        self.grants: Any = SyncAPI(self.aio.grants, self._runner)
        self.permalinks: Any = SyncAPI(self.aio.permalinks, self._runner)
        self.processing: Any = SyncAPI(self.aio.processing, self._runner)

    @property
    def transport(self) -> Transport:
        return self.aio.transport

    @property
    def tenant_id(self) -> Optional[str]:
        """Scopes ``AssetUrlCache`` entries; never sent to the server."""
        return self.aio.tenant_id

    @property
    def policies(self) -> ResourcePoliciesAPI:
        """Deprecated resource policies (HTTP only)."""
        return ResourcePoliciesAPI(self._deprecated_http("policies"))

    @property
    def access(self) -> AssetAccessAPI:
        """Deprecated per-user access grants (HTTP only)."""
        return AssetAccessAPI(self._deprecated_http("access"))

    def set_token(self, token: str) -> None:
        """Replace the fixed bearer token (e.g. after a rotation)."""
        self.aio.set_token(token)
        if self._legacy_http is not None:
            self._legacy_http.headers["Authorization"] = f"Bearer {token}"

    def close(self) -> None:
        if self._runner.loop.is_closed():
            return
        try:
            self._runner.run(self.aio.aclose())
        finally:
            self._runner.stop()
            if self._legacy_http is not None:
                self._legacy_http.close()

    def __enter__(self) -> MediaClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _deprecated_http(self, api: str) -> httpx.Client:
        if self.aio.transport != "http":
            raise MediaLibraryError(f"client.{api} is deprecated and HTTP-only: use a client with transport='http'")
        if self._legacy_http is None:
            headers = {}
            auth = None
            if self.aio.credentials is not None:
                credentials = self.aio.credentials
                auth = BearerAuth(credentials.token, sync_token=lambda: self._runner.run(credentials.token()))
            elif self.aio._token is not None:
                headers["Authorization"] = f"Bearer {self.aio._token}"
            self._legacy_http = httpx.Client(
                base_url=self.aio.base_url or "", headers=headers, auth=auth, timeout=self._timeout
            )
        return self._legacy_http


class MediaLibraryClient(MediaClient):
    """``MediaClient`` over HTTP with the signature of earlier releases::

        client = MediaLibraryClient(base_url="https://media.example.com", token="ml_live_…")

    ``tenant_id`` is optional and never sent (no ``X-Tenant``); it only
    scopes ``AssetUrlCache`` entries. ``http_client`` must be an
    ``httpx.AsyncClient``.
    """

    def __init__(
        self,
        base_url: str,
        tenant_id: Optional[str] = None,
        token: Optional[str] = None,
        timeout: float = 30.0,
        http_client: Optional[httpx.AsyncClient] = None,
        **kwargs: Any,
    ):
        super().__init__(
            "http",
            base_url=base_url,
            tenant_id=tenant_id,
            token=token,
            timeout=timeout,
            http_client=http_client,
            **kwargs,
        )

    def __enter__(self) -> MediaLibraryClient:
        return self
