from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import respx

from saas_media_library import MediaLibraryClient, MediaLibraryError, Visibility


BASE_URL = "https://media.test"
TENANT_ID = str(uuid4())


def _make_client() -> MediaLibraryClient:
    return MediaLibraryClient(base_url=BASE_URL, tenant_id=TENANT_ID, token="t")


def _asset_response(asset_id, visibility: str) -> dict:
    return {
        "id": str(asset_id),
        "kind": "image",
        "title": "t",
        "originalFilename": "x.png",
        "mimeType": "image/png",
        "sizeBytes": 1,
        "hashSha256": "h",
        "status": "ready",
        "visibility": visibility,
        "createdAt": "2026-05-08T00:00:00",
        "renditions": [],
    }


@respx.mock
def test_update_visibility_returns_parsed_asset() -> None:
    asset_id = uuid4()
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, json=_asset_response(asset_id, "restricted"))

    respx.patch(f"{BASE_URL}/v1/manage/assets/{asset_id}").mock(side_effect=handler)

    with _make_client() as client:
        asset = client.assets.update_visibility(asset_id, Visibility.RESTRICTED)

    assert captured["body"] == {"visibility": "restricted"}
    assert asset.visibility == Visibility.RESTRICTED


@respx.mock
def test_update_visibility_400_raises_validation_error() -> None:
    asset_id = uuid4()
    respx.patch(f"{BASE_URL}/v1/manage/assets/{asset_id}").mock(
        return_value=httpx.Response(
            400,
            json={"detail": "PUBLIC visibility transitions are not yet supported"},
        )
    )

    with _make_client() as client, pytest.raises(MediaLibraryError) as excinfo:
        client.assets.update_visibility(asset_id, Visibility.PUBLIC)

    assert excinfo.value.status_code == 400
    assert "PUBLIC" in str(excinfo.value)


@respx.mock
def test_manage_asset_parses_stream_urls() -> None:
    asset_id = uuid4()
    body = {**_asset_response(asset_id, "private"), "kind": "video",
            "hlsUrl": "https://stream.test/s/t/master.m3u8",
            "dashUrl": "https://stream.test/s/t/manifest.mpd",
            "streamUrlExpiresIn": 1800}
    respx.get(f"{BASE_URL}/v1/manage/assets/{asset_id}").mock(
        return_value=httpx.Response(200, json=body)
    )

    with _make_client() as client:
        asset = client.assets.get(asset_id)

    assert asset.hls_url == body["hlsUrl"]
    assert asset.dash_url == body["dashUrl"]
    assert asset.stream_url_expires_in == 1800


@respx.mock
def test_manage_asset_stream_urls_default_to_none() -> None:
    asset_id = uuid4()
    respx.get(f"{BASE_URL}/v1/manage/assets/{asset_id}").mock(
        return_value=httpx.Response(200, json=_asset_response(asset_id, "private"))
    )

    with _make_client() as client:
        asset = client.assets.get(asset_id)

    assert (asset.hls_url, asset.dash_url, asset.stream_url_expires_in) == (None, None, None)


@respx.mock
def test_get_parses_preview_urls_of_asset_renditions_and_variants() -> None:
    asset_id = uuid4()
    url = "https://media.test/o/x?token=t"
    preview = {"previewUrl": url, "previewUrlType": "gate", "previewUrlExpiresIn": 900}
    body = {
        **_asset_response(asset_id, "private"),
        **preview,
        "renditions": [{
            "id": str(uuid4()),
            "renditionType": "thumbnail",
            "entrypoint": "thumb.webp",
            "status": "ready",
            "createdAt": "2026-05-08T00:00:00",
            "updatedAt": "2026-05-08T00:00:00",
            "variants": [{"name": "256", "entrypoint": "thumb-256.webp", **preview}],
            **preview,
        }],
    }
    respx.get(f"{BASE_URL}/v1/manage/assets/{asset_id}").mock(
        return_value=httpx.Response(200, json=body)
    )

    with _make_client() as client:
        asset = client.assets.get(asset_id)

    rendition = asset.renditions[0]
    for item in (asset, rendition, rendition.variants[0]):
        assert (item.preview_url, item.preview_url_type, item.preview_url_expires_in) == (url, "gate", 900)


def test_preview_urls_default_to_none() -> None:
    from saas_media_library import Asset

    asset = Asset.model_validate(_asset_response(uuid4(), "public"))

    assert asset.preview_url is None
    assert asset.preview_url_type is None
    assert asset.preview_url_expires_in is None
