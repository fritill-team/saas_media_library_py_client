from __future__ import annotations

import json
import logging
from typing import Dict, List
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from saas_media_library import (
    AccessGate,
    AssetUrlCache,
    AuthenticationError,
    DeliveryBatchResolveResult,
    MediaLibraryClient,
    ResolveStatus,
)


BASE_URL = "https://media.test"
TENANT_ID = str(uuid4())
RESOLVE_URL = f"{BASE_URL}/v1/delivery/assets/resolve"

THUMB_RENDITION_URL = "https://cdn.test/{id}/thumb-256.webp"
ITEM_THUMB_URL = "https://cdn.test/{id}/thumb.webp"
# Every link of an asset that is not a thumbnail: none may reach a denied user.
SECRET_URLS = (
    "https://media.test/v/{id}?token=download",
    "https://media.test/hls/{id}/master.m3u8?token=hls",
    "https://media.test/hls/{id}/720p.m3u8?token=v720",
    "https://media.test/hls/{id}/1080p.m3u8?token=v1080",
    "https://media.test/s3/{id}/preview.mp4?sig=preview",
)


def _make_client() -> MediaLibraryClient:
    return MediaLibraryClient(base_url=BASE_URL, tenant_id=TENANT_ID, token="service-token")


def _asset_json(asset_id, *, thumbnail: bool = True) -> dict:
    download, hls, v720, v1080, preview = (u.format(id=asset_id) for u in SECRET_URLS)
    renditions = [
        {
            "id": str(uuid4()),
            "renditionType": "hls",
            "status": "ready",
            "entrypoint": "master.m3u8",
            "durationSeconds": 12.5,
            "variants": [
                {"name": "720p", "width": 1280, "height": 720, "bitrateKbps": 2500,
                 "previewUrl": v720, "previewUrlType": "volume_token", "previewUrlExpiresIn": 3600},
                {"name": "1080p", "width": 1920, "height": 1080, "bitrateKbps": 5000,
                 "previewUrl": v1080, "previewUrlType": "volume_token", "previewUrlExpiresIn": 3600},
            ],
            "language": None,
            "previewUrl": hls,
            "previewUrlType": "volume_token",
            "previewUrlExpiresIn": 3600,
        },
        {
            "id": str(uuid4()),
            "renditionType": "preview",
            "status": "ready",
            "entrypoint": "preview.mp4",
            "durationSeconds": None,
            "variants": [],
            "language": "en",
            "previewUrl": preview,
            "previewUrlType": "presign",
            "previewUrlExpiresIn": 900,
        },
    ]
    if thumbnail:
        renditions.append({
            "id": str(uuid4()),
            "renditionType": "thumbnail",
            "status": "ready",
            "entrypoint": "thumb-1024.webp",
            "durationSeconds": None,
            "variants": [
                {"name": "256", "width": 256, "height": None, "bitrateKbps": None,
                 "previewUrl": THUMB_RENDITION_URL.format(id=asset_id), "previewUrlType": "cdn",
                 "previewUrlExpiresIn": None},
            ],
            "language": None,
            "previewUrl": THUMB_RENDITION_URL.format(id=asset_id),
            "previewUrlType": "cdn",
            "previewUrlExpiresIn": None,
        })
    return {
        "id": str(asset_id),
        "kind": "video",
        "title": "Lesson",
        "status": "ready",
        "visibility": "restricted",
        "mimeType": "video/mp4",
        "downloadType": "volume_token",
        "downloadUrl": download,
        "downloadUrlExpiresIn": 3600,
        "renditions": renditions,
    }


def _ok_item(asset_id, **kwargs) -> dict:
    return {
        "assetId": str(asset_id),
        "status": "ok",
        "thumbnailUrl": ITEM_THUMB_URL.format(id=asset_id),
        "asset": _asset_json(asset_id, **kwargs),
        "error": None,
    }


class FakeServer:
    """Answers POST /delivery/assets/resolve from a per-id table (default: ok)."""

    def __init__(self) -> None:
        self.items: Dict[str, dict] = {}
        self.requests: List[List[str]] = []

    def set(self, asset_id, item: dict) -> None:
        self.items[str(asset_id)] = item

    def __call__(self, request: httpx.Request) -> httpx.Response:
        ids = json.loads(request.content)["assetIds"]
        self.requests.append(ids)
        items = [self.items.get(aid) or _ok_item(aid) for aid in ids]
        return httpx.Response(200, json={"items": items, "total": len(items)})


@pytest.fixture
def server():
    fake = FakeServer()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(RESOLVE_URL).mock(side_effect=fake)
        yield fake


class Recorder:
    """can_access that allows a fixed set of ids and records its calls."""

    def __init__(self, allowed=()) -> None:
        self.allowed = {str(a) for a in allowed}
        self.calls: list = []

    def __call__(self, user, asset_ids):
        self.calls.append((user, list(asset_ids)))
        return [a for a in asset_ids if str(a) in self.allowed]


def _assert_no_secret_urls(asset_id, data) -> None:
    text = json.dumps(data)
    for url in SECRET_URLS:
        assert url.format(id=asset_id) not in text


# ── Allowed / denied ───────────────────────────────────────────────────


def test_allowed_asset_gets_the_service_result(server) -> None:
    asset_id = uuid4()
    gate = AccessGate(_make_client(), Recorder([asset_id]))

    [result] = gate.resolve("alice", [asset_id])

    assert result.status == ResolveStatus.OK
    assert result.asset.download_url == SECRET_URLS[0].format(id=asset_id)
    assert result.thumbnail_url == ITEM_THUMB_URL.format(id=asset_id)


def test_denied_asset_is_forbidden_with_only_a_thumbnail(server) -> None:
    asset_id = uuid4()
    gate = AccessGate(_make_client(), Recorder())

    [result] = gate.resolve("mallory", [asset_id])

    assert result.status == ResolveStatus.FORBIDDEN
    assert result.asset is None
    assert result.thumbnail_url == THUMB_RENDITION_URL.format(id=asset_id)
    assert result.error == AccessGate.DENIED_ERROR
    _assert_no_secret_urls(asset_id, result.model_dump(mode="json"))
    _assert_no_secret_urls(asset_id, gate.to_payload([result]))


def test_denied_asset_without_thumbnail_rendition_uses_item_thumbnail(server) -> None:
    asset_id = uuid4()
    server.set(asset_id, _ok_item(asset_id, thumbnail=False))
    gate = AccessGate(_make_client(), Recorder())

    [result] = gate.resolve("mallory", [asset_id])

    assert result.thumbnail_url == ITEM_THUMB_URL.format(id=asset_id)
    _assert_no_secret_urls(asset_id, gate.to_payload([result]))


def test_denied_asset_without_any_thumbnail(server) -> None:
    asset_id = uuid4()
    item = _ok_item(asset_id, thumbnail=False)
    item["thumbnailUrl"] = None
    server.set(asset_id, item)
    gate = AccessGate(_make_client(), Recorder())

    [result] = gate.resolve("mallory", [asset_id])

    assert result.status == ResolveStatus.FORBIDDEN
    assert result.thumbnail_url is None
    assert result.asset is None


def test_denied_result_from_cache_never_leaks(server) -> None:
    asset_id = uuid4()
    cache = AssetUrlCache()
    can_access = Recorder([asset_id])
    gate = AccessGate(_make_client(), can_access, cache=cache)
    gate.resolve("alice", [asset_id])  # warms the cache with the full result

    can_access.allowed.clear()
    [result] = gate.resolve("mallory", [asset_id])

    assert len(server.requests) == 1
    assert result.status == ResolveStatus.FORBIDDEN and result.asset is None
    _assert_no_secret_urls(asset_id, gate.to_payload([result]))
    # The cached entry still serves entitled users.
    assert cache.get(asset_id).asset.download_url is not None


@pytest.mark.parametrize("status", ["not_found", "error", "forbidden", "some_new_status"])
def test_denied_asset_never_leaks_whatever_the_service_says(server, status) -> None:
    asset_id = uuid4()
    item = _ok_item(asset_id)
    item["status"] = status  # even with an asset attached
    server.set(asset_id, item)
    gate = AccessGate(_make_client(), Recorder())

    [result] = gate.resolve("mallory", [asset_id])

    assert result.asset is None
    assert result.status == ("not_found" if status == "not_found" else "forbidden")
    _assert_no_secret_urls(asset_id, gate.to_payload([result]))


# ── Pass-through statuses ──────────────────────────────────────────────


@pytest.mark.parametrize("allowed", [True, False])
def test_not_found_passes_through(server, allowed) -> None:
    asset_id = uuid4()
    server.set(asset_id, {"assetId": str(asset_id), "status": "not_found"})
    gate = AccessGate(_make_client(), Recorder([asset_id] if allowed else []))

    [result] = gate.resolve("u", [asset_id])

    assert result.status == ResolveStatus.NOT_FOUND
    assert result.asset is None and result.thumbnail_url is None


def test_error_passes_through_for_allowed_asset(server) -> None:
    asset_id = uuid4()
    server.set(asset_id, {"assetId": str(asset_id), "status": "error",
                          "thumbnailUrl": ITEM_THUMB_URL.format(id=asset_id),
                          "error": "Blob not found for asset"})
    gate = AccessGate(_make_client(), Recorder([asset_id]))

    [result] = gate.resolve("u", [asset_id])

    assert result.status == ResolveStatus.ERROR
    assert result.error == "Blob not found for asset"


def test_error_for_denied_asset_is_forbidden(server) -> None:
    # As on the server: the access check comes before the blob lookup.
    asset_id = uuid4()
    server.set(asset_id, {"assetId": str(asset_id), "status": "error",
                          "thumbnailUrl": ITEM_THUMB_URL.format(id=asset_id),
                          "error": "Blob not found for asset"})
    gate = AccessGate(_make_client(), Recorder())

    [result] = gate.resolve("u", [asset_id])

    assert result.status == ResolveStatus.FORBIDDEN
    assert result.thumbnail_url == ITEM_THUMB_URL.format(id=asset_id)
    assert result.error == AccessGate.DENIED_ERROR


def test_service_forbidden_for_allowed_asset_warns_about_token(server, caplog) -> None:
    asset_id = uuid4()
    server.set(asset_id, {"assetId": str(asset_id), "status": "forbidden",
                          "thumbnailUrl": ITEM_THUMB_URL.format(id=asset_id),
                          "error": "RESTRICTED asset is only available through the owning application"})
    gate = AccessGate(_make_client(), Recorder([asset_id]))

    with caplog.at_level(logging.WARNING, logger="saas_media_library.gate"):
        [result] = gate.resolve("u", [asset_id])

    assert result.status == ResolveStatus.FORBIDDEN and result.asset is None
    assert result.thumbnail_url == ITEM_THUMB_URL.format(id=asset_id)
    assert "service token" in caplog.text


def test_missing_item_in_server_response_becomes_error() -> None:
    asset_id = uuid4()
    with respx.mock:
        respx.post(RESOLVE_URL).mock(return_value=httpx.Response(200, json={"items": [], "total": 0}))
        [result] = AccessGate(_make_client(), Recorder([asset_id])).resolve("u", [asset_id])

    assert result.status == ResolveStatus.ERROR
    assert result.asset_id == asset_id


# ── Order, duplicates, can_access contract ─────────────────────────────


def test_results_keep_input_order_with_duplicates(server) -> None:
    ok_id, denied_id, missing_id = uuid4(), uuid4(), uuid4()
    server.set(missing_id, {"assetId": str(missing_id), "status": "not_found"})
    can_access = Recorder([ok_id])
    gate = AccessGate(_make_client(), can_access)
    requested = [denied_id, ok_id, str(missing_id), ok_id, denied_id]

    results = gate.resolve("alice", requested)

    assert [r.asset_id for r in results] == [UUID(str(a)) for a in requested]
    assert [r.status for r in results] == ["forbidden", "ok", "not_found", "ok", "forbidden"]
    # can_access and the media library each see every id once.
    assert can_access.calls == [("alice", [denied_id, ok_id, missing_id])]
    assert server.requests == [[str(denied_id), str(ok_id), str(missing_id)]]


def test_can_access_may_return_strings_and_ignored_extra_ids(server) -> None:
    a, b = uuid4(), uuid4()
    gate = AccessGate(_make_client(), lambda user, ids: {str(a), str(uuid4())})

    results = gate.resolve("u", [a, b])

    assert [r.status for r in results] == ["ok", "forbidden"]


def test_can_access_generator_is_accepted(server) -> None:
    a = uuid4()
    gate = AccessGate(_make_client(), lambda user, ids: (i for i in ids))

    assert gate.resolve("u", [a])[0].status == ResolveStatus.OK


@pytest.mark.parametrize("bad", [None, True, "not-a-list"])
def test_can_access_must_return_ids(server, bad) -> None:
    gate = AccessGate(_make_client(), lambda user, ids: bad)

    with pytest.raises(TypeError, match="can_access"):
        gate.resolve("u", [uuid4()])

    assert server.requests == []


def test_empty_input_calls_nothing(server) -> None:
    can_access = Recorder()
    gate = AccessGate(_make_client(), can_access)

    assert gate.resolve("u", []) == []
    assert can_access.calls == [] and server.requests == []


def test_invalid_id_raises_value_error(server) -> None:
    with pytest.raises(ValueError):
        AccessGate(_make_client(), Recorder()).resolve("u", ["not-a-uuid"])


def test_resolve_one(server) -> None:
    asset_id = uuid4()
    gate = AccessGate(_make_client(), Recorder([asset_id]))

    result = gate.resolve_one("u", str(asset_id))

    assert isinstance(result, DeliveryBatchResolveResult)
    assert result.asset_id == asset_id and result.status == ResolveStatus.OK


def test_more_than_one_server_batch(server) -> None:
    ids = [uuid4() for _ in range(150)]
    gate = AccessGate(_make_client(), Recorder(ids[::2]))

    results = gate.resolve("u", ids)

    assert [len(r) for r in server.requests] == [100, 50]
    assert [r.asset_id for r in results] == ids
    assert [r.status for r in results[:4]] == ["ok", "forbidden", "ok", "forbidden"]


def test_media_library_errors_propagate() -> None:
    with respx.mock:
        respx.post(RESOLVE_URL).mock(return_value=httpx.Response(401, json={"detail": "expired"}))
        gate = AccessGate(_make_client(), Recorder())
        with pytest.raises(AuthenticationError):
            gate.resolve("u", [uuid4()])


# ── Cache ──────────────────────────────────────────────────────────────


def test_cache_serves_every_entitled_user(server) -> None:
    a, b = uuid4(), uuid4()
    gate = AccessGate(_make_client(), Recorder([a, b]), cache=AssetUrlCache())

    first = gate.resolve("alice", [a, b])
    second = gate.resolve("bob", [b, a])

    assert len(server.requests) == 1
    assert [r.asset.download_url for r in second] == [first[1].asset.download_url,
                                                      first[0].asset.download_url]


def test_cache_only_requests_missing_ids(server) -> None:
    a, b = uuid4(), uuid4()
    gate = AccessGate(_make_client(), Recorder([a, b]), cache=AssetUrlCache())
    gate.resolve("alice", [a])

    gate.resolve("alice", [a, b])

    assert server.requests == [[str(a)], [str(b)]]


def test_uncacheable_results_are_requested_again(server) -> None:
    missing = uuid4()
    server.set(missing, {"assetId": str(missing), "status": "not_found"})
    gate = AccessGate(_make_client(), Recorder([missing]), cache=AssetUrlCache())

    gate.resolve("u", [missing])
    gate.resolve("u", [missing])

    assert len(server.requests) == 2


def test_cached_result_reports_remaining_expiry(server) -> None:
    clock = [0.0]
    asset_id = uuid4()
    gate = AccessGate(_make_client(), Recorder([asset_id]),
                      cache=AssetUrlCache(clock=lambda: clock[0]))
    gate.resolve("alice", [asset_id])

    clock[0] = 600
    [result] = gate.resolve("bob", [asset_id])

    assert result.asset.download_url_expires_in == 3000
    assert len(server.requests) == 1


# ── Payload ────────────────────────────────────────────────────────────


def test_to_payload_matches_server_response_exactly(server) -> None:
    ok_id, missing_id, broken_id = uuid4(), uuid4(), uuid4()
    body_items = [
        _ok_item(ok_id),
        {"assetId": str(missing_id), "status": "not_found", "thumbnailUrl": None,
         "asset": None, "error": None},
        {"assetId": str(broken_id), "status": "error", "thumbnailUrl": None,
         "asset": None, "error": "Blob not found for asset"},
    ]
    for item in body_items:
        server.set(item["assetId"], item)
    gate = AccessGate(_make_client(), Recorder([ok_id, missing_id, broken_id]))

    payload = gate.to_payload(gate.resolve("u", [ok_id, missing_id, broken_id]))

    assert payload == {"items": body_items, "total": 3}
    assert json.loads(json.dumps(payload)) == payload


def test_to_payload_for_denied_item(server) -> None:
    asset_id = uuid4()
    gate = AccessGate(_make_client(), Recorder())

    payload = gate.to_payload(gate.resolve("u", [asset_id]))

    assert payload == {
        "items": [{
            "assetId": str(asset_id),
            "status": "forbidden",
            "thumbnailUrl": THUMB_RENDITION_URL.format(id=asset_id),
            "asset": None,
            "error": "Access denied",
        }],
        "total": 1,
    }


def test_to_payload_empty() -> None:
    assert AccessGate.to_payload([]) == {"items": [], "total": 0}
