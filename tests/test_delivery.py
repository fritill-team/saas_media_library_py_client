from __future__ import annotations

from uuid import uuid4

import grpc
import pytest

from pkg_media import (
    AssetUrlCache,
    AuthenticationError,
    DeliveryAPI,
    DeliveryBatchResolveResult,
    MediaLibraryClient,
    NotFoundError,
    ResolveStatus,
    UnimplementedError,
    thumbnail_url,
)
from tests import delivery_fake


TENANT_ID = str(uuid4())


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


def _ok(aid) -> dict:
    return {"assetId": aid, "status": "ok", "thumbnailUrl": f"https://cdn/{aid}.webp",
            "asset": _delivery_asset(aid)}


@pytest.fixture
def media():
    """The media library's gRPC Delivery: ``ok`` for every id unless set."""
    with delivery_fake.serve(_ok) as fake:
        yield fake


@pytest.fixture
def client(media):
    return media.client(tenant_id=TENANT_ID)


# ── HTTP has no delivery ───────────────────────────────────────────────


@pytest.mark.parametrize("call", [
    lambda c: c.delivery.resolve([uuid4()]),
    lambda c: c.delivery.get(uuid4()),
    lambda c: c.delivery.list(),
], ids=["resolve", "get", "list"])
def test_http_client_has_no_delivery(call) -> None:
    with MediaLibraryClient(base_url="https://media.test", token="t") as client:
        with pytest.raises(UnimplementedError, match="grpc"):
            call(client)


def test_resolve_parses_all_statuses(media, client) -> None:
    ok_id, missing_id, locked_id, broken_id = uuid4(), uuid4(), uuid4(), uuid4()
    media.set(ok_id, {"assetId": str(ok_id), "status": "ok", "thumbnailUrl": "https://cdn/t.webp",
                      "asset": _delivery_asset(ok_id)})
    media.set(missing_id, {"assetId": str(missing_id), "status": "not_found"})
    media.set(locked_id, {"assetId": str(locked_id), "status": "forbidden", "thumbnailUrl": "https://cdn/l.webp",
                          "error": "Access denied"})
    media.set(broken_id, {"assetId": str(broken_id), "status": "error", "error": "Blob not found for asset"})

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


def test_resolve_chunks_by_server_batch_size_and_keeps_order(media, client) -> None:
    ids = [uuid4() for _ in range(250)]

    items = client.delivery.resolve(ids)

    assert [len(r) for r in media.requests] == [100, 100, 50]
    assert [i.asset_id for i in items] == ids


def test_resolve_exact_batch_size_is_one_request(media, client) -> None:
    client.delivery.resolve([uuid4() for _ in range(DeliveryAPI.RESOLVE_BATCH_SIZE)])

    assert len(media.requests) == 1


def test_resolve_accepts_any_iterable_and_string_ids(media, client) -> None:
    ids = [str(uuid4()) for _ in range(3)]

    items = client.delivery.resolve(i for i in ids)

    assert media.requests == [ids]
    assert [str(i.asset_id) for i in items] == ids


def test_resolve_empty_input_makes_no_request(media, client) -> None:
    assert client.delivery.resolve([]) == []

    assert media.requests == []


def test_resolve_error_in_later_batch_raises(media, client) -> None:
    media.fail(None, grpc.StatusCode.UNAUTHENTICATED)

    with pytest.raises(AuthenticationError):
        client.delivery.resolve([uuid4() for _ in range(150)])

    assert [len(r) for r in media.requests] == [100, 50]


def test_get(client) -> None:
    asset_id = uuid4()

    asset = client.delivery.get(asset_id)

    assert asset.id == asset_id
    assert asset.download_type == "gate"


def test_get_404_raises_not_found(media, client) -> None:
    asset_id = uuid4()
    media.set(asset_id, None)

    with pytest.raises(NotFoundError):
        client.delivery.get(asset_id)


# ── resolve(..., cache=) ──────────────────────────────────────────────


def test_resolve_with_cache_requests_only_misses_and_keeps_order(media, client) -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    cache = AssetUrlCache()

    client.delivery.resolve([a], cache=cache)
    items = client.delivery.resolve([b, a, str(c), b], cache=cache)

    assert media.requests == [[str(a)], [str(b), str(c)]]
    assert [i.asset_id for i in items] == [b, a, c, b]
    assert all(i.status == ResolveStatus.OK for i in items)


def test_resolve_with_cache_stores_under_the_clients_tenant(client) -> None:
    asset_id = uuid4()
    cache = AssetUrlCache(clock=lambda: 0.0)

    [item] = client.delivery.resolve([asset_id], cache=cache)

    assert client.tenant_id == client.delivery.tenant_id == TENANT_ID
    assert cache.get(asset_id, tenant_id=TENANT_ID) == item
    assert cache.get(asset_id) is None


def test_resolve_with_cache_is_scoped_by_tenant(media) -> None:
    asset_id = uuid4()
    cache = AssetUrlCache()
    a = media.client(tenant_id="tenant-a")
    b = media.client(tenant_id="tenant-b")

    a.delivery.resolve([asset_id], cache=cache)
    b.delivery.resolve([asset_id], cache=cache)
    a.delivery.resolve([asset_id], cache=cache)

    assert len(media.requests) == 2
    # tenant_id only scopes the cache: no tenant is sent.
    assert not [m for m in media.metadata if "x-tenant" in m]


def test_resolve_with_cache_does_not_cache_other_statuses(media, client) -> None:
    missing = uuid4()
    media.set(missing, {"assetId": str(missing), "status": "not_found"})
    cache = AssetUrlCache()

    client.delivery.resolve([missing], cache=cache)
    [item] = client.delivery.resolve([missing], cache=cache)

    assert item.status == ResolveStatus.NOT_FOUND
    assert len(media.requests) == 2


def test_resolve_with_cache_fills_in_missing_results(media, client) -> None:
    asset_id = uuid4()
    media.set(asset_id, None)

    [item] = client.delivery.resolve([asset_id], cache=AssetUrlCache())

    assert item.asset_id == asset_id
    assert item.status == ResolveStatus.ERROR


def test_resolve_with_cache_all_hits_makes_no_request(media, client) -> None:
    cache = AssetUrlCache()
    ids = [uuid4(), uuid4()]

    client.delivery.resolve(ids, cache=cache)
    assert client.delivery.resolve([], cache=cache) == []
    client.delivery.resolve(ids[::-1], cache=cache)

    assert len(media.requests) == 1


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


def test_get_parses_stream_urls_and_gate_download_type(media, client) -> None:
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
    media.set(asset_id, {"assetId": str(asset_id), "status": "ok", "asset": body})

    asset = client.delivery.get(asset_id)

    assert asset.download_type == DownloadType.GATE == "gate"
    assert asset.renditions[0].preview_url_type == DownloadType.GATE
    assert asset.hls_url == body["hlsUrl"]
    assert asset.dash_url == body["dashUrl"]
    assert asset.stream_url_expires_in == 900
    dumped = asset.model_dump(mode="json", by_alias=True)
    assert dumped["hlsUrl"] == body["hlsUrl"] and dumped["streamUrlExpiresIn"] == 900


def test_gate_download_without_url_parses(media, client) -> None:
    asset_id = uuid4()
    body = {**_delivery_asset(asset_id), "downloadType": "gate",
            "downloadUrl": None, "downloadUrlExpiresIn": 0}
    media.set(asset_id, {"assetId": str(asset_id), "status": "ok", "asset": body})

    asset = client.delivery.get(asset_id)

    assert asset.download_type == "gate"
    assert asset.download_url is None


def test_stream_fields_default_to_none(client) -> None:
    asset_id = uuid4()

    asset = client.delivery.get(asset_id)

    assert asset.hls_url is None
    assert asset.dash_url is None
    assert asset.stream_url_expires_in is None


def test_resolve_items_carry_stream_urls(media, client) -> None:
    asset_id = uuid4()
    asset = {**_delivery_asset(asset_id), "hlsUrl": "https://stream.test/h.m3u8",
             "dashUrl": None, "streamUrlExpiresIn": 600}
    media.set(asset_id, {"assetId": str(asset_id), "status": "ok", "asset": asset})

    [item] = client.delivery.resolve([asset_id])

    assert item.asset.hls_url == "https://stream.test/h.m3u8"
    assert item.asset.dash_url is None
    assert item.asset.stream_url_expires_in == 600


def test_download_type_values() -> None:
    from pkg_media import DownloadType

    assert [t.value for t in DownloadType] == ["gate", "unavailable"]


def test_get_unavailable_asset_has_no_download_url(media, client) -> None:
    from pkg_media import DownloadType

    asset_id = uuid4()
    body = {**_delivery_asset(asset_id), "downloadType": "unavailable",
            "downloadUrl": None, "downloadUrlExpiresIn": None}
    media.set(asset_id, {"assetId": str(asset_id), "status": "ok", "asset": body})

    asset = client.delivery.get(asset_id)

    assert asset.download_type == DownloadType.UNAVAILABLE
    assert asset.download_url is None
