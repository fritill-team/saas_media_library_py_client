"""The HTTP transport's new pieces: client credentials, ``open_session``, the
per-status exceptions and the async client."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
import respx

from pkg_media import (
    AsyncMediaClient,
    AuthenticationError,
    FailedPreconditionError,
    InternalError,
    InvalidArgumentError,
    MediaClient,
    MediaLibraryError,
    PermissionDeniedError,
    ResourceExhaustedError,
    UnauthenticatedError,
    UnavailableError,
    UploadProfile,
    ValidationError,
)
from pkg_media.exceptions import ConflictError, ServerError

BASE_URL = "https://media.test"
TOKEN_URL = "https://sso.test/realms/Itqadem/protocol/openid-connect/token"
SESSIONS_URL = f"{BASE_URL}/v1/manage/uploads/sessions"
GRANTS_URL = f"{BASE_URL}/v1/manage/grants"
COVER = UploadProfile(name="cover", kind="image", allowed_mime_types={"image/png"})


def _session(session_id) -> dict:
    return {
        "id": str(session_id), "state": "initiated", "part_size_bytes": 8388608, "parts_received": 0,
        "expires_at": "2026-10-21T10:00:00Z", "checksum_verified": False,
    }


def _grant(scopes) -> dict:
    return {"grant": "mlg_x", "grantId": str(uuid4()), "scopes": scopes, "expiresIn": 600,
            "expiresAt": "2026-10-21T10:10:00Z"}


@respx.mock
def test_client_credentials_over_http() -> None:
    token = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "kc", "expires_in": 300}))
    route = respx.get(f"{BASE_URL}/v1/manage/processing-options/schema").mock(
        return_value=httpx.Response(200, json={"kinds": []})
    )
    with MediaClient(base_url=BASE_URL, token_url=TOKEN_URL, client_id="c", client_secret="s") as client:
        client.processing.get_schema()
        client.processing.get_schema()
    assert token.call_count == 1
    assert [c.request.headers["Authorization"] for c in route.calls] == ["Bearer kc", "Bearer kc"]
    assert "X-Tenant" not in route.calls.last.request.headers


@respx.mock
def test_open_session_creates_then_issues_a_grant_for_it() -> None:
    session_id = uuid4()
    respx.post(SESSIONS_URL).mock(return_value=httpx.Response(201, json=_session(session_id)))
    grants = respx.post(GRANTS_URL).mock(return_value=httpx.Response(201, json=_grant(["upload"])))
    with MediaClient(base_url=BASE_URL, token="ml_live_x") as client:
        opened = client.uploads.open_session(mime_type="image/png", profile=COVER, subject="u1", grant_ttl_seconds=60)
    assert opened.session.id == session_id and opened.grant.grant == "mlg_x"
    assert json.loads(grants.calls.last.request.content) == {
        "scopes": ["upload"], "subject": "u1", "ttlSeconds": 60, "sessionIds": [str(session_id)],
    }


@respx.mock
def test_open_session_aborts_the_session_when_the_grant_fails() -> None:
    session_id = uuid4()
    respx.post(SESSIONS_URL).mock(return_value=httpx.Response(201, json=_session(session_id)))
    respx.post(GRANTS_URL).mock(return_value=httpx.Response(503, json={"detail": "Media grants are not configured"}))
    abort = respx.post(f"{SESSIONS_URL}/{session_id}/abort").mock(
        return_value=httpx.Response(200, json={"session_id": str(session_id), "aborted": True})
    )
    with MediaClient(base_url=BASE_URL, token="ml_live_x") as client, pytest.raises(UnavailableError):
        client.uploads.open_session(mime_type="image/png", profile=COVER)
    assert abort.called


@pytest.mark.parametrize(
    ("status", "cls", "legacy"),
    [
        (400, InvalidArgumentError, ValidationError),
        (422, InvalidArgumentError, ValidationError),
        (401, UnauthenticatedError, AuthenticationError),
        (403, PermissionDeniedError, AuthenticationError),
        (409, FailedPreconditionError, ConflictError),
        (410, FailedPreconditionError, ConflictError),
        (429, ResourceExhaustedError, MediaLibraryError),
        (503, UnavailableError, ServerError),
        (500, InternalError, ServerError),
        (502, InternalError, ServerError),
    ],
)
@respx.mock
def test_http_statuses_raise_the_grpc_status_classes(status, cls, legacy) -> None:
    respx.get(f"{BASE_URL}/v1/manage/processing-options/schema").mock(
        return_value=httpx.Response(status, json={"detail": {"reason": "some_reason", "message": "no"}})
    )
    with MediaClient(base_url=BASE_URL, token="t") as client, pytest.raises(cls) as excinfo:
        client.processing.get_schema()
    assert isinstance(excinfo.value, legacy)
    assert excinfo.value.status_code == status and excinfo.value.reason == "some_reason"


@respx.mock
async def test_the_async_client_over_http() -> None:
    asset_id = uuid4()
    respx.delete(f"{BASE_URL}/v1/manage/assets/{asset_id}").mock(return_value=httpx.Response(204))
    async with AsyncMediaClient(base_url=BASE_URL, token="t") as client:
        await client.assets.delete(asset_id)
    assert respx.calls.call_count == 1


def test_a_sync_http_client_is_refused() -> None:
    with pytest.raises(TypeError):
        MediaClient(base_url=BASE_URL, http_client=httpx.Client())


@respx.mock
def test_set_token_replaces_the_bearer() -> None:
    route = respx.get(f"{BASE_URL}/v1/manage/processing-options/pipelines").mock(
        return_value=httpx.Response(200, json={})
    )
    with MediaClient(base_url=BASE_URL, token="old") as client:
        client.processing.get_pipelines()
        client.set_token("new")
        client.processing.get_pipelines()
    assert [c.request.headers["Authorization"] for c in route.calls] == ["Bearer old", "Bearer new"]
