from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
import respx

from pkg_media import (
    AuthenticationError,
    MediaLibraryClient,
    NotFoundError,
    Permalink,
    ValidationError,
    permalink_url,
    validate_permalink_name,
)


BASE_URL = "https://api.media.test"
TENANT_ID = str(uuid4())
PERMALINKS_URL = f"{BASE_URL}/v1/manage/permalinks"


def _make_client() -> MediaLibraryClient:
    return MediaLibraryClient(base_url=BASE_URL, tenant_id=TENANT_ID, token="ml_test_x")


def _permalink(name: str, asset_id) -> dict:
    return {
        "name": name,
        "assetId": str(asset_id),
        "url": f"https://media.test/p/{name}",
        "createdAt": "2026-10-20T12:00:00Z",
        "updatedAt": "2026-10-20T12:05:00Z",
    }


@respx.mock
def test_set_puts_the_asset_id_under_the_name() -> None:
    asset_id = uuid4()
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["headers"] = request.headers
        return httpx.Response(200, json=_permalink("users/42/avatar", asset_id))

    route = respx.put(f"{PERMALINKS_URL}/users/42/avatar").mock(side_effect=handler)

    with _make_client() as client:
        permalink = client.permalinks.set("users/42/avatar", asset_id)

    assert route.called
    assert captured["body"] == {"assetId": str(asset_id)}
    assert "X-Tenant" not in captured["headers"]
    assert isinstance(permalink, Permalink)
    assert permalink.name == "users/42/avatar"
    assert permalink.asset_id == asset_id
    assert permalink.url == "https://media.test/p/users/42/avatar"
    assert permalink.updated_at > permalink.created_at


@respx.mock
def test_get_returns_the_permalink() -> None:
    asset_id = uuid4()
    respx.get(f"{PERMALINKS_URL}/logo.png").mock(
        return_value=httpx.Response(200, json=_permalink("logo.png", asset_id))
    )

    with _make_client() as client:
        assert client.permalinks.get("logo.png").asset_id == asset_id


@respx.mock
def test_delete_sends_delete() -> None:
    route = respx.delete(f"{PERMALINKS_URL}/users/42/avatar").mock(return_value=httpx.Response(204))

    with _make_client() as client:
        assert client.permalinks.delete("users/42/avatar") is None

    assert route.called


@pytest.mark.parametrize(
    ("method", "status", "error"),
    [
        ("get", 404, NotFoundError),
        ("delete", 404, NotFoundError),
        ("get", 403, AuthenticationError),
    ],
)
@respx.mock
def test_errors_map_to_typed_exceptions(method, status, error) -> None:
    respx.route(method=method.upper(), url=f"{PERMALINKS_URL}/x").mock(
        return_value=httpx.Response(status, json={"detail": "nope"})
    )

    with _make_client() as client, pytest.raises(error):
        getattr(client.permalinks, method)("x")


@pytest.mark.parametrize(("status", "error"), [(404, NotFoundError), (422, ValidationError)])
@respx.mock
def test_set_errors(status, error) -> None:
    respx.put(f"{PERMALINKS_URL}/x").mock(return_value=httpx.Response(status, json={"detail": "nope"}))

    with _make_client() as client, pytest.raises(error):
        client.permalinks.set("x", uuid4())


@pytest.mark.parametrize("name", ["", "a//b", "../x", ".x", "a b", "a/", "/a", "a%2Fb", "é", "a" * 256])
@respx.mock
def test_invalid_names_never_reach_the_server(name) -> None:
    with _make_client() as client:
        for call in (lambda: client.permalinks.set(name, uuid4()),
                     lambda: client.permalinks.get(name),
                     lambda: client.permalinks.delete(name)):
            with pytest.raises(ValueError):
                call()

    assert not respx.calls


@pytest.mark.parametrize("name", ["a", "users/42/avatar", "x.png", "_a-b/c.d", "a" * 255])
def test_valid_names(name) -> None:
    assert validate_permalink_name(name) == name


def test_permalink_url_uses_the_media_host() -> None:
    assert permalink_url("https://media.test/", "users/42/avatar") == "https://media.test/p/users/42/avatar"
    with pytest.raises(ValueError):
        permalink_url("https://media.test", "../x")
