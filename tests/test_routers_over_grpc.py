"""The resolve router, the upload router and the helpers with a gRPC client
(the in-process server of tests/grpc_fake.py)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from pkg_media import (
    AccessGate,
    AssetUrlCache,
    InMemoryUploadStore,
    MediaClient,
    UploadProfile,
    claim_upload,
    methods,
)
from pkg_media.integrations.fastapi import create_media_router, create_upload_router
from tests import grpc_fake

COVER = UploadProfile(name="course-cover", kind="image", allowed_mime_types={"image/png"}, max_size_bytes=5_000_000)


@pytest.fixture
def fake():
    media = grpc_fake.FakeMedia()
    server, target = grpc_fake.start(media)
    media.target = target
    yield media
    server.stop(0)


@pytest.fixture
def client(fake):
    with MediaClient(transport="grpc", target=fake.target, token="service") as c:
        yield c


def _user(x_user: str = Header(default="")):
    if not x_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return x_user


def test_resolve_router_over_grpc(client, fake) -> None:
    allowed, denied, missing_allowed, missing_denied = (str(uuid4()) for _ in range(4))
    fake.missing.update({missing_allowed, missing_denied})
    app = FastAPI()
    app.include_router(create_media_router(
        client, lambda user, ids: [allowed, missing_allowed] if user == "alice" else [], _user,
        cache=AssetUrlCache(),
    ))
    http = TestClient(app)

    body = http.post(
        "/media/resolve", json={"assetIds": [allowed, denied, missing_allowed, missing_denied]},
        headers={"X-User": "alice"},
    ).json()

    assert [i["status"] for i in body["items"]] == ["ok", "forbidden", "not_found", "forbidden"]
    assert body["items"][0]["asset"]["downloadUrl"] == f"https://media.test/o/{allowed}"
    assert body["items"][1] == {"assetId": denied, "status": "forbidden",
                                "thumbnailUrl": f"https://media.test/t/{denied}", "asset": None,
                                "error": "Access denied"}
    assert body["total"] == 4
    assert http.get(f"/media/{allowed}", headers={"X-User": "alice"}).json()["id"] == allowed
    assert http.get(f"/media/{denied}", headers={"X-User": "alice"}).status_code == 403


def test_the_gate_and_cache_over_grpc(client, fake) -> None:
    asset_id = uuid4()
    gate = AccessGate(client, lambda user, ids: ids, cache=AssetUrlCache())
    gate.resolve("a", [asset_id])
    gate.resolve("b", [asset_id])
    assert len(fake.calls_to(methods.Delivery.RESOLVE_DELIVERY_ASSETS)) == 1


def test_media_failures_over_grpc_are_502(client, fake) -> None:
    import grpc

    fake.fail(methods.Delivery.RESOLVE_DELIVERY_ASSETS, grpc.StatusCode.PERMISSION_DENIED)
    app = FastAPI()
    app.include_router(create_media_router(client, lambda user, ids: ids, lambda: "u"))
    resp = TestClient(app).post("/media/resolve", json={"assetIds": [str(uuid4())]})
    assert resp.status_code == 502 and resp.json() == {"detail": "Media library request failed"}


def _upload_app(client, store, **kwargs):
    app = FastAPI()
    app.include_router(create_upload_router(
        client, lambda user, req, profile: user == "alice", _user,
        profiles={"course-cover": COVER}, store=store, get_owner=lambda user: user, ttl_seconds=300,
        on_attach=lambda user, claimed: {"courseId": claimed.record.context.get("courseId")},
        **kwargs,
    ))
    return TestClient(app)


def test_upload_router_over_grpc(client, fake) -> None:
    store = InMemoryUploadStore()
    http = _upload_app(client, store)

    start = http.post("/uploads", json={"mimeType": "image/png", "sizeBytes": 1000, "filename": "c.png",
                                         "context": {"courseId": "c1"}}, headers={"X-User": "alice"})
    assert start.status_code == 201
    started = start.json()
    assert started["grant"] == "mlg_test" and "tenant" not in started
    [open_call] = fake.calls_to(methods.Uploads.OPEN_SESSION)
    assert open_call.request.subject == "alice" and open_call.request.grant_ttl_seconds == 300
    assert open_call.request.expected_size_bytes == 1000 and open_call.request.original_filename == "c.png"
    assert "idempotency-key" in open_call.metadata
    session_id = started["sessionId"]

    refreshed = http.post(f"/uploads/{session_id}/grant", headers={"X-User": "alice"})
    assert refreshed.status_code == 200
    [issue] = fake.calls_to(methods.Grants.ISSUE_GRANT)
    assert list(issue.request.session_ids) == [session_id] and list(issue.request.scopes) == ["upload"]

    # The browser uploaded and finalized over HTTP; media now has the asset.
    asset_id = str(uuid4())
    fake.sessions[session_id] = grpc_fake.session_message(session_id, state="finalized", final_blob_id=str(uuid4()))
    fake.assets[asset_id] = grpc_fake.asset_message(
        asset_id, kind="image", mime_type="image/png", size_bytes=1000, visibility="private",
        upload_session_id=session_id, renditions=[],
    )
    attached = http.post(f"/uploads/{session_id}/attach", json={"assetId": asset_id}, headers={"X-User": "alice"})
    assert attached.status_code == 200
    assert attached.json() == {"courseId": "c1", "sessionId": session_id, "assetId": asset_id}
    again = http.post(f"/uploads/{session_id}/attach", json={"assetId": asset_id}, headers={"X-User": "alice"})
    assert again.status_code == 409


def test_upload_router_aborts_when_the_record_cannot_be_saved(client, fake) -> None:
    class BrokenStore(InMemoryUploadStore):
        def save(self, record):
            raise RuntimeError("db down")

    http = TestClient(_upload_app(client, BrokenStore()).app, raise_server_exceptions=False)
    resp = http.post("/uploads", json={"mimeType": "image/png", "sizeBytes": 10}, headers={"X-User": "alice"})
    assert resp.status_code == 500
    assert len(fake.calls_to(methods.Uploads.ABORT_SESSION)) == 1


def test_claim_upload_rejects_another_sessions_asset_over_grpc(client, fake) -> None:
    from pkg_media import UploadRecord, UploadRejected

    store = InMemoryUploadStore()
    session_id, other_session = str(uuid4()), str(uuid4())
    store.save(UploadRecord(session_id=UUID(session_id), owner="alice", profile="course-cover"))
    fake.sessions[session_id] = grpc_fake.session_message(session_id, state="finalized", final_blob_id=str(uuid4()))
    asset_id = str(uuid4())
    fake.assets[asset_id] = grpc_fake.asset_message(
        asset_id, kind="image", mime_type="image/png", size_bytes=10, visibility="private",
        upload_session_id=other_session, renditions=[],
    )
    with pytest.raises(UploadRejected) as excinfo:
        claim_upload(client, store, session_id, asset_id, owner="alice", profile=COVER)
    assert excinfo.value.reason == UploadRejected.ASSET_MISMATCH
