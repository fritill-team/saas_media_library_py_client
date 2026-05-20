from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import respx

from saas_media_library import MediaLibraryClient, MediaLibraryError, Visibility
from saas_media_library.assets import BulkVisibilityResult


BASE_URL = "https://media.test"
TENANT_ID = str(uuid4())


def _make_client() -> MediaLibraryClient:
    return MediaLibraryClient(base_url=BASE_URL, tenant_id=TENANT_ID, token="t")


@respx.mock
def test_bulk_update_visibility_returns_result() -> None:
    ids = [uuid4(), uuid4(), uuid4()]
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, json={
            "items": [
                {"assetId": str(ids[0]), "status": "ok"},
                {"assetId": str(ids[1]), "status": "ok"},
                {"assetId": str(ids[2]), "status": "ok"},
            ],
            "updated": 3,
            "failed": 0,
        })

    respx.post(f"{BASE_URL}/v1/manage/assets/bulk-visibility").mock(side_effect=handler)

    with _make_client() as client:
        result = client.assets.bulk_update_visibility(ids, Visibility.PUBLIC)

    assert captured["body"] == {
        "assetIds": [str(i) for i in ids],
        "visibility": "public",
    }
    assert isinstance(result, BulkVisibilityResult)
    assert result.updated == 3
    assert result.failed == 0
    assert len(result.items) == 3


@respx.mock
def test_bulk_update_visibility_partial_failure() -> None:
    ids = [uuid4(), uuid4()]
    respx.post(f"{BASE_URL}/v1/manage/assets/bulk-visibility").mock(
        return_value=httpx.Response(200, json={
            "items": [
                {"assetId": str(ids[0]), "status": "ok"},
                {"assetId": str(ids[1]), "status": "not_found"},
            ],
            "updated": 1,
            "failed": 1,
        })
    )

    with _make_client() as client:
        result = client.assets.bulk_update_visibility(ids, Visibility.RESTRICTED)

    assert result.updated == 1
    assert result.failed == 1


@respx.mock
def test_bulk_update_visibility_400_raises_error() -> None:
    respx.post(f"{BASE_URL}/v1/manage/assets/bulk-visibility").mock(
        return_value=httpx.Response(400, json={"detail": "Invalid visibility"})
    )

    with _make_client() as client, pytest.raises(MediaLibraryError) as excinfo:
        client.assets.bulk_update_visibility([uuid4()], Visibility.PUBLIC)

    assert excinfo.value.status_code == 400
