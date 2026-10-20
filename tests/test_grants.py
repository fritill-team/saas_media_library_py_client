from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas_media_library import (
    AssetKind,
    AuthenticationError,
    GrantScope,
    MediaLibraryClient,
    ProcessingConfig,
    UploadProfile,
    ValidationError,
    Visibility,
    grant_profile_payload,
)
from saas_media_library.integrations.fastapi import create_grant_router

BASE_URL = "https://media.test"
GRANTS_URL = f"{BASE_URL}/v1/manage/grants"

COVER = UploadProfile(
    name="course-cover",
    kind=AssetKind.IMAGE,
    visibility=Visibility.PUBLIC,
    allowed_mime_types={"image/png", "image/jpeg"},
    min_size_bytes=1,
    max_size_bytes=5_000_000,
    required_permissions={"courses.edit"},
    processing_config=ProcessingConfig(step_options={"image_derive": {"sizes": [256]}}),
)


def _client() -> MediaLibraryClient:
    return MediaLibraryClient(base_url=BASE_URL, tenant_id=str(uuid4()), token="ml_live_x")


def _issued(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(201, json={
        "grant": "mlg_abc", "grantId": str(uuid4()), "scopes": sorted(body["scopes"]),
        "expiresIn": body.get("ttlSeconds", 600), "expiresAt": "2026-10-11T12:10:00Z",
    })


def test_profile_payload_is_the_servers_grant_profile():
    payload = grant_profile_payload(COVER)
    assert payload == {
        "kind": "image",
        "visibility": "public",
        "allowedMimeTypes": ["image/jpeg", "image/png"],
        "maxSizeBytes": 5_000_000,
        "processingConfig": {
            "stepOptions": {"image_derive": {"sizes": [256]}},
            "useFilenameAsAssetId": False,
            "enableTranscription": False,
        },
    }
    anything = UploadProfile(kind=AssetKind.VIDEO, allowed_mime_types={"*/*"})
    assert grant_profile_payload(anything) == {"kind": "video", "visibility": "private"}
    with pytest.raises(ValueError):
        grant_profile_payload(UploadProfile(kind=AssetKind.IMAGE, allowed_mime_types=set()))


@respx.mock
def test_issue_sends_scopes_assets_and_profile():
    route = respx.post(GRANTS_URL).mock(side_effect=_issued)
    asset = uuid4()
    grant = _client().grants.issue(
        [GrantScope.READ, "upload"], asset_ids=[asset], profile=COVER,
        subject="user-1", ttl_seconds=120, collection_name="covers",
    )
    sent = json.loads(route.calls.last.request.content)
    assert sent["scopes"] == ["read", "upload"] and sent["assetIds"] == [str(asset)]
    assert sent["profile"]["collectionName"] == "covers" and sent["subject"] == "user-1"
    assert route.calls.last.request.headers["authorization"] == "Bearer ml_live_x"
    assert grant.grant == "mlg_abc" and grant.expires_in == 120
    assert grant.to_payload()["expiresAt"].startswith("2026-10-11T12:10:00")


@respx.mock
@pytest.mark.parametrize("status, error", [(422, ValidationError), (403, AuthenticationError)])
def test_issue_errors(status, error):
    respx.post(GRANTS_URL).mock(return_value=httpx.Response(status, json={"detail": "no"}))
    with pytest.raises(error):
        _client().grants.issue(["read"], asset_ids=[uuid4()])


@pytest.fixture
def app_client():
    state = SimpleNamespace(allowed=True, seen=None)

    def authorize(user, request, profile):
        state.seen = (user, request, profile)
        return state.allowed

    app = FastAPI()
    app.include_router(create_grant_router(
        _client(), authorize, lambda: SimpleNamespace(id=7),
        profiles={"course-cover": COVER}, get_subject=lambda u: f"user-{u.id}",
    ))
    with TestClient(app) as http:
        yield http, state


@respx.mock
def test_grant_route_asks_the_app_then_issues(app_client):
    http, state = app_client
    route = respx.post(GRANTS_URL).mock(side_effect=_issued)
    asset = uuid4()
    response = http.post("/media/grants", json={"scopes": ["read"], "assetIds": [str(asset)]})
    assert response.status_code == 201 and response.json()["grant"] == "mlg_abc"
    assert state.seen[1].asset_ids == [asset] and state.seen[2] is None
    assert json.loads(route.calls.last.request.content)["subject"] == "user-7"

    response = http.post("/media/grants", json={"scopes": ["upload"], "profile": "course-cover"})
    assert response.status_code == 201 and state.seen[2] is COVER
    assert json.loads(route.calls.last.request.content)["profile"]["kind"] == "image"


@respx.mock
def test_grant_route_refusals(app_client):
    http, state = app_client
    respx.post(GRANTS_URL).mock(return_value=httpx.Response(422, json={"detail": "need assetIds"}))
    assert http.post("/media/grants", json={"scopes": ["read"]}).status_code == 422
    assert http.post("/media/grants", json={"scopes": ["manage:*"]}).status_code == 422
    assert http.post("/media/grants", json={"scopes": ["upload"], "profile": "nope"}).status_code == 422
    state.allowed = False
    assert http.post("/media/grants", json={"scopes": ["read"], "assetIds": [str(uuid4())]}).status_code == 403


@respx.mock
def test_grant_route_hides_service_token_failures(app_client):
    http, _ = app_client
    respx.post(GRANTS_URL).mock(return_value=httpx.Response(401, json={"detail": "bad token"}))
    response = http.post("/media/grants", json={"scopes": ["read"], "assetIds": [str(uuid4())]})
    assert response.status_code == 502


# ── delivery with grants of several services ──

DELIVERY_URL = f"{BASE_URL}/v1/delivery/assets"


@respx.mock
def test_read_grant_helper():
    route = respx.post(GRANTS_URL).mock(side_effect=_issued)
    asset = uuid4()
    _client().grants.read([asset], subject="u-1")
    assert json.loads(route.calls.last.request.content) == {
        "scopes": ["read"], "assetIds": [str(asset)], "subject": "u-1",
    }
    with pytest.raises(ValueError):
        _client().grants.read([])


@respx.mock
def test_resolve_sends_every_grant_in_one_header():
    seen = []

    def handler(request):
        seen.append(request.headers.get("x-media-grants"))
        ids = json.loads(request.content)["assetIds"]
        return httpx.Response(200, json={"items": [{"assetId": a, "status": "forbidden"} for a in ids]})

    respx.post(f"{DELIVERY_URL}/resolve").mock(side_effect=handler)
    client = _client()
    ids = [uuid4() for _ in range(150)]  # two batches, the header on both
    grant = SimpleNamespace(grant="mlg_b")  # a MediaGrant works too
    results = client.delivery.resolve(ids, grants=["mlg_a", grant, "mlg_a"])
    assert len(results) == 150 and seen == ["mlg_a, mlg_b", "mlg_a, mlg_b"]

    client.delivery.resolve(ids[:1])
    assert seen[-1] is None

    with pytest.raises(ValueError):
        client.delivery.resolve(ids, grants=[f"mlg_{i}" for i in range(11)])
    from saas_media_library import AssetUrlCache
    with pytest.raises(ValueError):
        client.delivery.resolve(ids, grants=["mlg_a"], cache=AssetUrlCache())


@respx.mock
def test_get_sends_grants():
    asset = uuid4()
    route = respx.get(f"{DELIVERY_URL}/{asset}").mock(return_value=httpx.Response(200, json={
        "id": str(asset), "kind": "image", "title": "x", "status": "ready", "visibility": "private",
        "mimeType": "image/png", "renditions": [], "downloadType": "gate",
        "downloadUrl": "https://files/x", "downloadUrlExpiresIn": 300,
    }))
    _client().delivery.get(asset, grants="mlg_a")
    assert route.calls.last.request.headers["x-media-grants"] == "mlg_a"
