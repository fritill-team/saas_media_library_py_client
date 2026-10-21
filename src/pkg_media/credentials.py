"""Keycloak client credentials for calling media as a service.

Media trusts itqadem's backend services through the Itqadem Keycloak realm
(audience ``media-api``). A service's Keycloak client gets a token with the
client-credentials grant; ``ClientCredentials`` fetches it, caches it and
fetches a new one shortly before it expires. The client sends it as
``authorization: Bearer <token>`` (gRPC metadata or HTTP header).
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

import httpx

from pkg_media.exceptions import UnauthenticatedError, UnavailableError


class ClientCredentials:
    """A cached client-credentials token from a Keycloak realm.

    ``token_url`` is the realm's token endpoint,
    ``{keycloak}/realms/{realm}/protocol/openid-connect/token`` (see
    ``for_realm``). A token is reused until ``refresh_margin`` seconds before
    it expires (at most half its lifetime), so a call never starts with a
    token about to lapse. Concurrent callers share one fetch.

    The secret is sent only to ``token_url`` and never appears in errors or
    ``repr``.
    """

    DEFAULT_REFRESH_MARGIN = 30.0

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        *,
        scope: Optional[str] = None,
        refresh_margin: float = DEFAULT_REFRESH_MARGIN,
        timeout: float = 10.0,
        http_client: Optional[httpx.AsyncClient] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not token_url or not client_id or not client_secret:
            raise ValueError("ClientCredentials needs token_url, client_id and client_secret")
        self.token_url = token_url
        self.client_id = client_id
        self._client_secret = client_secret
        self.scope = scope
        self.refresh_margin = refresh_margin
        self._timeout = timeout
        self._http = http_client
        self._clock = clock
        self._token: Optional[str] = None
        self._refresh_at = 0.0
        self._lock: Optional[asyncio.Lock] = None

    @classmethod
    def for_realm(
        cls,
        keycloak_url: str,
        realm: str,
        client_id: str,
        client_secret: str,
        **kwargs,
    ) -> ClientCredentials:
        """Credentials for ``{keycloak_url}/realms/{realm}``."""
        token_url = f"{keycloak_url.rstrip('/')}/realms/{realm}/protocol/openid-connect/token"
        return cls(token_url, client_id, client_secret, **kwargs)

    def __repr__(self) -> str:
        return f"ClientCredentials(token_url={self.token_url!r}, client_id={self.client_id!r})"

    async def token(self) -> str:
        """A valid access token, fetched when the cached one is due."""
        if self._token is not None and self._clock() < self._refresh_at:
            return self._token
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._token is None or self._clock() >= self._refresh_at:
                await self._fetch()
            assert self._token is not None
            return self._token

    def invalidate(self) -> None:
        """Drop the cached token (e.g. after the server rejected it)."""
        self._token = None
        self._refresh_at = 0.0

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _fetch(self) -> None:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self._client_secret,
        }
        if self.scope:
            data["scope"] = self.scope
        started = self._clock()
        try:
            resp = await self._http.post(self.token_url, data=data)
        except httpx.HTTPError as exc:
            raise UnavailableError(f"Keycloak token request failed: {type(exc).__name__}") from None
        if resp.status_code >= 500:
            raise UnavailableError(f"Keycloak token request failed with {resp.status_code}", status_code=resp.status_code)
        if not resp.is_success:
            # Keycloak's body names the problem (invalid_client…), never the secret.
            raise UnauthenticatedError(
                f"Keycloak refused the client credentials of {self.client_id!r} ({resp.status_code})",
                status_code=resp.status_code,
                reason=_error_code(resp),
            )
        body = resp.json()
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise UnauthenticatedError("Keycloak returned no access_token")
        lifetime = float(body.get("expires_in") or 60)
        self._token = token
        self._refresh_at = started + lifetime - min(self.refresh_margin, lifetime / 2)


def _error_code(resp: httpx.Response) -> Optional[str]:
    try:
        error = resp.json().get("error")
    except Exception:
        return None
    return error if isinstance(error, str) else None
