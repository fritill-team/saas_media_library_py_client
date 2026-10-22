from __future__ import annotations

import json
from typing import Optional
from uuid import UUID, uuid4

import pytest

from pkg_media import (
    AssetUrlCache,
    DeliveryBatchResolveResult,
    EmbeddedMedia,
    MediaEmbedder,
)
from tests import delivery_fake


TENANT_ID = str(uuid4())

DOWNLOAD = "https://media.test/o/{id}?token=download"
HLS = "https://stream.test/s/stoken/{id}/master.m3u8"
DASH = "https://stream.test/s/stoken/{id}/manifest.mpd"
THUMB = "https://media.test/o/{id}/thumb-1024.webp"
THUMB_TOKENED = "https://media.test/o/{id}/thumb-1024.webp?token=thumb"
SIZES = {256: "https://media.test/o/{id}/256.webp", 512: "https://media.test/o/{id}/512.webp"}
# Links no denied user may see.
SECRETS = (DOWNLOAD, HLS, DASH, "https://media.test/o/{id}/720p.m3u8")


def _video(asset_id, *, thumb_expires: Optional[int] = None, status: str = "ready") -> dict:
    thumb = THUMB_TOKENED if thumb_expires else THUMB
    return {
        "id": str(asset_id), "kind": "video", "title": "Lesson", "status": status,
        "visibility": "restricted", "mimeType": "video/mp4", "downloadType": "gate",
        "downloadUrl": DOWNLOAD.format(id=asset_id), "downloadUrlExpiresIn": 3600,
        "hlsUrl": HLS.format(id=asset_id), "dashUrl": DASH.format(id=asset_id),
        "streamUrlExpiresIn": 14400,
        "renditions": [
            {"id": str(uuid4()), "renditionType": "hls", "status": "ready", "entrypoint": "master.m3u8",
             "variants": [
                 {"name": "720p", "width": 1280, "height": 720,
                  "previewUrl": SECRETS[3].format(id=asset_id), "previewUrlExpiresIn": 14400},
                 {"name": "1080p", "width": 1920, "height": 1080},
             ]},
            {"id": str(uuid4()), "renditionType": "thumbnail", "status": "ready", "entrypoint": "t.webp",
             "variants": [{"name": "default", "width": 120, "height": 90}],
             "previewUrl": thumb.format(id=asset_id), "previewUrlType": "gate",
             "previewUrlExpiresIn": thumb_expires},
        ],
    }


def _image(asset_id, *, public: bool = True) -> dict:
    expires = None if public else 3600
    suffix = "" if public else "?token=t"
    return {
        "id": str(asset_id), "kind": "image", "status": "ready",
        "visibility": "public" if public else "restricted", "mimeType": "image/png",
        "downloadType": "gate", "downloadUrl": DOWNLOAD.format(id=asset_id).split("?")[0] + suffix,
        "downloadUrlExpiresIn": expires,
        "renditions": [
            {"id": str(uuid4()), "renditionType": "thumbnail", "status": "ready", "entrypoint": "preview_512.webp",
             "variants": [
                 # Out of order, plus a second format at the same width: first one wins.
                 {"name": "thumb_512_webp", "width": 512, "height": 384,
                  "previewUrl": SIZES[512].format(id=asset_id) + suffix, "previewUrlExpiresIn": expires},
                 {"name": "thumb_256_webp", "width": 256, "height": 192,
                  "previewUrl": SIZES[256].format(id=asset_id) + suffix, "previewUrlExpiresIn": expires},
                 {"name": "thumb_256_avif", "width": 256, "height": 192,
                  "previewUrl": "https://media.test/o/x.avif" + suffix, "previewUrlExpiresIn": expires},
             ],
             "previewUrl": SIZES[512].format(id=asset_id) + suffix, "previewUrlExpiresIn": expires},
        ],
    }


def _ok(asset_id, asset: dict) -> dict:
    return {"assetId": str(asset_id), "status": "ok", "thumbnailUrl": None, "asset": asset, "error": None}


@pytest.fixture
def server():
    with delivery_fake.serve(lambda a: _ok(a, _video(a))) as fake:
        yield fake




def _allow(*ids):
    allowed = {str(a) for a in ids}
    return lambda user, asset_ids: [a for a in asset_ids if str(a) in allowed]


def _no_secrets(asset_id, media: EmbeddedMedia) -> None:
    text = json.dumps(media.to_json())
    for url in SECRETS:
        assert url.format(id=asset_id) not in text


# ── Mapping ────────────────────────────────────────────────────────────


def test_allowed_video_maps_every_link() -> None:
    asset_id = uuid4()
    result = DeliveryBatchResolveResult.model_validate(_ok(asset_id, _video(asset_id)))

    media = EmbeddedMedia.from_result(result)

    assert media.asset_id == asset_id
    assert media.kind == "video"
    assert media.url == DOWNLOAD.format(id=asset_id)
    assert media.thumbnail_url == THUMB.format(id=asset_id)
    assert media.poster == THUMB.format(id=asset_id)
    assert media.streams.hls == HLS.format(id=asset_id)
    assert media.streams.dash == DASH.format(id=asset_id)
    # Largest variant outside the (fixed-size) thumbnails.
    assert (media.width, media.height) == (1920, 1080)
    assert media.sources == []
    assert media.alt is None
    # Smallest expiry of the links carried: download 3600, streams 14400.
    assert media.expires_in == 3600
    assert media.tokened is True
    assert media.public is False


def test_json_is_camel_case_with_every_field() -> None:
    asset_id = uuid4()
    media = EmbeddedMedia.from_result(DeliveryBatchResolveResult.model_validate(_ok(asset_id, _video(asset_id))))

    data = media.to_json()

    assert set(data) == {
        "assetId", "kind", "url", "thumbnailUrl", "width", "height", "alt", "poster",
        "streams", "sources", "expiresIn", "public",
    }
    assert data["assetId"] == str(asset_id)
    assert data["streams"] == {"hls": HLS.format(id=asset_id), "dash": DASH.format(id=asset_id)}
    assert EmbeddedMedia.model_validate(data) == media


def test_public_image_is_tokenless_with_sources() -> None:
    asset_id = uuid4()
    media = EmbeddedMedia.from_result(DeliveryBatchResolveResult.model_validate(_ok(asset_id, _image(asset_id))))

    assert media.kind == "image"
    assert media.poster is None and media.streams is None
    assert [(s.width, s.height, s.url) for s in media.sources] == [
        (256, 192, SIZES[256].format(id=asset_id)),
        (512, 384, SIZES[512].format(id=asset_id)),
    ]
    assert (media.width, media.height) == (512, 384)
    assert media.expires_in is None
    assert media.public is True
    assert media.tokened is False


def test_restricted_image_sources_count_in_expiry() -> None:
    asset_id = uuid4()
    media = EmbeddedMedia.from_result(
        DeliveryBatchResolveResult.model_validate(_ok(asset_id, _image(asset_id, public=False)))
    )

    assert media.expires_in == 3600
    assert media.public is False


def test_shortest_expiry_wins() -> None:
    asset_id = uuid4()
    result = DeliveryBatchResolveResult.model_validate(_ok(asset_id, _video(asset_id, thumb_expires=900)))

    assert EmbeddedMedia.from_result(result).expires_in == 900


def test_asset_not_ready_has_no_url() -> None:
    asset_id = uuid4()
    asset = _video(asset_id, status="processing")
    asset.update(hlsUrl=None, dashUrl=None, streamUrlExpiresIn=None)
    media = EmbeddedMedia.from_result(DeliveryBatchResolveResult.model_validate(_ok(asset_id, asset)))

    assert media.url is None
    assert media.thumbnail_url == THUMB.format(id=asset_id)
    assert media.public is True


@pytest.mark.parametrize("kind,expected", [
    ("audio", "audio"), ("document", "document"), ("web_app", "other"), ("image_batch", "other"),
])
def test_kinds_outside_the_shape_are_other(kind, expected) -> None:
    asset_id = uuid4()
    asset = _image(asset_id)
    asset["kind"] = kind
    media = EmbeddedMedia.from_result(DeliveryBatchResolveResult.model_validate(_ok(asset_id, asset)))

    assert media.kind == expected


def test_not_found_is_empty() -> None:
    asset_id = uuid4()
    media = EmbeddedMedia.from_result(DeliveryBatchResolveResult(asset_id=asset_id, status="not_found"))

    assert media.url is None and media.thumbnail_url is None
    assert media.kind == "other"
    assert media.expires_in is None
    assert media.public is False


# ── MediaEmbedder ──────────────────────────────────────────────────────


def test_allowed_and_denied(server) -> None:
    allowed, denied = uuid4(), uuid4()
    embedder = MediaEmbedder(server.client(tenant_id=TENANT_ID), _allow(allowed))

    embeds = embedder.embed("alice", [allowed, denied])

    assert embeds[allowed].url == DOWNLOAD.format(id=allowed)
    assert embeds[str(allowed)] is embeds[allowed]
    teaser = embeds[denied]
    assert teaser.url is None
    assert teaser.streams is None and teaser.sources == []
    assert teaser.thumbnail_url == THUMB.format(id=denied)
    assert teaser.poster == THUMB.format(id=denied)
    assert teaser.kind == "video"
    assert teaser.width is None and teaser.height is None
    assert teaser.public is True and teaser.expires_in is None
    _no_secrets(denied, teaser)
    assert embeds.tokened is True
    assert embeds.cache_control == "private"


def test_denied_tokened_thumbnail_keeps_its_expiry(server) -> None:
    denied = uuid4()
    server.set(denied, _ok(denied, _video(denied, thumb_expires=3600)))
    embeds = MediaEmbedder(server.client(tenant_id=TENANT_ID), _allow()).embed("mallory", [denied])

    assert embeds[denied].thumbnail_url == THUMB_TOKENED.format(id=denied)
    assert embeds[denied].expires_in == 3600
    assert embeds.tokened is True


def test_denied_thumbnail_with_unknown_expiry_is_dropped(server) -> None:
    denied = uuid4()
    # Media refused the service itself: only an item-level thumbnail, no asset.
    server.set(denied, {"assetId": str(denied), "status": "forbidden",
                        "thumbnailUrl": THUMB.format(id=denied), "asset": None, "error": "no"})
    embeds = MediaEmbedder(server.client(tenant_id=TENANT_ID), _allow()).embed("mallory", [denied])

    assert embeds[denied].thumbnail_url is None
    assert embeds[denied].public is False


def test_denied_without_thumbnails_is_not_resolved(server) -> None:
    denied = uuid4()
    embeds = MediaEmbedder(server.client(tenant_id=TENANT_ID), _allow(), thumbnails=False).embed("mallory", [denied])

    assert server.requests == []
    assert embeds[denied].url is None and embeds[denied].thumbnail_url is None


def test_missing_asset_looks_like_denied(server) -> None:
    missing = uuid4()
    server.set(missing, {"assetId": str(missing), "status": "not_found", "asset": None})
    embeds = MediaEmbedder(server.client(tenant_id=TENANT_ID), _allow(missing)).embed("alice", [missing])

    assert embeds[missing].url is None
    assert embeds.tokened is False
    assert embeds.cache_control is None


def test_only_public_links_are_not_tokened(server) -> None:
    a, b = uuid4(), uuid4()
    server.set(a, _ok(a, _image(a)))
    server.set(b, _ok(b, _image(b)))
    embeds = MediaEmbedder(server.client(tenant_id=TENANT_ID), _allow(a, b)).embed("alice", [a, b])

    assert embeds.tokened is False
    assert all(m.public for m in embeds.values())


def test_none_and_duplicate_ids(server) -> None:
    a = uuid4()
    calls = []

    def can_access(user, ids):
        calls.append(list(ids))
        return ids

    embedder = MediaEmbedder(server.client(tenant_id=TENANT_ID), can_access)
    embeds = embedder.embed("alice", [a, None, str(a)])

    assert list(embeds) == [a]
    assert calls == [[a]]
    assert embeds.get(None) is None
    assert embeds.get(uuid4()) is None

    assert len(embedder.embed("alice", [None])) == 0
    assert calls == [[a]]


def test_cache_hit_makes_no_call(server) -> None:
    a = uuid4()
    clock = [0.0]
    embedder = MediaEmbedder(server.client(tenant_id=TENANT_ID), _allow(a), cache=AssetUrlCache(clock=lambda: clock[0]))

    first = embedder.embed("alice", [a])
    clock[0] = 100.0
    second = embedder.embed("bob", [a])

    assert len(server.requests) == 1
    assert second[a].url == first[a].url
    # The cache reports the time the links really have left.
    assert second[a].expires_in == first[a].expires_in - 100


def test_cache_false_always_calls(server) -> None:
    a = uuid4()
    embedder = MediaEmbedder(server.client(tenant_id=TENANT_ID), _allow(a), cache=False)
    assert embedder.cache is None

    embedder.embed("alice", [a])
    embedder.embed("alice", [a])

    assert len(server.requests) == 2


def test_shared_cache_is_used(server) -> None:
    cache = AssetUrlCache()
    a = uuid4()
    embedder = MediaEmbedder(server.client(tenant_id=TENANT_ID), _allow(a), cache=cache)
    embedder.embed("alice", [a])

    assert embedder.cache is cache
    assert cache.get(a, tenant_id=TENANT_ID) is not None


async def test_aembed(server) -> None:
    a = uuid4()
    embeds = await MediaEmbedder(server.client(tenant_id=TENANT_ID), _allow(a)).aembed("alice", [a])

    assert isinstance(embeds[a].asset_id, UUID)
    assert embeds[a].url == DOWNLOAD.format(id=a)
