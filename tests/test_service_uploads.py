from __future__ import annotations

import json
from typing import Optional
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from saas_media_library import (
    AssetKind,
    InMemoryUploadStore,
    MediaLibraryClient,
    NotFoundError,
    UploadProfile,
    UploadRecord,
    UploadRejected,
    Visibility,
    claim_upload,
)
from saas_media_library.integrations.fastapi import create_upload_router


BASE_URL = "https://media.test"
TENANT = "acme"
SESSIONS_URL = f"{BASE_URL}/v1/manage/uploads/sessions"
ASSETS_URL = f"{BASE_URL}/v1/manage/assets"
GRANTS_URL = f"{BASE_URL}/v1/manage/grants"

COVER = UploadProfile(
    name="course-cover",
    kind=AssetKind.IMAGE,
    visibility=Visibility.PUBLIC,
    allowed_mime_types={"image/png"},
    max_size_bytes=1_000,
)
AVATAR = UploadProfile(name="avatar", kind=AssetKind.IMAGE, visibility=Visibility.PUBLIC)


def _client() -> MediaLibraryClient:
    return MediaLibraryClient(base_url=BASE_URL, tenant_id=TENANT, token="ml_test_x")


def _session_json(session_id, *, state="finalized", final_blob_id=None) -> dict:
    return {
        "id": str(session_id),
        "state": state,
        "part_size_bytes": 8_388_608,
        "parts_expected": 1,
        "parts_received": 1 if state == "finalized" else 0,
        "received_parts": [1] if state == "finalized" else [],
        "expires_at": "2026-10-12T13:00:00",
        "checksum_verified": state == "finalized",
        "final_blob_id": str(final_blob_id) if final_blob_id else None,
    }


def _asset_json(asset_id, *, blob_id, kind="image", mime="image/png", size=500, visibility="public") -> dict:
    return {
        "id": str(asset_id),
        "kind": kind,
        "originalFilename": "cover.png",
        "mimeType": mime,
        "sizeBytes": size,
        "hashSha256": "0" * 64,
        "status": "ready",
        "visibility": visibility,
        "createdAt": "2026-10-12T12:00:00Z",
        "blobId": str(blob_id),
    }


def _grant_json(scopes=("upload",)) -> dict:
    return {
        "grant": "mlg_" + uuid4().hex,
        "grantId": str(uuid4()),
        "scopes": list(scopes),
        "expiresIn": 600,
        "expiresAt": "2026-10-12T12:10:00Z",
    }


def _mock_upload(router, session_id, asset_id, *, blob_id=None, state="finalized", **asset):
    blob_id = blob_id or uuid4()
    router.get(f"{SESSIONS_URL}/{session_id}").mock(
        return_value=httpx.Response(200, json=_session_json(session_id, state=state, final_blob_id=blob_id))
    )
    asset.setdefault("blob_id", blob_id)
    router.get(f"{ASSETS_URL}/{asset_id}").mock(return_value=httpx.Response(200, json=_asset_json(asset_id, **asset)))


# ── UploadsAPI.verify_upload ──────────────────────────────────────────


@respx.mock(assert_all_called=False)
def test_verify_upload_returns_the_sessions_asset(respx_mock) -> None:
    session_id, asset_id = uuid4(), uuid4()
    _mock_upload(respx_mock, session_id, asset_id)

    asset = _client().uploads.verify_upload(session_id, asset_id, profile=COVER)

    assert asset.id == asset_id


@respx.mock(assert_all_called=False)
def test_verify_upload_rejects_unfinalized_session_before_reading_the_asset(respx_mock) -> None:
    session_id, asset_id = uuid4(), uuid4()
    respx_mock.get(f"{SESSIONS_URL}/{session_id}").mock(
        return_value=httpx.Response(200, json=_session_json(session_id, state="in_progress"))
    )
    asset_route = respx_mock.get(f"{ASSETS_URL}/{asset_id}")

    with pytest.raises(UploadRejected) as exc:
        _client().uploads.verify_upload(session_id, asset_id, profile=COVER)

    assert exc.value.reason == UploadRejected.NOT_FINALIZED
    assert not asset_route.called


@respx.mock(assert_all_called=False)
def test_verify_upload_rejects_an_asset_from_another_blob(respx_mock) -> None:
    session_id, asset_id = uuid4(), uuid4()
    _mock_upload(respx_mock, session_id, asset_id, blob_id=uuid4())
    respx_mock.get(f"{ASSETS_URL}/{asset_id}").mock(
        return_value=httpx.Response(200, json=_asset_json(asset_id, blob_id=uuid4()))
    )

    with pytest.raises(UploadRejected) as exc:
        _client().uploads.verify_upload(session_id, asset_id, profile=COVER)

    assert exc.value.reason == UploadRejected.ASSET_MISMATCH


@pytest.mark.parametrize(
    ("asset", "reason"),
    [
        ({"visibility": "private"}, UploadRejected.VISIBILITY),
        ({"kind": "document"}, UploadRejected.KIND),
        ({"mime": "image/gif"}, UploadRejected.MIME_TYPE),
        ({"size": 2_000}, UploadRejected.TOO_LARGE),
    ],
)
@respx.mock(assert_all_called=False)
def test_verify_upload_applies_the_profile(asset, reason, respx_mock) -> None:
    session_id, asset_id = uuid4(), uuid4()
    _mock_upload(respx_mock, session_id, asset_id, **asset)

    with pytest.raises(UploadRejected) as exc:
        _client().uploads.verify_upload(session_id, asset_id, profile=COVER)

    assert exc.value.reason == reason


@respx.mock(assert_all_called=False)
def test_verify_upload_unknown_asset_raises_not_found(respx_mock) -> None:
    session_id, asset_id = uuid4(), uuid4()
    _mock_upload(respx_mock, session_id, asset_id)
    respx_mock.get(f"{ASSETS_URL}/{asset_id}").mock(return_value=httpx.Response(404, json={"detail": "nope"}))

    with pytest.raises(NotFoundError):
        _client().uploads.verify_upload(session_id, asset_id, profile=COVER)


# ── InMemoryUploadStore / claim_upload ────────────────────────────────


def test_in_memory_store_attaches_once() -> None:
    store = InMemoryUploadStore()
    session_id = uuid4()
    store.save(UploadRecord(session_id=session_id, owner="u1", profile="course-cover"))

    assert store.mark_attached(session_id, uuid4()) is True
    assert store.mark_attached(session_id, uuid4()) is False
    assert store.mark_attached(uuid4(), uuid4()) is False
    assert store.get(session_id).attached_asset_id is not None


def _store_with(session_id, *, owner="u1", profile="course-cover", context=None) -> InMemoryUploadStore:
    store = InMemoryUploadStore()
    store.save(UploadRecord(session_id=session_id, owner=owner, profile=profile, context=context or {}))
    return store


@respx.mock(assert_all_called=False)
def test_claim_upload_verifies_and_attaches(respx_mock) -> None:
    session_id, asset_id = uuid4(), uuid4()
    _mock_upload(respx_mock, session_id, asset_id)
    store = _store_with(session_id, context={"courseId": "c1"})

    claimed = claim_upload(_client(), store, str(session_id), str(asset_id), owner="u1", profile=COVER)

    assert claimed.asset.id == asset_id
    assert claimed.record.context == {"courseId": "c1"}
    assert claimed.record.attached_asset_id == asset_id
    assert store.get(session_id).attached_asset_id == asset_id


@pytest.mark.parametrize(
    ("owner", "profile", "reason"),
    [
        ("u2", COVER, UploadRejected.UNKNOWN_SESSION),
        ("u1", AVATAR, UploadRejected.PROFILE_MISMATCH),
    ],
)
@respx.mock(assert_all_called=False)
def test_claim_upload_checks_the_record_before_calling_the_media_library(owner, profile, reason, respx_mock) -> None:
    session_id = uuid4()
    store = _store_with(session_id)
    route = respx_mock.route(host="media.test")

    with pytest.raises(UploadRejected) as exc:
        claim_upload(_client(), store, session_id, uuid4(), owner=owner, profile=profile)

    assert exc.value.reason == reason
    assert not route.called


def test_claim_upload_unknown_session() -> None:
    with pytest.raises(UploadRejected) as exc:
        claim_upload(_client(), InMemoryUploadStore(), uuid4(), uuid4(), owner="u1", profile=COVER)
    assert exc.value.reason == UploadRejected.UNKNOWN_SESSION


@respx.mock(assert_all_called=False)
def test_claim_upload_twice_is_rejected(respx_mock) -> None:
    session_id, asset_id = uuid4(), uuid4()
    _mock_upload(respx_mock, session_id, asset_id)
    store = _store_with(session_id)
    claim_upload(_client(), store, session_id, asset_id, owner="u1", profile=COVER)

    with pytest.raises(UploadRejected) as exc:
        claim_upload(_client(), store, session_id, asset_id, owner="u1", profile=COVER)

    assert exc.value.reason == UploadRejected.ALREADY_ATTACHED


@respx.mock(assert_all_called=False)
def test_claim_upload_loses_a_concurrent_attach(respx_mock) -> None:
    session_id, asset_id = uuid4(), uuid4()
    _mock_upload(respx_mock, session_id, asset_id)
    store = _store_with(session_id)
    store.mark_attached = lambda *_: False  # another request attached it meanwhile

    with pytest.raises(UploadRejected) as exc:
        claim_upload(_client(), store, session_id, asset_id, owner="u1", profile=COVER)

    assert exc.value.reason == UploadRejected.ALREADY_ATTACHED


# ── create_upload_router ──────────────────────────────────────────────


class _User:
    def __init__(self, user_id: str, allowed: bool = True):
        self.id = user_id
        self.allowed = allowed


USERS = {"alice": _User("u1"), "bob": _User("u2"), "mallory": _User("u3", allowed=False)}


def _get_current_user(x_user: Optional[str] = Header(None)) -> _User:
    if x_user not in USERS:
        raise HTTPException(status_code=401)
    return USERS[x_user]


def _app(store=None, **kwargs):
    store = store if store is not None else InMemoryUploadStore()
    seen = []

    def can_start(user, req, profile):
        seen.append((user, req, profile))
        return user.allowed

    app = FastAPI()
    app.include_router(create_upload_router(
        _client(), can_start, _get_current_user,
        profiles=kwargs.pop("profiles", {"course-cover": COVER}),
        store=store,
        get_owner=lambda user: user.id,
        **kwargs,
    ))
    return TestClient(app), store, seen


class _MediaLibrary:
    """Mocks session creation, abort and grant issuing."""

    def __init__(self, router: respx.MockRouter, *, grant_status: int = 201):
        self.session_id = uuid4()
        self.create = router.post(SESSIONS_URL).mock(
            return_value=httpx.Response(201, json=_session_json(self.session_id, state="initiated"))
        )
        self.abort = router.post(f"{SESSIONS_URL}/{self.session_id}/abort").mock(
            return_value=httpx.Response(200, json={"session_id": str(self.session_id), "aborted": True})
        )
        self.grant = router.post(GRANTS_URL).mock(
            return_value=httpx.Response(grant_status, json=_grant_json() if grant_status == 201 else {"detail": "x"})
        )


START = {"mimeType": "image/png", "sizeBytes": 500, "filename": "cover.png", "context": {"courseId": "c1"}}


@respx.mock(assert_all_called=False)
def test_start_creates_the_session_and_a_session_grant(respx_mock) -> None:
    ml = _MediaLibrary(respx_mock)
    http, store, seen = _app(upload_base_url="https://cdn.media.test/v1/")

    resp = http.post("/uploads", json={**START, "profile": "course-cover"}, headers={"X-User": "alice"})

    assert resp.status_code == 201
    body = resp.json()
    assert body["sessionId"] == str(ml.session_id)
    assert body["grant"].startswith("mlg_")
    assert body["uploadBaseUrl"] == "https://cdn.media.test/v1"
    assert body["tenant"] == TENANT
    assert body["partSizeBytes"] == 8_388_608
    assert {"grantId", "expiresAt", "expiresIn"} <= body.keys()

    created = json.loads(ml.create.calls.last.request.content)
    assert created["mime_type"] == "image/png"
    assert created["kind"] == "image" and created["visibility"] == "public"
    assert created["expected_size_bytes"] == 500
    assert created["original_filename"] == "cover.png"
    assert ml.create.calls.last.request.headers["Authorization"] == "Bearer ml_test_x"

    grant = json.loads(ml.grant.calls.last.request.content)
    assert grant == {"scopes": ["upload"], "sessionIds": [str(ml.session_id)], "subject": "u1"}

    record = store.get(ml.session_id)
    assert record.owner == "u1" and record.profile == "course-cover"
    assert record.context == {"courseId": "c1"}
    _, req, profile = seen[0]
    assert req.context == {"courseId": "c1"} and profile is COVER
    assert not ml.abort.called


@respx.mock(assert_all_called=False)
def test_start_defaults_the_only_profile_and_the_upload_base_url(respx_mock) -> None:
    ml = _MediaLibrary(respx_mock)
    http, _, _ = _app(ttl_seconds=900)

    resp = http.post("/uploads", json=START, headers={"X-User": "alice"})

    assert resp.status_code == 201
    assert resp.json()["uploadBaseUrl"] == f"{BASE_URL}/v1"
    assert json.loads(ml.grant.calls.last.request.content)["ttlSeconds"] == 900


@pytest.mark.parametrize(
    ("body", "status", "reason"),
    [
        ({"mimeType": "image/gif"}, 415, UploadRejected.MIME_TYPE),
        ({"sizeBytes": 5_000}, 413, UploadRejected.TOO_LARGE),
        ({"mimeType": "not a type"}, 415, UploadRejected.MIME_TYPE),
    ],
)
@respx.mock(assert_all_called=False)
def test_start_rejects_files_the_profile_refuses(body, status, reason, respx_mock) -> None:
    ml = _MediaLibrary(respx_mock)
    http, _, seen = _app()

    resp = http.post("/uploads", json={**START, **body}, headers={"X-User": "alice"})

    assert resp.status_code == status
    assert resp.json()["reason"] == reason
    assert not ml.create.called and not seen


@respx.mock(assert_all_called=False)
def test_start_refused_by_the_apps_rule(respx_mock) -> None:
    ml = _MediaLibrary(respx_mock)
    http, _, _ = _app()

    resp = http.post("/uploads", json=START, headers={"X-User": "mallory"})

    assert resp.status_code == 403
    assert not ml.create.called


@respx.mock(assert_all_called=False)
def test_start_profile_names(respx_mock) -> None:
    ml = _MediaLibrary(respx_mock)
    http, _, _ = _app(profiles={"course-cover": COVER, "avatar": AVATAR})

    missing = http.post("/uploads", json=START, headers={"X-User": "alice"})
    unknown = http.post("/uploads", json={**START, "profile": "banner"}, headers={"X-User": "alice"})

    assert missing.status_code == 422
    assert unknown.status_code == 422
    assert not ml.create.called


@respx.mock(assert_all_called=False)
def test_start_aborts_the_session_when_the_grant_fails(respx_mock) -> None:
    ml = _MediaLibrary(respx_mock, grant_status=503)
    http, _, _ = _app()

    resp = http.post("/uploads", json=START, headers={"X-User": "alice"})

    assert resp.status_code == 502
    assert ml.abort.called


@respx.mock(assert_all_called=False)
def test_start_media_library_down_is_502(respx_mock) -> None:
    respx_mock.post(SESSIONS_URL).mock(side_effect=httpx.ConnectError("down"))
    http, _, _ = _app()

    resp = http.post("/uploads", json=START, headers={"X-User": "alice"})

    assert resp.status_code == 502


@respx.mock(assert_all_called=False)
def test_refresh_issues_a_new_session_grant(respx_mock) -> None:
    session_id = uuid4()
    grant = respx_mock.post(GRANTS_URL).mock(return_value=httpx.Response(201, json=_grant_json()))
    http, _, _ = _app(_store_with(session_id))

    resp = http.post(f"/uploads/{session_id}/grant", headers={"X-User": "alice"})

    assert resp.status_code == 200
    assert resp.json()["sessionId"] == str(session_id)
    assert resp.json()["grant"].startswith("mlg_")
    assert json.loads(grant.calls.last.request.content)["sessionIds"] == [str(session_id)]


@respx.mock(assert_all_called=False)
def test_refresh_only_for_the_owner_before_attach(respx_mock) -> None:
    session_id = uuid4()
    grant = respx_mock.post(GRANTS_URL).mock(return_value=httpx.Response(201, json=_grant_json()))
    store = _store_with(session_id)
    http, _, _ = _app(store)

    assert http.post(f"/uploads/{session_id}/grant", headers={"X-User": "bob"}).status_code == 404
    assert http.post(f"/uploads/{uuid4()}/grant", headers={"X-User": "alice"}).status_code == 404
    store.mark_attached(session_id, uuid4())
    assert http.post(f"/uploads/{session_id}/grant", headers={"X-User": "alice"}).status_code == 409
    assert not grant.called


def test_router_profiles_must_be_named_by_their_key() -> None:
    with pytest.raises(ValueError):
        _app(profiles={"cover": COVER})
    with pytest.raises(ValueError):
        _app(profiles={})
