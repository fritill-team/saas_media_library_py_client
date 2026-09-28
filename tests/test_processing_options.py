from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import respx

from saas_media_library import MediaLibraryClient, NotFoundError, ServerError


BASE_URL = "https://media.test"
TENANT_ID = str(uuid4())

SCHEMA_URL = f"{BASE_URL}/v1/manage/processing-options/schema"
PIPELINES_URL = f"{BASE_URL}/v1/manage/processing-options/pipelines"
LEGACY_SCHEMA_URL = f"{BASE_URL}/v1/manage/resource-policies/schema"
LEGACY_PIPELINES_URL = f"{BASE_URL}/v1/manage/resource-policies/pipelines/available-steps"

SCHEMA = {
    "kinds": ["image", "video"],
    "visibilities": ["public", "private", "restricted"],
    "mimeTypesByKind": {"image": ["image/png"]},
}
PIPELINES = {"image": {"pipeline": ["virus_scan", "image_derive"], "processors": {}}}


def _make_client() -> MediaLibraryClient:
    return MediaLibraryClient(base_url=BASE_URL, tenant_id=TENANT_ID, token="t")


@respx.mock
def test_get_schema_uses_new_endpoint() -> None:
    route = respx.get(SCHEMA_URL).mock(return_value=httpx.Response(200, json=SCHEMA))
    legacy = respx.get(LEGACY_SCHEMA_URL).mock(return_value=httpx.Response(200, json={}))

    with _make_client() as client:
        assert client.processing.get_schema() == SCHEMA

    assert route.called
    assert not legacy.called


@respx.mock
def test_get_pipelines_uses_new_endpoint() -> None:
    respx.get(PIPELINES_URL).mock(return_value=httpx.Response(200, json=PIPELINES))
    legacy = respx.get(LEGACY_PIPELINES_URL).mock(return_value=httpx.Response(200, json={}))

    with _make_client() as client:
        assert client.processing.get_pipelines() == PIPELINES

    assert not legacy.called


@respx.mock
def test_get_schema_falls_back_to_legacy_on_404() -> None:
    respx.get(SCHEMA_URL).mock(return_value=httpx.Response(404, json={"detail": "Not Found"}))
    legacy = respx.get(LEGACY_SCHEMA_URL).mock(return_value=httpx.Response(200, json=SCHEMA))

    with _make_client() as client:
        assert client.processing.get_schema() == SCHEMA

    assert legacy.called


@respx.mock
def test_get_pipelines_falls_back_to_available_steps_on_404() -> None:
    respx.get(PIPELINES_URL).mock(return_value=httpx.Response(404, json={"detail": "Not Found"}))
    legacy = respx.get(LEGACY_PIPELINES_URL).mock(return_value=httpx.Response(200, json=PIPELINES))

    with _make_client() as client:
        assert client.processing.get_pipelines() == PIPELINES

    assert legacy.called


@respx.mock
def test_no_fallback_on_other_errors() -> None:
    respx.get(SCHEMA_URL).mock(return_value=httpx.Response(500, json={"detail": "boom"}))
    legacy = respx.get(LEGACY_SCHEMA_URL).mock(return_value=httpx.Response(200, json=SCHEMA))

    with _make_client() as client, pytest.raises(ServerError):
        client.processing.get_schema()

    assert not legacy.called


@respx.mock
def test_404_on_both_endpoints_raises_not_found() -> None:
    respx.get(PIPELINES_URL).mock(return_value=httpx.Response(404, json={"detail": "Not Found"}))
    respx.get(LEGACY_PIPELINES_URL).mock(return_value=httpx.Response(404, json={"detail": "Not Found"}))

    with _make_client() as client, pytest.raises(NotFoundError):
        client.processing.get_pipelines()
