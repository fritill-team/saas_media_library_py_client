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
