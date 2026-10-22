from __future__ import annotations

import warnings
from uuid import uuid4

import httpx
import pytest
import respx

from pkg_media import MediaLibraryClient


BASE_URL = "https://media.test"
TENANT_ID = str(uuid4())


def _make_client() -> MediaLibraryClient:
    return MediaLibraryClient(base_url=BASE_URL, tenant_id=TENANT_ID, token="t")


ASSET_ID = uuid4()
USER_ID = uuid4()

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


# ── client.policies is gone ────────────────────────────────────────────


def test_resource_policies_are_removed() -> None:
    import pkg_media

    with _make_client() as client:
        assert not hasattr(client, "policies")
    for name in ("ResourcePolicy", "FileConstraints", "PermissionRequirements",
                 "profiles_from_policies", "policy_key", "find_profile", "render_profiles_module"):
        assert not hasattr(pkg_media, name), name
    with pytest.raises(ImportError):
        import pkg_media.resource_policies  # noqa: F401
    with pytest.raises(ImportError):
        import pkg_media.migrate_policies  # noqa: F401


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


def test_deprecated_call_still_raises_errors(server) -> None:
    missing = uuid4()
    server.get(f"/v1/manage/assets/{missing}/access").mock(
        return_value=httpx.Response(404, json={"detail": "nope"}))

    from pkg_media import NotFoundError

    with _make_client() as client, pytest.warns(DeprecationWarning), pytest.raises(NotFoundError):
        client.access.list(missing)

    # The nesting guard is reset after an exception: the next call warns again.
    with _make_client() as client, pytest.warns(DeprecationWarning):
        client.access.list(ASSET_ID)


def test_every_public_access_method_is_covered() -> None:
    from pkg_media.asset_access import AssetAccessAPI

    public = {n for n in vars(AssetAccessAPI) if not n.startswith("_") and callable(getattr(AssetAccessAPI, n))}
    assert public == set(ACCESS_CALLS)


# ── Replacement APIs do not warn ───────────────────────────────────────


def test_replacement_apis_do_not_warn() -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/v1/manage/processing-options/schema").mock(
            return_value=httpx.Response(200, json={}))
        mock.get("/v1/manage/processing-options/pipelines").mock(
            return_value=httpx.Response(200, json={}))

        with _make_client() as client, warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            client.processing.get_schema()
            client.processing.get_pipelines()
