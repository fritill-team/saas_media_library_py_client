from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
import respx

from saas_media_library import (
    AuthenticationError,
    MediaLibraryClient,
    NotFoundError,
)


BASE_URL = "https://media.test"
TENANT_ID = str(uuid4())


def _make_client() -> MediaLibraryClient:
    return MediaLibraryClient(base_url=BASE_URL, tenant_id=TENANT_ID, token="t")


@respx.mock
def test_grant_returns_parsed_model() -> None:
    asset_id = uuid4()
    user_id = uuid4()
    expires_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    server_response = {
        "userId": str(user_id),
        "expiresAt": expires_at.isoformat(),
        "grantedAt": "2026-05-08T00:00:00",
        "grantedByActorType": "tenant_user",
        "grantedByActorId": "owner-sub",
    }
    route = respx.post(
        f"{BASE_URL}/v1/manage/assets/{asset_id}/access"
    ).mock(return_value=httpx.Response(200, json=server_response))

    with _make_client() as client:
        grant = client.access.grant(asset_id, user_id, expires_at=expires_at)

    assert route.called
    assert grant.user_id == user_id
    assert grant.expires_at == expires_at.replace(tzinfo=timezone.utc)
    assert grant.granted_by_actor_type == "tenant_user"


@respx.mock
def test_grant_without_expiry_omits_field() -> None:
    asset_id = uuid4()
    user_id = uuid4()
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        captured["body"] = _json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "userId": str(user_id),
                "expiresAt": None,
                "grantedAt": "2026-05-08T00:00:00",
                "grantedByActorType": "service",
                "grantedByActorId": None,
            },
        )

    respx.post(f"{BASE_URL}/v1/manage/assets/{asset_id}/access").mock(side_effect=handler)

    with _make_client() as client:
        client.access.grant(asset_id, user_id)

    assert captured["body"] == {"userId": str(user_id)}


@respx.mock
def test_revoke_returns_none_on_204() -> None:
    asset_id = uuid4()
    user_id = uuid4()
    route = respx.delete(
        f"{BASE_URL}/v1/manage/assets/{asset_id}/access/{user_id}"
    ).mock(return_value=httpx.Response(204))

    with _make_client() as client:
        result = client.access.revoke(asset_id, user_id)

    assert route.called
    assert result is None


@respx.mock
def test_list_returns_grants() -> None:
    asset_id = uuid4()
    grants = [
        {
            "userId": str(uuid4()),
            "expiresAt": None,
            "grantedAt": "2026-05-08T00:00:00",
            "grantedByActorType": "tenant_user",
            "grantedByActorId": "u1",
        },
        {
            "userId": str(uuid4()),
            "expiresAt": "2030-01-01T00:00:00+00:00",
            "grantedAt": "2026-05-08T00:00:00",
            "grantedByActorType": "service",
            "grantedByActorId": "svc-1",
        },
    ]
    respx.get(
        f"{BASE_URL}/v1/manage/assets/{asset_id}/access"
    ).mock(return_value=httpx.Response(200, json={"items": grants, "total": 2}))

    with _make_client() as client:
        result = client.access.list(asset_id)

    assert len(result) == 2
    assert result[0].expires_at is None
    assert result[1].granted_by_actor_type == "service"


@respx.mock
def test_grant_403_raises_auth_error() -> None:
    asset_id = uuid4()
    user_id = uuid4()
    respx.post(
        f"{BASE_URL}/v1/manage/assets/{asset_id}/access"
    ).mock(return_value=httpx.Response(403, json={"detail": "forbidden"}))

    with _make_client() as client, pytest.raises(AuthenticationError):
        client.access.grant(asset_id, user_id)


@respx.mock
def test_list_404_raises_not_found() -> None:
    asset_id = uuid4()
    respx.get(
        f"{BASE_URL}/v1/manage/assets/{asset_id}/access"
    ).mock(return_value=httpx.Response(404, json={"detail": "Asset not found"}))

    with _make_client() as client, pytest.raises(NotFoundError):
        client.access.list(asset_id)
