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

IMAGE_STEP_OPTIONS = {"formats": {"type": "array"}, "sizes": {"type": "array"}}
SCHEMA = {
    "kinds": ["image", "video"],
    "visibilities": ["public", "private", "restricted"],
    "mimeTypesByKind": {"image": ["image/png"]},
    "pipelinesByKind": {
        "image": {
            "pipeline": ["virus_scan", "image_derive"],
            "processors": {"virus_scan": {"options": {}},
                           "image_derive": {"options": IMAGE_STEP_OPTIONS}},
            "configurableSteps": ["image_derive"],
        },
    },
    "autoStartSteps": [],
    "gatedSteps": {"video_transcription": "enable_transcription"},
    "transcriptionAvailable": False,
    "uploadDefaults": {
        "visibility": "private",
        "processingConfig": {"stepOptions": None, "useFilenameAsAssetId": False,
                             "enableTranscription": False},
    },
}
# GET /manage/resource-policies/schema on an older server: snake_case keys,
# policy defaults, no upload metadata.
LEGACY_SCHEMA = {
    "kinds": ["image", "video"],
    "visibilities": ["public", "private", "restricted"],
    "mime_types_by_kind": {"image": ["image/png"]},
    "pipelines_by_kind": {
        "image": {
            "pipeline": ["virus_scan", "image_derive"],
            "processors": {"virus_scan": {"options": {}},
                           "image_derive": {"options": IMAGE_STEP_OPTIONS}},
            "configurable_steps": ["image_derive"],
        },
    },
    "permission_schema": {"supports_permissions": True, "supports_anonymous": True},
    "defaults": {
        "kind": "image",
        "visibility": "public",
        "file_constraints": {"allowed_mime_types": [], "max_size_bytes": None,
                             "min_size_bytes": None, "allow_multiple": False},
        "permission_requirements": {"required_permissions": [], "allow_anonymous": False},
        "processing_config": {"step_options": None},
    },
}
# The keys the legacy payload yields: the new schema minus what it cannot derive.
UNDERIVABLE_KEYS = {"autoStartSteps", "gatedSteps", "transcriptionAvailable", "uploadDefaults"}
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
    legacy = respx.get(LEGACY_SCHEMA_URL).mock(return_value=httpx.Response(200, json=LEGACY_SCHEMA))

    with _make_client() as client:
        schema = client.processing.get_schema()

    assert legacy.called
    # Same shape as the new route, minus the keys the legacy payload lacks.
    assert schema == {k: v for k, v in SCHEMA.items() if k not in UNDERIVABLE_KEYS}


@respx.mock
def test_legacy_schema_uses_the_new_keys_only() -> None:
    respx.get(SCHEMA_URL).mock(return_value=httpx.Response(404, json={"detail": "Not Found"}))
    respx.get(LEGACY_SCHEMA_URL).mock(return_value=httpx.Response(200, json=LEGACY_SCHEMA))

    with _make_client() as client:
        schema = client.processing.get_schema()

    assert set(schema) == set(SCHEMA) - UNDERIVABLE_KEYS
    assert schema["mimeTypesByKind"] == LEGACY_SCHEMA["mime_types_by_kind"]
    assert schema["pipelinesByKind"]["image"]["configurableSteps"] == ["image_derive"]
    assert "configurable_steps" not in schema["pipelinesByKind"]["image"]
    # Policy defaults are not upload defaults: dropped, not converted.
    assert "defaults" not in schema and "uploadDefaults" not in schema


@respx.mock
def test_new_schema_is_returned_unchanged() -> None:
    respx.get(SCHEMA_URL).mock(return_value=httpx.Response(200, json={**SCHEMA, "newField": 1}))

    with _make_client() as client:
        assert client.processing.get_schema() == {**SCHEMA, "newField": 1}


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
