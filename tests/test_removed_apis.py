"""APIs media retired are gone from the client, and nothing left warns."""

from __future__ import annotations

import importlib
import warnings
from uuid import uuid4

import httpx
import pytest
import respx

import pkg_media
from pkg_media import MediaLibraryClient


BASE_URL = "https://media.test"


def _make_client() -> MediaLibraryClient:
    return MediaLibraryClient(base_url=BASE_URL, tenant_id=str(uuid4()), token="t")


def test_resource_policies_are_removed() -> None:
    with _make_client() as client:
        assert not hasattr(client, "policies")
    for name in ("ResourcePolicy", "FileConstraints", "PermissionRequirements",
                 "profiles_from_policies", "policy_key", "find_profile", "render_profiles_module"):
        assert not hasattr(pkg_media, name), name
    with pytest.raises(ImportError):
        importlib.import_module("pkg_media.resource_policies")
    with pytest.raises(ImportError):
        importlib.import_module("pkg_media.migrate_policies")


def test_asset_access_grants_are_removed() -> None:
    with _make_client() as client:
        assert not hasattr(client, "access")
    for name in ("AssetAccessAPI", "AssetAccessGrant"):
        assert not hasattr(pkg_media, name), name
    with pytest.raises(ImportError):
        importlib.import_module("pkg_media.asset_access")


def test_remaining_apis_do_not_warn() -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/v1/manage/processing-options/schema").mock(
            return_value=httpx.Response(200, json={}))
        mock.get("/v1/manage/processing-options/pipelines").mock(
            return_value=httpx.Response(200, json={}))

        with _make_client() as client, warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            client.processing.get_schema()
            client.processing.get_pipelines()
