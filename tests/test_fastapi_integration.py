from __future__ import annotations

import json
from uuid import NAMESPACE_URL, uuid4, uuid5

import grpc
import httpx
import pydantic
import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from pkg_media import AssetUrlCache
from pkg_media.exceptions import MediaLibraryError
from pkg_media.integrations.fastapi import MAX_RESOLVE_IDS, create_media_router
from tests import delivery_fake


TENANT_ID = str(uuid4())


def _asset_json(asset_id) -> dict:
    return {
        "id": str(asset_id),
        "kind": "document",
        "title": "Syllabus",
        "status": "ready",
        "visibility": "restricted",
        "mimeType": "application/pdf",
        "downloadType": "gate",
        "downloadUrl": f"https://s3.test/{asset_id}.pdf?sig=secret",
        "downloadUrlExpiresIn": 3600,
        "hlsUrl": None,
        "dashUrl": None,
        "streamUrlExpiresIn": None,
        "renditions": [
            {
                "id": str(uuid5(NAMESPACE_URL, f"{asset_id}/pdf_preview")),
                "renditionType": "pdf_preview",
                "status": "ready",
                "entrypoint": "preview.pdf",
                "durationSeconds": None,
                "variants": [],
                "language": None,
                "previewUrl": f"https://s3.test/{asset_id}-preview.pdf?sig=secret",
                "previewUrlType": "gate",
                "previewUrlExpiresIn": 3600,
            },
            {
                "id": str(uuid5(NAMESPACE_URL, f"{asset_id}/thumbnail")),
                "renditionType": "thumbnail",
                "status": "ready",
                "entrypoint": "page1.webp",
                "durationSeconds": None,
                "variants": [],
                "language": None,
                "previewUrl": f"https://s3.test/{asset_id}-thumb.webp?sig=thumb",
                "previewUrlType": "gate",
                "previewUrlExpiresIn": 3600,
            },
        ],
    }


@pytest.fixture
def media():
    """The media library's gRPC Delivery; ids in ``missing`` / ``broken`` are
    ``not_found`` / ``error``, the others ``ok``."""
    missing: set = set()
    broken: set = set()

    def answer(aid):
        if aid in missing:
            return {"assetId": aid, "status": "not_found"}
        if aid in broken:
            return {"assetId": aid, "status": "error", "error": "Blob not found for asset"}
        return {"assetId": aid, "status": "ok", "thumbnailUrl": f"https://s3.test/{aid}-thumb.webp?sig=thumb",
                "asset": _asset_json(aid)}

    with delivery_fake.serve(answer) as fake:
        fake.missing, fake.broken = missing, broken
        yield fake


@pytest.fixture
def enrollments():
    """user -> set of asset ids (str) the user may open."""
    return {}


@pytest.fixture
def seen_users():
    return []


@pytest.fixture
def app(media, enrollments, seen_users):
    def can_access(user, asset_ids):
        seen_users.append(user)
        return [a for a in asset_ids if str(a) in enrollments.get(user["id"], set())]

    def get_current_user(x_user: str = Header(default="")):
        if not x_user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return {"id": x_user}

    client = media.client(tenant_id=TENANT_ID)
    cache = AssetUrlCache()
    app = FastAPI()
    app.include_router(create_media_router(client, can_access, get_current_user, cache=cache))
    app.include_router(create_media_router(client, can_access, get_current_user, cache=cache, prefix="/api/v2/files"))
    return app


@pytest.fixture
def http(app):
    return TestClient(app)


# ── POST /resolve ──────────────────────────────────────────────────────


def test_resolve_returns_server_shaped_payload(media, http, enrollments) -> None:
    ok_id, denied_id, missing_id = str(uuid4()), str(uuid4()), str(uuid4())
    media.missing.add(missing_id)
    enrollments["alice"] = {ok_id, missing_id}

    resp = http.post("/media/resolve", json={"assetIds": [denied_id, ok_id, missing_id]},
                     headers={"X-User": "alice"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert [i["assetId"] for i in body["items"]] == [denied_id, ok_id, missing_id]
    assert [i["status"] for i in body["items"]] == ["forbidden", "ok", "not_found"]
    assert set(body["items"][0]) == {"assetId", "status", "thumbnailUrl", "asset", "error"}
    assert body["items"][1]["asset"] == _asset_json(ok_id)


def test_resolve_never_leaks_urls_for_denied_assets(media, http) -> None:
    denied_id = str(uuid4())

    resp = http.post("/media/resolve", json={"assetIds": [denied_id]}, headers={"X-User": "mallory"})

    item = resp.json()["items"][0]
    assert item == {
        "assetId": denied_id,
        "status": "forbidden",
        "thumbnailUrl": f"https://s3.test/{denied_id}-thumb.webp?sig=thumb",
        "asset": None,
        "error": "Access denied",
    }
    assert "sig=secret" not in resp.text


def test_resolve_passes_the_dependency_result_to_can_access(media, http, seen_users) -> None:
    http.post("/media/resolve", json={"assetIds": [str(uuid4())]}, headers={"X-User": "bob"})

    assert seen_users == [{"id": "bob"}]


@pytest.mark.parametrize(
    "body",
    [
        {"assetIds": []},
        {"assetIds": [str(uuid4()) for _ in range(MAX_RESOLVE_IDS + 1)]},
        {"assetIds": ["not-a-uuid"]},
        {},
    ],
)
def test_resolve_validates_body(media, http, body) -> None:
    resp = http.post("/media/resolve", json=body, headers={"X-User": "alice"})

    assert resp.status_code == 422
    assert media.requests == []


def test_resolve_accepts_the_maximum_batch(media, http) -> None:
    ids = [str(uuid4()) for _ in range(MAX_RESOLVE_IDS)]

    resp = http.post("/media/resolve", json={"assetIds": ids}, headers={"X-User": "alice"})

    assert resp.status_code == 200
    assert resp.json()["total"] == MAX_RESOLVE_IDS


def test_unauthenticated_user_is_rejected_by_the_app_dependency(media, http) -> None:
    resp = http.post("/media/resolve", json={"assetIds": [str(uuid4())]})

    assert resp.status_code == 401
    assert media.requests == []


def test_media_library_failure_is_a_502_not_the_users_401(media, http) -> None:
    media.fail(grpc.StatusCode.UNAUTHENTICATED)
    resp = http.post("/media/resolve", json={"assetIds": [str(uuid4())]}, headers={"X-User": "a"})

    assert resp.status_code == 502
    assert resp.json() == {"detail": "Media library request failed"}


@pytest.mark.parametrize(
    "status",
    [grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.INTERNAL],
)
def test_unreachable_media_library_is_a_502(media, http, status) -> None:
    asset_id = str(uuid4())
    media.fail(status, status)
    resolve = http.post("/media/resolve", json={"assetIds": [asset_id]}, headers={"X-User": "a"})
    get = http.get(f"/media/{asset_id}", headers={"X-User": "a"})

    for resp in (resolve, get):
        assert resp.status_code == 502
        assert resp.json() == {"detail": "Media library request failed"}


def test_malformed_media_library_response_is_a_502(media, http) -> None:
    asset_id = str(uuid4())
    media.set(asset_id, {"status": "ok"})  # no asset id
    resolve = http.post("/media/resolve", json={"assetIds": [asset_id]}, headers={"X-User": "a"})
    get = http.get(f"/media/{asset_id}", headers={"X-User": "a"})

    for resp in (resolve, get):
        assert resp.status_code == 502
        assert resp.json() == {"detail": "Media library request failed"}


def test_app_errors_are_not_reported_as_media_library_failures(media) -> None:
    # A can_access bug is the app's own 500, not a 502.
    client = media.client(tenant_id=TENANT_ID)
    app = FastAPI()
    app.include_router(create_media_router(client, lambda user, ids: None, lambda: "u"))

    resp = TestClient(app, raise_server_exceptions=False).post(
        "/media/resolve", json={"assetIds": [str(uuid4())]}
    )

    assert resp.status_code == 500


def _pydantic_error() -> Exception:
    try:
        pydantic.TypeAdapter(int).validate_python("not a number")
    except pydantic.ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


@pytest.mark.parametrize(
    "make_error",
    [
        lambda: httpx.ConnectError("entitlement service down"),
        _pydantic_error,
        lambda: json.JSONDecodeError("bad", "", 0),
    ],
    ids=["httpx", "pydantic", "json"],
)
def test_can_access_errors_of_media_library_types_stay_500(media, make_error) -> None:
    # The app's own httpx/pydantic/json errors are not media library failures.
    def can_access(user, ids):
        raise make_error()

    client = media.client(tenant_id=TENANT_ID)
    app = FastAPI()
    app.include_router(create_media_router(client, can_access, lambda: "u"))
    http = TestClient(app, raise_server_exceptions=False)
    asset_id = str(uuid4())

    resolve = http.post("/media/resolve", json={"assetIds": [asset_id]})
    get = http.get(f"/media/{asset_id}")

    assert resolve.status_code == 500
    assert get.status_code == 500


def test_can_access_media_library_error_answers_502(media) -> None:
    # Indistinguishable from the gate's own failures (documented in the README).
    def can_access(user, ids):
        raise MediaLibraryError("can_access called the media library")

    client = media.client(tenant_id=TENANT_ID)
    app = FastAPI()
    app.include_router(create_media_router(client, can_access, lambda: "u"))
    http = TestClient(app, raise_server_exceptions=False)
    asset_id = str(uuid4())

    assert http.post("/media/resolve", json={"assetIds": [asset_id]}).status_code == 502
    assert http.get(f"/media/{asset_id}").status_code == 502


# ── GET /{asset_id} ────────────────────────────────────────────────────


def test_get_returns_the_asset(media, http, enrollments) -> None:
    asset_id = str(uuid4())
    enrollments["alice"] = {asset_id}

    resp = http.get(f"/media/{asset_id}", headers={"X-User": "alice"})

    assert resp.status_code == 200
    assert resp.json() == _asset_json(asset_id)


def test_get_forbidden_is_403_with_thumbnail_only(media, http) -> None:
    asset_id = str(uuid4())

    resp = http.get(f"/media/{asset_id}", headers={"X-User": "mallory"})

    assert resp.status_code == 403
    assert resp.json() == {
        "detail": "Access denied",
        "thumbnailUrl": f"https://s3.test/{asset_id}-thumb.webp?sig=thumb",
    }
    assert "sig=secret" not in resp.text


def test_get_not_found_is_404(media, http, enrollments) -> None:
    asset_id = str(uuid4())
    media.missing.add(asset_id)
    enrollments["alice"] = {asset_id}

    resp = http.get(f"/media/{asset_id}", headers={"X-User": "alice"})

    assert resp.status_code == 404
    assert resp.json() == {"detail": f"Asset {asset_id} not found"}


def test_get_error_is_502(media, http, enrollments) -> None:
    asset_id = str(uuid4())
    media.broken.add(asset_id)
    enrollments["alice"] = {asset_id}

    resp = http.get(f"/media/{asset_id}", headers={"X-User": "alice"})

    assert resp.status_code == 502
    assert resp.json() == {"detail": "Blob not found for asset", "thumbnailUrl": None}


def test_get_invalid_id_is_422(media, http) -> None:
    assert http.get("/media/not-a-uuid", headers={"X-User": "a"}).status_code == 422


def test_custom_prefix(media, http, enrollments) -> None:
    asset_id = str(uuid4())
    enrollments["alice"] = {asset_id}

    assert http.get(f"/api/v2/files/{asset_id}", headers={"X-User": "alice"}).status_code == 200
    resp = http.post("/api/v2/files/resolve", json={"assetIds": [asset_id]}, headers={"X-User": "alice"})
    assert resp.json()["items"][0]["status"] == "ok"


def test_links_are_cached_across_users(media, http, enrollments) -> None:
    asset_id = str(uuid4())
    enrollments["alice"] = {asset_id}
    enrollments["bob"] = {asset_id}

    http.get(f"/media/{asset_id}", headers={"X-User": "alice"})
    resp = http.post("/media/resolve", json={"assetIds": [asset_id]}, headers={"X-User": "bob"})

    assert resp.json()["items"][0]["status"] == "ok"
    assert len(media.requests) == 1


# ── Story 7.10: every id failing can_access is forbidden ──────────────


def test_a_denied_id_is_forbidden_even_when_it_does_not_exist(media, http) -> None:
    missing_id = str(uuid4())
    media.missing.add(missing_id)

    resp = http.post("/media/resolve", json={"assetIds": [missing_id]}, headers={"X-User": "mallory"})

    assert resp.json()["items"] == [{
        "assetId": missing_id, "status": "forbidden", "thumbnailUrl": None, "asset": None,
        "error": "Access denied",
    }]


def test_a_full_batch_is_one_media_request(media, http, enrollments) -> None:
    ids = [str(uuid4()) for _ in range(MAX_RESOLVE_IDS)]
    enrollments["alice"] = set(ids)

    resp = http.post("/media/resolve", json={"assetIds": ids}, headers={"X-User": "alice"})

    assert [i["status"] for i in resp.json()["items"]] == ["ok"] * MAX_RESOLVE_IDS
    assert [len(r) for r in media.requests] == [MAX_RESOLVE_IDS]


def test_without_thumbnails_denied_ids_are_not_sent_to_media(media, enrollments) -> None:
    allowed_id, denied_id = str(uuid4()), str(uuid4())
    client = media.client()
    app = FastAPI()
    app.include_router(create_media_router(
        client, lambda user, ids: [allowed_id], lambda: "u", thumbnails=False,
    ))

    body = TestClient(app).post("/media/resolve", json={"assetIds": [denied_id, allowed_id]}).json()

    assert media.requests == [[allowed_id]]
    assert body["items"][0] == {"assetId": denied_id, "status": "forbidden", "thumbnailUrl": None,
                                "asset": None, "error": "Access denied"}
    assert body["items"][1]["status"] == "ok"
    client.close()


def test_importing_pkg_media_does_not_need_fastapi() -> None:
    import subprocess
    import sys

    code = (
        "import sys; import pkg_media, pkg_media.contract; "
        "assert 'fastapi' not in sys.modules, 'pkg_media imported fastapi'"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
