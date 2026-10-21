from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
import respx

from pkg_media import (
    AssetUrlCache,
    AuthenticationError,
    DeliveryAPI,
    DeliveryBatchResolveResult,
    MediaLibraryClient,
    NotFoundError,
    ResolveStatus,
    thumbnail_url,
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
        "downloadType": "gate",
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
    assert asset.download_type == "gate"


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


# ── resolve(..., cache=) ──────────────────────────────────────────────


@respx.mock
def test_resolve_with_cache_requests_only_misses_and_keeps_order() -> None:
    requests: list = []
    respx.post(f"{DELIVERY_URL}/resolve").mock(side_effect=_resolve_handler(requests))
    a, b, c = uuid4(), uuid4(), uuid4()
    cache = AssetUrlCache()

    with _make_client() as client:
        client.delivery.resolve([a], cache=cache)
        items = client.delivery.resolve([b, a, str(c), b], cache=cache)

    assert requests == [[str(a)], [str(b), str(c)]]
    assert [i.asset_id for i in items] == [b, a, c, b]
    assert all(i.status == ResolveStatus.OK for i in items)


@respx.mock
def test_resolve_with_cache_stores_under_the_clients_tenant() -> None:
    requests: list = []
    respx.post(f"{DELIVERY_URL}/resolve").mock(side_effect=_resolve_handler(requests))
    asset_id = uuid4()
    cache = AssetUrlCache(clock=lambda: 0.0)

    with _make_client() as client:
        [item] = client.delivery.resolve([asset_id], cache=cache)

    assert client.tenant_id == client.delivery.tenant_id == TENANT_ID
    assert cache.get(asset_id, tenant_id=TENANT_ID) == item
    assert cache.get(asset_id) is None


@respx.mock
def test_resolve_with_cache_is_scoped_by_tenant() -> None:
    requests: list = []
    respx.post(f"{DELIVERY_URL}/resolve").mock(side_effect=_resolve_handler(requests))
    asset_id = uuid4()
    cache = AssetUrlCache()

    with MediaLibraryClient(base_url=BASE_URL, tenant_id="tenant-a", token="t") as a, \
            MediaLibraryClient(base_url=BASE_URL, tenant_id="tenant-b", token="t") as b:
        a.delivery.resolve([asset_id], cache=cache)
        b.delivery.resolve([asset_id], cache=cache)
        a.delivery.resolve([asset_id], cache=cache)

    assert len(requests) == 2
    tenants = [call.request.headers["X-Tenant"] for call in respx.calls]
    assert tenants == ["tenant-a", "tenant-b"]


@respx.mock
def test_resolve_with_cache_does_not_cache_other_statuses() -> None:
    missing = uuid4()
    route = respx.post(f"{DELIVERY_URL}/resolve").mock(return_value=httpx.Response(200, json={
        "items": [{"assetId": str(missing), "status": "not_found"}], "total": 1,
    }))
    cache = AssetUrlCache()

    with _make_client() as client:
        client.delivery.resolve([missing], cache=cache)
        [item] = client.delivery.resolve([missing], cache=cache)

    assert item.status == ResolveStatus.NOT_FOUND
    assert route.call_count == 2


@respx.mock
def test_resolve_with_cache_fills_in_missing_results() -> None:
    respx.post(f"{DELIVERY_URL}/resolve").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0}))
    asset_id = uuid4()

    with _make_client() as client:
        [item] = client.delivery.resolve([asset_id], cache=AssetUrlCache())

    assert item.asset_id == asset_id
    assert item.status == ResolveStatus.ERROR


@respx.mock
def test_resolve_with_cache_all_hits_makes_no_request() -> None:
    requests: list = []
    route = respx.post(f"{DELIVERY_URL}/resolve").mock(side_effect=_resolve_handler(requests))
    cache = AssetUrlCache()
    ids = [uuid4(), uuid4()]

    with _make_client() as client:
        client.delivery.resolve(ids, cache=cache)
        assert client.delivery.resolve([], cache=cache) == []
        client.delivery.resolve(ids[::-1], cache=cache)

    assert route.call_count == 1


# ── thumbnail_url ─────────────────────────────────────────────────────


def _result_with_renditions(asset_id, renditions, thumbnail=None) -> DeliveryBatchResolveResult:
    asset = _delivery_asset(asset_id)
    asset["renditions"] = renditions
    return DeliveryBatchResolveResult.model_validate({
        "assetId": str(asset_id), "status": "ok", "thumbnailUrl": thumbnail, "asset": asset,
    })


def _rendition(rendition_type: str, url) -> dict:
    return {"id": str(uuid4()), "renditionType": rendition_type, "status": "ready",
            "entrypoint": "x", "previewUrl": url}


def test_thumbnail_url_prefers_the_thumbnail_rendition() -> None:
    result = _result_with_renditions(
        uuid4(),
        [_rendition("hls", "https://media.test/hls?token=secret"),
         _rendition("thumbnail", "https://cdn/thumb-256.webp")],
        thumbnail="https://cdn/item-thumb.webp",
    )

    assert thumbnail_url(result) == "https://cdn/thumb-256.webp"


def test_thumbnail_url_falls_back_to_the_items_thumbnail() -> None:
    result = _result_with_renditions(
        uuid4(), [_rendition("thumbnail", None), _rendition("hls", "https://media.test/hls?token=secret")],
        thumbnail="https://cdn/item-thumb.webp",
    )
    forbidden = DeliveryBatchResolveResult.model_validate({
        "assetId": str(uuid4()), "status": "forbidden", "thumbnailUrl": "https://cdn/locked.webp",
    })

    assert thumbnail_url(result) == "https://cdn/item-thumb.webp"
    assert thumbnail_url(forbidden) == "https://cdn/locked.webp"


def test_thumbnail_url_is_never_another_link() -> None:
    asset_id = uuid4()
    result = _result_with_renditions(asset_id, [_rendition("hls", "https://media.test/hls?token=secret")])

    assert thumbnail_url(result) is None


# ── Stream URLs and gate links ─────────────────────────────────────────


@respx.mock
def test_get_parses_stream_urls_and_gate_download_type() -> None:
    from pkg_media import DownloadType

    asset_id = uuid4()
    body = {
        **_delivery_asset(asset_id),
        "downloadType": "gate",
        "downloadUrl": f"https://stream.test/o/{asset_id}/orig.mp4?token=x",
        "hlsUrl": f"https://stream.test/s/tok/{asset_id}/master.m3u8",
        "dashUrl": f"https://stream.test/s/tok/{asset_id}/manifest.mpd",
        "streamUrlExpiresIn": 900,
        "renditions": [{
            "id": str(uuid4()), "renditionType": "thumbnail", "status": "ready",
            "entrypoint": "t.webp", "previewUrl": "https://media.test/o/t.webp",
            "previewUrlType": "gate", "previewUrlExpiresIn": 0,
        }],
    }
    respx.get(f"{DELIVERY_URL}/{asset_id}").mock(return_value=httpx.Response(200, json=body))

    with _make_client() as client:
        asset = client.delivery.get(asset_id)

    assert asset.download_type == DownloadType.GATE == "gate"
    assert asset.renditions[0].preview_url_type == DownloadType.GATE
    assert asset.hls_url == body["hlsUrl"]
    assert asset.dash_url == body["dashUrl"]
    assert asset.stream_url_expires_in == 900
    dumped = asset.model_dump(mode="json", by_alias=True)
    assert dumped["hlsUrl"] == body["hlsUrl"] and dumped["streamUrlExpiresIn"] == 900


@respx.mock
def test_gate_download_without_url_parses() -> None:
    asset_id = uuid4()
    body = {**_delivery_asset(asset_id), "downloadType": "gate",
            "downloadUrl": None, "downloadUrlExpiresIn": 0}
    respx.get(f"{DELIVERY_URL}/{asset_id}").mock(return_value=httpx.Response(200, json=body))

    with _make_client() as client:
        asset = client.delivery.get(asset_id)

    assert asset.download_type == "gate"
    assert asset.download_url is None


@respx.mock
def test_stream_fields_default_to_none() -> None:
    asset_id = uuid4()
    respx.get(f"{DELIVERY_URL}/{asset_id}").mock(
        return_value=httpx.Response(200, json=_delivery_asset(asset_id))
    )

    with _make_client() as client:
        asset = client.delivery.get(asset_id)

    assert asset.hls_url is None
    assert asset.dash_url is None
    assert asset.stream_url_expires_in is None


@respx.mock
def test_resolve_items_carry_stream_urls() -> None:
    asset_id = uuid4()
    asset = {**_delivery_asset(asset_id), "hlsUrl": "https://stream.test/h.m3u8",
             "dashUrl": None, "streamUrlExpiresIn": 600}
    respx.post(f"{DELIVERY_URL}/resolve").mock(return_value=httpx.Response(200, json={
        "items": [{"assetId": str(asset_id), "status": "ok", "asset": asset}], "total": 1,
    }))

    with _make_client() as client:
        [item] = client.delivery.resolve([asset_id])

    assert item.asset.hls_url == "https://stream.test/h.m3u8"
    assert item.asset.dash_url is None
    assert item.asset.stream_url_expires_in == 600


def test_download_type_values() -> None:
    from pkg_media import DownloadType

    assert [t.value for t in DownloadType] == ["gate", "unavailable"]


@respx.mock
def test_get_unavailable_asset_has_no_download_url() -> None:
    from pkg_media import DownloadType

    asset_id = uuid4()
    body = {**_delivery_asset(asset_id), "downloadType": "unavailable",
            "downloadUrl": None, "downloadUrlExpiresIn": None}
    respx.get(f"{DELIVERY_URL}/{asset_id}").mock(return_value=httpx.Response(200, json=body))

    with _make_client() as client:
        asset = client.delivery.get(asset_id)

    assert asset.download_type == DownloadType.UNAVAILABLE
    assert asset.download_url is None
