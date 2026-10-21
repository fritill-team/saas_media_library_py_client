from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import respx

from pkg_media import (
    ConflictError,
    MediaLibraryClient,
    NotFoundError,
    WebAppDeploy,
    WebAppDeployList,
)


BASE_URL = "https://media.test"
TENANT_ID = str(uuid4())
MANAGE_URL = f"{BASE_URL}/v1/manage/assets"


def _make_client() -> MediaLibraryClient:
    return MediaLibraryClient(base_url=BASE_URL, tenant_id=TENANT_ID, token="t")


def _deploy(deploy_id, status: str, *, is_live: bool = False, **extra) -> dict:
    return {
        "id": str(deploy_id),
        "status": status,
        "createdAt": "2026-10-14T10:00:00Z",
        "isLive": is_live,
        "blobId": str(uuid4()),
        "scanStatus": "clean",
        "fileCount": 42,
        **extra,
    }


def _listing(live_id, deploys, *, limit=50, offset=0, next_offset=None) -> dict:
    return {
        "liveDeployId": str(live_id) if live_id else None,
        "deploys": deploys,
        "limit": limit,
        "offset": offset,
        "nextOffset": next_offset,
    }


@respx.mock
def test_list_deploys_sends_paging_and_parses_the_page() -> None:
    asset_id, live_id, old_id = uuid4(), uuid4(), uuid4()
    route = respx.get(f"{MANAGE_URL}/{asset_id}/deploys").mock(
        return_value=httpx.Response(200, json=_listing(
            live_id,
            [_deploy(live_id, "live", is_live=True),
             _deploy(old_id, "deleted", blobId=None, scanStatus=None, fileCount=0)],
            limit=2, offset=4, next_offset=6,
        ))
    )

    with _make_client() as client:
        page = client.assets.list_deploys(asset_id, limit=2, offset=4)

    request = route.calls.last.request
    assert dict(request.url.params) == {"limit": "2", "offset": "4"}
    assert request.headers["X-Tenant"] == TENANT_ID
    assert isinstance(page, WebAppDeployList)
    assert page.live_deploy_id == live_id
    assert (page.limit, page.offset, page.next_offset) == (2, 4, 6)
    live, old = page.deploys
    assert isinstance(live, WebAppDeploy)
    assert live.id == live_id and live.status == "live" and live.is_live
    assert live.scan_status == "clean" and live.file_count == 42
    assert old.status == "deleted" and not old.is_live
    assert old.blob_id is None and old.scan_status is None


@respx.mock
def test_list_deploys_defaults_and_last_page() -> None:
    asset_id = uuid4()
    route = respx.get(f"{MANAGE_URL}/{asset_id}/deploys").mock(
        return_value=httpx.Response(200, json=_listing(None, []))
    )

    with _make_client() as client:
        page = client.assets.list_deploys(str(asset_id))

    assert dict(route.calls.last.request.url.params) == {"limit": "50", "offset": "0"}
    assert page.live_deploy_id is None
    assert page.deploys == []
    assert page.next_offset is None


@respx.mock
def test_activate_deploy_posts_and_returns_the_list() -> None:
    asset_id, target_id, previous_id = uuid4(), uuid4(), uuid4()
    route = respx.post(f"{MANAGE_URL}/{asset_id}/deploys/{target_id}/activate").mock(
        return_value=httpx.Response(200, json=_listing(
            target_id,
            [_deploy(previous_id, "ready"), _deploy(target_id, "live", is_live=True)],
        ))
    )

    with _make_client() as client:
        page = client.assets.activate_deploy(asset_id, target_id)

    assert route.called
    assert route.calls.last.request.content == b""
    assert page.live_deploy_id == target_id
    assert [d.is_live for d in page.deploys] == [False, True]


@respx.mock
@pytest.mark.parametrize("reason", ["asset_not_found", "not_a_web_app", "deploy_not_found"])
def test_activate_deploy_404_carries_reason(reason) -> None:
    asset_id, deploy_id = uuid4(), uuid4()
    respx.post(f"{MANAGE_URL}/{asset_id}/deploys/{deploy_id}/activate").mock(
        return_value=httpx.Response(
            404, json={"detail": {"reason": reason, "message": "nope"}}
        )
    )

    with _make_client() as client, pytest.raises(NotFoundError) as excinfo:
        client.assets.activate_deploy(asset_id, deploy_id)

    assert excinfo.value.status_code == 404
    assert excinfo.value.reason == reason
    assert excinfo.value.body == {"detail": {"reason": reason, "message": "nope"}}


@respx.mock
@pytest.mark.parametrize("reason", [
    "deploy_deleted", "deploy_failed", "deploy_uploading", "scan_not_allowed", "deploy_changed",
])
def test_activate_deploy_409_carries_reason(reason) -> None:
    asset_id, deploy_id = uuid4(), uuid4()
    respx.post(f"{MANAGE_URL}/{asset_id}/deploys/{deploy_id}/activate").mock(
        return_value=httpx.Response(
            409, json={"detail": {"reason": reason, "message": "cannot activate"}}
        )
    )

    with _make_client() as client, pytest.raises(ConflictError) as excinfo:
        client.assets.activate_deploy(asset_id, deploy_id)

    assert excinfo.value.reason == reason


@respx.mock
def test_list_deploys_404_not_a_web_app() -> None:
    asset_id = uuid4()
    respx.get(f"{MANAGE_URL}/{asset_id}/deploys").mock(
        return_value=httpx.Response(
            404, json={"detail": {"reason": "not_a_web_app", "message": "not a web app"}}
        )
    )

    with _make_client() as client, pytest.raises(NotFoundError) as excinfo:
        client.assets.list_deploys(asset_id)

    assert excinfo.value.reason == "not_a_web_app"


@respx.mock
def test_reason_is_none_for_plain_string_detail() -> None:
    asset_id = uuid4()
    respx.get(f"{MANAGE_URL}/{asset_id}").mock(
        return_value=httpx.Response(404, json={"detail": "Asset not found"})
    )

    with _make_client() as client, pytest.raises(NotFoundError) as excinfo:
        client.assets.get(asset_id)

    assert excinfo.value.reason is None
    assert str(excinfo.value) == "Asset not found"
