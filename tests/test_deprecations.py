from __future__ import annotations

import warnings
from uuid import uuid4

import httpx
import pytest
import respx

from saas_media_library import MediaLibraryClient


BASE_URL = "https://media.test"
TENANT_ID = str(uuid4())
POLICIES_URL = f"{BASE_URL}/v1/manage/resource-policies"


def _make_client() -> MediaLibraryClient:
    return MediaLibraryClient(base_url=BASE_URL, tenant_id=TENANT_ID, token="t")


def _policy_json(policy_id=None, collection_name="cover") -> dict:
    return {
        "id": str(policy_id or uuid4()),
        "tenantId": str(uuid4()),
        "resourceType": "courses",
        "collectionName": collection_name,
        "fileConstraints": {"allowedMimeTypes": ["image/png"], "maxSizeBytes": None,
                            "minSizeBytes": None, "allowMultiple": False},
        "permissionRequirements": {"requiredPermissions": [], "allowAnonymous": False},
        "processingConfig": {"stepOptions": None, "useFilenameAsAssetId": False,
                             "enableTranscription": True},
        "kind": "image",
        "visibility": "public",
        "isActive": True,
        "version": 1,
        "createdAt": "2026-05-08T00:00:00",
        "updatedAt": "2026-05-08T00:00:00",
    }


POLICY_ID = uuid4()
ASSET_ID = uuid4()
USER_ID = uuid4()

POLICY_CALLS = {
    "upsert": lambda c: c.policies.upsert("courses", "image", "public", {"image/png"},
                                          collection_name="cover"),
    "get": lambda c: c.policies.get(POLICY_ID),
    "get_by_name": lambda c: c.policies.get_by_name("courses", "cover"),
    "list": lambda c: c.policies.list(),
    "update": lambda c: c.policies.update(POLICY_ID, is_active=True),
    "delete": lambda c: c.policies.delete(POLICY_ID),
    "activate": lambda c: c.policies.activate(POLICY_ID),
    "deactivate": lambda c: c.policies.deactivate(POLICY_ID),
    "sync_policies": lambda c: c.policies.sync_policies([]),
}

ACCESS_CALLS = {
    "grant": lambda c: c.access.grant(ASSET_ID, USER_ID),
    "revoke": lambda c: c.access.revoke(ASSET_ID, USER_ID),
    "list": lambda c: c.access.list(ASSET_ID),
    "bulk_grant": lambda c: c.access.bulk_grant([ASSET_ID], USER_ID),
    "bulk_revoke": lambda c: c.access.bulk_revoke([ASSET_ID], USER_ID),
}


@pytest.fixture
def server():
    grant = {"userId": str(USER_ID), "expiresAt": None, "grantedAt": None,
             "grantedByActorType": None, "grantedByActorId": None}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.post("/v1/manage/resource-policies").mock(
            return_value=httpx.Response(200, json=_policy_json()))
        mock.get("/v1/manage/resource-policies").mock(
            return_value=httpx.Response(200, json=[_policy_json(POLICY_ID)]))
        mock.get(f"/v1/manage/resource-policies/{POLICY_ID}").mock(
            return_value=httpx.Response(200, json=_policy_json(POLICY_ID)))
        mock.put(f"/v1/manage/resource-policies/{POLICY_ID}").mock(
            return_value=httpx.Response(200, json=_policy_json(POLICY_ID)))
        mock.delete(f"/v1/manage/resource-policies/{POLICY_ID}", name="delete_policy").mock(
            return_value=httpx.Response(204))
        mock.get("/v1/manage/resource-policies/schema").mock(
            return_value=httpx.Response(200, json={"defaults": {}}))
        mock.get("/v1/manage/resource-policies/pipelines/available-steps", name="legacy_steps").mock(
            return_value=httpx.Response(200, json={"image": {"pipeline": []}}))
        mock.post(f"/v1/manage/assets/{ASSET_ID}/access").mock(
            return_value=httpx.Response(200, json=grant))
        mock.delete(f"/v1/manage/assets/{ASSET_ID}/access/{USER_ID}").mock(
            return_value=httpx.Response(204))
        mock.get(f"/v1/manage/assets/{ASSET_ID}/access").mock(
            return_value=httpx.Response(200, json={"items": [grant], "total": 1}))
        mock.post("/v1/manage/access/bulk-grant").mock(
            return_value=httpx.Response(200, json={"items": [], "granted": 1, "failed": 0}))
        mock.post("/v1/manage/access/bulk-revoke").mock(
            return_value=httpx.Response(200, json={"revoked": 1}))
        yield mock


def _deprecations(record) -> list:
    return [w for w in record if issubclass(w.category, DeprecationWarning)]


# ── client.policies ────────────────────────────────────────────────────


@pytest.mark.parametrize("method", sorted(POLICY_CALLS))
def test_policy_methods_warn_once_at_the_caller(server, method) -> None:
    with _make_client() as client, pytest.warns(DeprecationWarning) as record:
        POLICY_CALLS[method](client)

    [warning] = _deprecations(record)
    message = str(warning.message)
    assert message.startswith(f"client.policies.{method}() is deprecated.")
    assert "UploadProfile" in message and "client.uploads" in message
    assert warning.filename == __file__


def test_every_public_policy_method_is_covered() -> None:
    from saas_media_library.resource_policies import ResourcePoliciesAPI

    public = {n for n in vars(ResourcePoliciesAPI) if not n.startswith("_") and callable(getattr(ResourcePoliciesAPI, n))}
    assert public == set(POLICY_CALLS) | {"get_schema", "get_available_steps"}


def test_sync_policies_warns_once_for_nested_calls(server) -> None:
    # Calls list, upsert twice, and delete for the stale "courses/cover".
    spec = {"resource_type": "courses", "collection_name": "banner", "kind": "image",
            "visibility": "public", "allowed_mime_types": {"image/png"}}

    with _make_client() as client, warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        client.policies.sync_policies([spec, spec], delete_stale=True)

    assert server["delete_policy"].called
    assert [str(w.message).split("(")[0] for w in _deprecations(record)] == ["client.policies.sync_policies"]


def test_upsert_auto_process_warning_still_points_at_caller(server) -> None:
    with _make_client() as client, warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        client.policies.upsert("courses", "image", "public", {"image/png"}, auto_process=True)

    messages = [str(w.message) for w in _deprecations(record)]
    assert any(m.startswith("client.policies.upsert()") for m in messages)
    assert any(m.startswith("auto_process is deprecated") for m in messages)
    assert all(w.filename == __file__ for w in _deprecations(record))


def test_get_available_steps_keeps_the_legacy_request(server) -> None:
    """Same request as v0.18.0 (apps' mocks keep matching), plus the warning."""
    with _make_client() as client, pytest.warns(DeprecationWarning) as record:
        steps = client.policies.get_available_steps()

    assert steps == {"image": {"pipeline": []}}
    assert server["legacy_steps"].call_count == 1
    [warning] = _deprecations(record)
    assert "client.processing.get_pipelines()" in str(warning.message)
    assert warning.filename == __file__


def test_get_schema_points_at_processing(server) -> None:
    with _make_client() as client, pytest.warns(DeprecationWarning) as record:
        schema = client.policies.get_schema()

    # Still the legacy policy schema: the new one has a different shape.
    assert schema == {"defaults": {}}
    [warning] = _deprecations(record)
    message = str(warning.message)
    assert "client.processing.get_schema()" in message
    assert "different shape" in message
    assert "mime_types_by_kind -> mimeTypesByKind" in message
    assert "configurable_steps -> configurableSteps" in message
    assert warning.filename == __file__


def test_warning_keeps_method_metadata() -> None:
    from saas_media_library.resource_policies import ResourcePoliciesAPI

    assert ResourcePoliciesAPI.upsert.__name__ == "upsert"
    assert "Create or update a resource policy" in ResourcePoliciesAPI.upsert.__doc__


def test_deprecated_call_still_raises_errors(server) -> None:
    missing = uuid4()
    server.get(f"/v1/manage/resource-policies/{missing}").mock(
        return_value=httpx.Response(404, json={"detail": "nope"}))

    from saas_media_library import NotFoundError

    with _make_client() as client, pytest.warns(DeprecationWarning), pytest.raises(NotFoundError):
        client.policies.get(missing)

    # The nesting guard is reset after an exception: the next call warns again.
    with _make_client() as client, pytest.warns(DeprecationWarning):
        client.policies.list()


# ── client.access ──────────────────────────────────────────────────────


@pytest.mark.parametrize("method", sorted(ACCESS_CALLS))
def test_access_methods_warn_at_the_caller(server, method) -> None:
    with _make_client() as client, pytest.warns(DeprecationWarning) as record:
        ACCESS_CALLS[method](client)

    [warning] = _deprecations(record)
    message = str(warning.message)
    assert message.startswith(f"client.access.{method}() is deprecated.")
    assert "AccessGate" in message
    assert warning.filename == __file__


def test_every_public_access_method_is_covered() -> None:
    from saas_media_library.asset_access import AssetAccessAPI

    public = {n for n in vars(AssetAccessAPI) if not n.startswith("_") and callable(getattr(AssetAccessAPI, n))}
    assert public == set(ACCESS_CALLS)


# ── Replacement APIs do not warn ───────────────────────────────────────


def test_replacement_apis_do_not_warn() -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/v1/delivery/assets/resolve").mock(
            return_value=httpx.Response(200, json={"items": [], "total": 0}))
        mock.get("/v1/manage/processing-options/schema").mock(
            return_value=httpx.Response(200, json={}))
        mock.get("/v1/manage/processing-options/pipelines").mock(
            return_value=httpx.Response(200, json={}))

        with _make_client() as client, warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            client.delivery.resolve([uuid4()])
            client.processing.get_schema()
            client.processing.get_pipelines()
