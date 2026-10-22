"""``MediaClient(transport="grpc")`` against an in-process server built from
the contract's base classes (tests/grpc_fake.py), and Keycloak client
credentials mocked with respx."""

from __future__ import annotations

import threading
from concurrent import futures
from uuid import UUID, uuid4

import grpc
import httpx
import pytest
import respx

from pkg_media import (
    AsyncMediaClient,
    ClientCredentials,
    DeliveryBatchResolveResult,
    FailedPreconditionError,
    InternalError,
    InvalidArgumentError,
    MediaClient,
    MediaLibraryError,
    NotFoundError,
    PermissionDeniedError,
    ResourceExhaustedError,
    UnauthenticatedError,
    UnavailableError,
    UploadProfile,
    Visibility,
    methods,
    reasons,
)
from pkg_media.contract import refusal_status
from pkg_media.exceptions import ConflictError, DeadlineExceededError, UnimplementedError, ValidationError
from tests import grpc_fake

TOKEN_URL = "https://sso.test/realms/Itqadem/protocol/openid-connect/token"
COVER = UploadProfile(name="cover", kind="image", allowed_mime_types={"image/png"}, max_size_bytes=10_000_000)


@pytest.fixture
def fake():
    media = grpc_fake.FakeMedia()
    server, target = grpc_fake.start(media)
    media.target = target
    yield media
    server.stop(0)


@pytest.fixture
def keycloak():
    with respx.mock(assert_all_called=False) as router:
        route = router.post(TOKEN_URL).mock(
            side_effect=lambda request: httpx.Response(
                200, json={"access_token": f"kc-{route.call_count + 1}", "expires_in": 300, "token_type": "Bearer"}
            )
        )
        yield route


@pytest.fixture
def client(fake, keycloak):
    with MediaClient(
        transport="grpc",
        target=fake.target,
        token_url=TOKEN_URL,
        client_id="courses",
        client_secret="s3cret",
        retries=2,
    ) as c:
        c.aio._transport._backoff = 0  # no sleeping between retries in tests
        yield c


# ── Credentials and metadata ───────────────────────────────────────────


def test_every_call_sends_the_cached_client_credentials_token(client, fake, keycloak) -> None:
    client.assets.get(uuid4())
    client.delivery.resolve([uuid4()])
    client.processing.get_schema()

    assert keycloak.call_count == 1
    form = dict(httpx.QueryParams(keycloak.calls.last.request.content.decode()))
    assert form == {"grant_type": "client_credentials", "client_id": "courses", "client_secret": "s3cret"}
    assert {c.metadata["authorization"] for c in fake.calls} == {"Bearer kc-1"}


def test_x_tenant_is_never_sent(client, fake) -> None:
    client.assets.get(uuid4())
    assert not [c for c in fake.calls if "x-tenant" in c.metadata]


def test_the_token_is_refreshed_before_it_expires(fake, keycloak) -> None:
    now = [0.0]
    credentials = ClientCredentials(TOKEN_URL, "courses", "s3cret", refresh_margin=30, clock=lambda: now[0])
    with MediaClient(transport="grpc", target=fake.target, credentials=credentials) as client:
        client.assets.get(uuid4())
        now[0] = 269.0  # 300 s token, refreshed from 270 s on
        client.assets.get(uuid4())
        now[0] = 270.0
        client.assets.get(uuid4())

    assert keycloak.call_count == 2
    assert [c.metadata["authorization"] for c in fake.calls] == ["Bearer kc-1", "Bearer kc-1", "Bearer kc-2"]


def test_a_rejected_token_is_fetched_again_once(client, fake, keycloak) -> None:
    fake.fail(methods.Assets.GET_ASSET, grpc.StatusCode.UNAUTHENTICATED)
    client.assets.get(uuid4())

    assert keycloak.call_count == 2
    assert [c.metadata["authorization"] for c in fake.calls] == ["Bearer kc-1", "Bearer kc-2"]


def test_a_token_rejected_twice_raises(client, fake) -> None:
    status = refusal_status(methods.Assets.SERVICE, reasons.CREDENTIAL_REJECTED, "Invalid token")
    fake.fail(methods.Assets.GET_ASSET, status, status)
    with pytest.raises(UnauthenticatedError) as excinfo:
        client.assets.get(uuid4())
    assert excinfo.value.reason == "credential_rejected"
    assert len(fake.calls) == 2


def test_keycloak_refusing_the_client_raises_without_the_secret(fake) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "invalid_client"}))
        with MediaClient(transport="grpc", target=fake.target, token_url=TOKEN_URL,
                         client_id="courses", client_secret="s3cret") as client:
            with pytest.raises(UnauthenticatedError) as excinfo:
                client.assets.get(uuid4())
    assert excinfo.value.reason == "invalid_client"
    assert "s3cret" not in str(excinfo.value) and "s3cret" not in repr(client.aio.credentials)
    assert not fake.calls


def test_a_static_token_is_sent_as_is(fake) -> None:
    with MediaClient(transport="grpc", target=fake.target, token="ml_live_x") as client:
        client.assets.get(uuid4())
    assert fake.calls[0].metadata["authorization"] == "Bearer ml_live_x"


def test_mutating_calls_carry_an_idempotency_key_and_reads_do_not(client, fake) -> None:
    asset_id = uuid4()
    client.assets.get(asset_id)
    client.assets.update_visibility(asset_id, Visibility.PRIVATE)
    client.assets.update_visibility(asset_id, Visibility.PRIVATE)
    client.permalinks.set("users/1/avatar", asset_id)
    client.grants.issue(["read"], asset_ids=[asset_id])

    read, *mutating = fake.calls
    assert "idempotency-key" not in read.metadata
    keys = [c.metadata["idempotency-key"] for c in mutating]
    assert all(keys) and len(set(keys)) == len(keys)


# ── Retries ────────────────────────────────────────────────────────────


def test_unavailable_is_retried_for_reads(client, fake) -> None:
    fake.fail(methods.Assets.GET_ASSET, grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.UNAVAILABLE)
    client.assets.get(uuid4())
    assert len(fake.calls_to(methods.Assets.GET_ASSET)) == 3


def test_a_keyed_call_is_retried_with_the_same_key(client, fake) -> None:
    fake.fail(methods.Assets.DELETE_ASSET, grpc.StatusCode.UNAVAILABLE)
    client.assets.delete(uuid4())
    first, second = fake.calls_to(methods.Assets.DELETE_ASSET)
    assert first.metadata["idempotency-key"] == second.metadata["idempotency-key"]


def test_retries_stop_after_the_limit(client, fake) -> None:
    fake.fail(methods.Assets.GET_ASSET, *[grpc.StatusCode.UNAVAILABLE] * 3)
    with pytest.raises(UnavailableError):
        client.assets.get(uuid4())
    assert len(fake.calls_to(methods.Assets.GET_ASSET)) == 3


@pytest.mark.parametrize(
    "code",
    [grpc.StatusCode.INTERNAL, grpc.StatusCode.NOT_FOUND, grpc.StatusCode.DEADLINE_EXCEEDED,
     grpc.StatusCode.RESOURCE_EXHAUSTED],
)
def test_other_statuses_are_not_retried(client, fake, code) -> None:
    fake.fail(methods.Assets.GET_ASSET, code)
    with pytest.raises(MediaLibraryError):
        client.assets.get(uuid4())
    assert len(fake.calls_to(methods.Assets.GET_ASSET)) == 1


# ── Errors ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("code", "cls", "legacy"),
    [
        (grpc.StatusCode.INVALID_ARGUMENT, InvalidArgumentError, ValidationError),
        (grpc.StatusCode.UNAUTHENTICATED, UnauthenticatedError, None),
        (grpc.StatusCode.PERMISSION_DENIED, PermissionDeniedError, None),
        (grpc.StatusCode.NOT_FOUND, NotFoundError, None),
        (grpc.StatusCode.FAILED_PRECONDITION, FailedPreconditionError, ConflictError),
        (grpc.StatusCode.RESOURCE_EXHAUSTED, ResourceExhaustedError, None),
        (grpc.StatusCode.UNAVAILABLE, UnavailableError, None),
        (grpc.StatusCode.DEADLINE_EXCEEDED, DeadlineExceededError, None),
        (grpc.StatusCode.UNIMPLEMENTED, UnimplementedError, None),
        (grpc.StatusCode.INTERNAL, InternalError, None),
    ],
)
def test_one_exception_class_per_status(fake, code, cls, legacy) -> None:
    with MediaClient(transport="grpc", target=fake.target, token="t", retries=0) as client:
        fake.fail(methods.Processing.GET_PROCESSING_SCHEMA, code)
        with pytest.raises(cls) as excinfo:
            client.processing.get_schema()
    assert type(excinfo.value) is cls
    assert excinfo.value.grpc_status is code
    if legacy is not None:
        assert isinstance(excinfo.value, legacy)


def test_a_refusal_exposes_its_reason_and_domain(client, fake) -> None:
    with pytest.raises(NotFoundError) as excinfo:
        client.uploads.get_session(uuid4())
    assert excinfo.value.reason == reasons.SESSION_NOT_FOUND
    assert excinfo.value.domain == "itqadem.media.v1.Uploads"
    assert str(excinfo.value) == "Upload session not found"


def test_the_deploy_reasons_arrive_as_on_http(client, fake) -> None:
    fake.fail(
        methods.Assets.ACTIVATE_DEPLOY,
        refusal_status(methods.Assets.SERVICE, reasons.DEPLOY_DELETED, "Deploy was retired"),
    )
    with pytest.raises(ConflictError) as excinfo:
        client.assets.activate_deploy(uuid4(), uuid4())
    assert excinfo.value.reason == "deploy_deleted"


# ── Methods: same results as over HTTP ─────────────────────────────────


def test_assets_map_to_the_same_models(client) -> None:
    asset_id = uuid4()
    asset = client.assets.get(asset_id)
    assert asset.id == asset_id
    assert asset.size_bytes == 5_000_000_000
    assert asset.created_at == grpc_fake.CREATED
    assert asset.title == "Lesson 1" and asset.blob_id is None
    assert asset.renditions[0].extras == {"width": 320, "ratio": 1.5}
    assert asset.renditions[0].variants[0].width == 320
    assert asset.preview_url_expires_in == 3600 and asset.hls_url is None

    page = client.assets.list(kind="video", page=2, page_size=5)
    assert page.total == 41 and page.page == 2 and page.page_size == 5

    found, missing = client.assets.resolve([uuid4(), uuid4()])
    assert found.status == "ok" and found.asset is not None
    assert missing.status == "not_found" and missing.asset is None


def test_list_sends_the_filters(client, fake) -> None:
    client.assets.list(kind="video", status="ready", query="lesson", page=3, page_size=20)
    request = fake.calls[-1].request
    assert (request.kind, request.status, request.query, request.page, request.page_size) == (
        "video", "ready", "lesson", 3, 20
    )


def test_assets_list_has_no_policy_filter(client) -> None:
    with pytest.raises(TypeError):
        client.assets.list(resource_policy_id=uuid4())


def test_visibility_deploys_and_bulk(client) -> None:
    asset_id = uuid4()
    assert client.assets.update_visibility(asset_id, "private").visibility == "private"
    result = client.assets.bulk_update_visibility([uuid4(), uuid4()], "public")
    assert (result.updated, result.failed) == (1, 1)
    assert result.items[1] == {"assetId": result.items[1]["assetId"], "ok": False, "changed": None, "error": "not_found"}
    deploys = client.assets.list_deploys(asset_id, limit=10, offset=0)
    assert deploys.deploys[0].is_live and deploys.next_offset is None
    deploy_id = uuid4()
    assert client.assets.activate_deploy(asset_id, deploy_id).live_deploy_id == deploy_id


def test_delivery_resolve_batches_and_keeps_statuses(client, fake) -> None:
    ids = [UUID(int=i + 1) for i in range(150)]
    fake.forbidden.add(str(ids[9]))
    results = client.delivery.resolve(ids)
    assert [len(c.request.asset_ids) for c in fake.calls_to(methods.Delivery.RESOLVE_DELIVERY_ASSETS)] == [100, 50]
    assert [r.asset_id for r in results] == ids
    forbidden = next(r for r in results if r.status == "forbidden")
    assert forbidden.asset is None and forbidden.thumbnail_url
    assert isinstance(results[0], DeliveryBatchResolveResult)


def test_delivery_grants_travel_as_metadata(client, fake) -> None:
    client.delivery.get(uuid4(), grants=["mlg_a", "mlg_b"])
    assert fake.calls[-1].metadata["x-media-grants"] == "mlg_a, mlg_b"


def test_delivery_list_and_get(client) -> None:
    assert client.delivery.list(kind="image").total == 1
    asset_id = uuid4()
    asset = client.delivery.get(asset_id)
    assert asset.id == asset_id and asset.download_type == "gate"


def test_processing_numbers_come_back_as_integers(client) -> None:
    schema = client.processing.get_schema()
    options = schema["pipelinesByKind"]["image"]["processors"]["image_derive"]["options"]
    assert options["quality"]["default"] == 80 and isinstance(options["quality"]["default"], int)
    assert options["scale"]["default"] == 0.5
    assert schema == grpc_fake.SCHEMA
    assert client.processing.get_pipelines() == {"image": grpc_fake.SCHEMA["pipelinesByKind"]["image"]}


def test_permalinks(client, fake) -> None:
    asset_id = uuid4()
    permalink = client.permalinks.set("users/42/avatar", asset_id)
    assert permalink.asset_id == asset_id and permalink.url.endswith("/p/users/42/avatar")
    assert client.permalinks.get("users/42/avatar").name == "users/42/avatar"
    client.permalinks.delete("users/42/avatar")
    with pytest.raises(ValueError):
        client.permalinks.get("../x")
    assert len(fake.calls) == 3


def test_grants_issue_maps_the_profile(client, fake) -> None:
    grant = client.grants.issue(
        ["upload"], profile=COVER, subject="user-1", ttl_seconds=300, collection_name="covers"
    )
    request = fake.calls[-1].request
    assert grant.grant == "mlg_test" and grant.scopes == ["upload"]
    assert grant.expires_at == grpc_fake.CREATED
    assert list(request.scopes) == ["upload"]
    assert request.profile.kind == "image" and request.profile.visibility == "private"
    assert list(request.profile.allowed_mime_types) == ["image/png"]
    assert request.profile.max_size_bytes == 10_000_000
    assert request.profile.collection_name == "covers"
    assert (request.subject, request.ttl_seconds) == ("user-1", 300)


def test_open_session_returns_the_session_and_its_grant(client, fake) -> None:
    opened = client.uploads.open_session(
        mime_type="Image/PNG; q=1", profile=COVER, expected_size_bytes=100, subject="user-1",
        grant_ttl_seconds=120, processing_config={"stepOptions": {"image_derive": {"quality": 70}}},
    )
    request = fake.calls[-1].request
    assert opened.grant.scopes == ["upload"] and opened.session.state == "initiated"
    assert request.mime_type == "image/png" and request.kind == "image"
    assert request.expected_size_bytes == 100 and request.subject == "user-1" and request.grant_ttl_seconds == 120
    assert dict(request.processing_config.step_options)["image_derive"]["quality"] == 70


def test_session_lifecycle(client, fake) -> None:
    session = client.uploads.create_session(mime_type="image/png", profile=COVER, expected_size_bytes=100)
    assert client.uploads.get_session(session.id).id == session.id
    assert client.uploads.finalize(session.id, expected_sha256_hex="ab" * 32).checksum_verified
    assert client.uploads.abort(session.id).aborted


def test_bytes_never_travel_over_grpc(client, fake) -> None:
    with pytest.raises(MediaLibraryError, match="never travel over gRPC"):
        client.uploads.upload_file(b"\x89PNG", profile=COVER, mime_type="image/png")
    with pytest.raises(MediaLibraryError, match="never travel over gRPC"):
        client.uploads.upload_part(uuid4(), 1, b"x")
    assert not fake.calls


def test_verify_upload_works_over_grpc(client, fake) -> None:
    opened = client.uploads.open_session(mime_type="image/png", profile=COVER, expected_size_bytes=100)
    asset_id = str(uuid4())
    fake.sessions[str(opened.session.id)] = grpc_fake.session_message(
        str(opened.session.id), state="finalized", final_blob_id=str(uuid4())
    )
    fake.assets[asset_id] = grpc_fake.asset_message(
        asset_id, kind="image", mime_type="image/png", size_bytes=100, visibility="private",
        upload_session_id=str(opened.session.id), renditions=[],
    )
    asset = client.uploads.verify_upload(opened.session.id, asset_id, profile=COVER)
    assert str(asset.id) == asset_id


# ── Clients ────────────────────────────────────────────────────────────


def test_the_sync_client_is_safe_across_threads(client, fake) -> None:
    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: client.assets.get(uuid4()).kind, range(32)))
    assert results == ["video"] * 32
    assert len(fake.calls) == 32


async def test_the_async_client(fake, keycloak) -> None:
    async with AsyncMediaClient(
        transport="grpc", target=fake.target, token_url=TOKEN_URL, client_id="courses", client_secret="s3cret"
    ) as client:
        asset = await client.assets.get(uuid4())
        opened = await client.uploads.open_session(mime_type="image/png", profile=COVER, expected_size_bytes=100)
    assert asset.kind == "video" and opened.grant.grant == "mlg_test"
    assert fake.calls[0].metadata["authorization"] == "Bearer kc-1"


def test_switching_transports_is_configuration(fake) -> None:
    """The same code runs against both transports."""
    asset_id = uuid4()

    def run(client: MediaClient):
        return client.assets.get(asset_id).id

    with respx.mock:
        respx.get(f"https://media.test/v1/manage/assets/{asset_id}").mock(return_value=httpx.Response(200, json={
            "id": str(asset_id), "kind": "image", "originalFilename": "a.png", "mimeType": "image/png",
            "sizeBytes": 1, "hashSha256": "ab", "status": "ready", "visibility": "public",
            "createdAt": "2026-10-21T09:30:00Z",
        }))
        with MediaClient(transport="http", base_url="https://media.test", token="t") as http_client:
            assert run(http_client) == asset_id
    with MediaClient(transport="grpc", target=fake.target, token="t") as grpc_client:
        assert run(grpc_client) == asset_id


@pytest.mark.parametrize(
    "kwargs",
    [
        {"transport": "grpc"},
        {"transport": "http"},
        {"transport": "smoke-signals", "base_url": "x"},
        {"transport": "grpc", "target": "x", "token": "t", "client_id": "c", "client_secret": "s", "token_url": "u"},
        {"transport": "grpc", "target": "x", "client_id": "c"},
    ],
)
def test_bad_configuration_is_refused(kwargs) -> None:
    with pytest.raises(ValueError):
        MediaClient(**kwargs)


def test_closing_stops_the_loop_thread(fake) -> None:
    before = threading.active_count()
    client = MediaClient(transport="grpc", target=fake.target, token="t")
    client.assets.get(uuid4())
    client.close()
    client.close()
    assert threading.active_count() <= before + 1
    with pytest.raises(RuntimeError):
        client.assets.get(uuid4())
