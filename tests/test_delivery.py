from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
import respx

from saas_media_library import (
    AuthenticationError,
    DeliveryAPI,
    DeliveryBatchResolveResult,
    MediaLibraryClient,
    NotFoundError,
    ResolveStatus,
)


BASE_URL = "https://media.test"
TENANT_ID = str(uuid4())
DELIVERY_URL = f"{BASE_URL}/v1/delivery/assets"


def _make_client() -> MediaLibraryClient:
    return MediaLibraryClient(base_url=BASE_URL, tenant_id=TENANT_ID, token="t")


def _delivery_asset(asset_id, visibility: str = "restricted") -> dict:
    return {
        "id": str(asset_id),
        "kind": "video",
        "title": "Lesson 1",
        "status": "ready",
        "visibility": visibility,
        "mimeType": "video/mp4",
        "downloadType": "volume_token",
        "downloadUrl": f"https://media.test/v/{asset_id}?token=x",
        "downloadUrlExpiresIn": 3600,
        "renditions": [],
    }


def _resolve_handler(requests: list):
    """Echo a result per requested id, in request order."""

    def handler(request: httpx.Request) -> httpx.Response:
        ids = json.loads(request.content)["assetIds"]
        requests.append(ids)
        items = [
            {"assetId": aid, "status": "ok", "thumbnailUrl": f"https://cdn/{aid}.webp",
             "asset": _delivery_asset(aid)}
            for aid in ids
        ]
        return httpx.Response(200, json={"items": items, "total": len(items)})

    return handler


@respx.mock
def test_resolve_parses_all_statuses() -> None:
    ok_id, missing_id, locked_id, broken_id = uuid4(), uuid4(), uuid4(), uuid4()
    respx.post(f"{DELIVERY_URL}/resolve").mock(
        return_value=httpx.Response(200, json={
            "items": [
                {"assetId": str(ok_id), "status": "ok", "thumbnailUrl": "https://cdn/t.webp",
                 "asset": _delivery_asset(ok_id)},
                {"assetId": str(missing_id), "status": "not_found"},
                {"assetId": str(locked_id), "status": "forbidden", "thumbnailUrl": "https://cdn/l.webp",
                 "error": "Access denied"},
                {"assetId": str(broken_id), "status": "error", "error": "Blob not found for asset"},
            ],
            "total": 4,
        })
    )

    with _make_client() as client:
        items = client.delivery.resolve([ok_id, missing_id, locked_id, broken_id])

    assert all(isinstance(i, DeliveryBatchResolveResult) for i in items)
    assert [i.status for i in items] == [
        ResolveStatus.OK,
        ResolveStatus.NOT_FOUND,
        ResolveStatus.FORBIDDEN,
        ResolveStatus.ERROR,
    ]
    assert items[0].asset.download_url.endswith("token=x")
    assert items[0].thumbnail_url == "https://cdn/t.webp"
    assert items[1].asset is None and items[1].thumbnail_url is None
    assert items[2].thumbnail_url == "https://cdn/l.webp"
    assert items[2].error == "Access denied"
    assert items[3].error == "Blob not found for asset"


@respx.mock
def test_resolve_chunks_by_server_batch_size_and_keeps_order() -> None:
    requests: list = []
    respx.post(f"{DELIVERY_URL}/resolve").mock(side_effect=_resolve_handler(requests))
    ids = [uuid4() for _ in range(250)]

    with _make_client() as client:
        items = client.delivery.resolve(ids)

    assert [len(r) for r in requests] == [100, 100, 50]
    assert [i.asset_id for i in items] == ids


@respx.mock
def test_resolve_exact_batch_size_is_one_request() -> None:
    requests: list = []
    respx.post(f"{DELIVERY_URL}/resolve").mock(side_effect=_resolve_handler(requests))

    with _make_client() as client:
        client.delivery.resolve([uuid4() for _ in range(DeliveryAPI.RESOLVE_BATCH_SIZE)])

    assert len(requests) == 1


@respx.mock
def test_resolve_accepts_any_iterable_and_string_ids() -> None:
    requests: list = []
    respx.post(f"{DELIVERY_URL}/resolve").mock(side_effect=_resolve_handler(requests))
    ids = [str(uuid4()) for _ in range(3)]

    with _make_client() as client:
        items = client.delivery.resolve(i for i in ids)

    assert requests == [ids]
    assert [str(i.asset_id) for i in items] == ids


@respx.mock
def test_resolve_empty_input_makes_no_request() -> None:
    route = respx.post(f"{DELIVERY_URL}/resolve")

    with _make_client() as client:
        assert client.delivery.resolve([]) == []

    assert not route.called


@respx.mock
def test_resolve_error_in_later_batch_raises() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.Response(401, json={"detail": "token expired"})
        return _resolve_handler([])(request)

    respx.post(f"{DELIVERY_URL}/resolve").mock(side_effect=handler)

    with _make_client() as client, pytest.raises(AuthenticationError):
        client.delivery.resolve([uuid4() for _ in range(150)])


@respx.mock
def test_get() -> None:
    asset_id = uuid4()
    respx.get(f"{DELIVERY_URL}/{asset_id}").mock(
        return_value=httpx.Response(200, json=_delivery_asset(asset_id))
    )

    with _make_client() as client:
        asset = client.delivery.get(asset_id)

    assert asset.id == asset_id
    assert asset.download_type == "volume_token"


@respx.mock
def test_get_404_raises_not_found() -> None:
    asset_id = uuid4()
    respx.get(f"{DELIVERY_URL}/{asset_id}").mock(
        return_value=httpx.Response(404, json={"detail": f"Asset {asset_id} not found"})
    )

    with _make_client() as client, pytest.raises(NotFoundError):
        client.delivery.get(asset_id)


@respx.mock
def test_list_sends_filters() -> None:
    asset_id = uuid4()
    route = respx.get(DELIVERY_URL).mock(
        return_value=httpx.Response(200, json={
            "items": [_delivery_asset(asset_id, "public")],
            "total": 1,
            "page": 2,
            "pageSize": 5,
        })
    )

    with _make_client() as client:
        result = client.delivery.list(kind="video", status="ready", query="lesson", page=2, page_size=5)

    params = route.calls.last.request.url.params
    assert dict(params) == {"kind": "video", "status": "ready", "query": "lesson", "page": "2", "pageSize": "5"}
    assert result.total == 1
    assert result.items[0].id == asset_id


# ── AssetsAPI.delivery_* stay working (delegating to DeliveryAPI) ─────


@respx.mock
def test_assets_delivery_resolve_delegates_with_chunking() -> None:
    requests: list = []
    respx.post(f"{DELIVERY_URL}/resolve").mock(side_effect=_resolve_handler(requests))
    ids = [uuid4() for _ in range(101)]

    with _make_client() as client:
        items = client.assets.delivery_resolve(ids)

    assert [len(r) for r in requests] == [100, 1]
    assert [i.asset_id for i in items] == ids
    assert items[0].thumbnail_url is not None


@respx.mock
def test_assets_delivery_get_and_list() -> None:
    asset_id = uuid4()
    respx.get(f"{DELIVERY_URL}/{asset_id}").mock(
        return_value=httpx.Response(200, json=_delivery_asset(asset_id))
    )
    list_route = respx.get(DELIVERY_URL).mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0, "page": 1, "pageSize": 10})
    )

    with _make_client() as client:
        assert client.assets.delivery_get(asset_id).id == asset_id
        assert client.assets.delivery_list(kind="image").total == 0

    assert list_route.calls.last.request.url.params["kind"] == "image"
