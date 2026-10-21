from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import respx

from pkg_media import MediaLibraryClient, NotFoundError, ServerError


BASE_URL = "https://media.test"
TENANT_ID = str(uuid4())

SCHEMA_URL = f"{BASE_URL}/v1/manage/processing-options/schema"
PIPELINES_URL = f"{BASE_URL}/v1/manage/processing-options/pipelines"
LEGACY_SCHEMA_URL = f"{BASE_URL}/v1/manage/resource-policies/schema"

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
PIPELINES = {"image": {"pipeline": ["virus_scan", "image_derive"], "processors": {}}}


def _make_client() -> MediaLibraryClient:
    return MediaLibraryClient(base_url=BASE_URL, tenant_id=TENANT_ID, token="t")


@respx.mock
def test_get_schema_uses_the_processing_options_route() -> None:
    respx.get(SCHEMA_URL).mock(return_value=httpx.Response(200, json=SCHEMA))

    with _make_client() as client:
        assert client.processing.get_schema() == SCHEMA


@respx.mock
def test_get_pipelines_uses_the_processing_options_route() -> None:
    respx.get(PIPELINES_URL).mock(return_value=httpx.Response(200, json=PIPELINES))

    with _make_client() as client:
        assert client.processing.get_pipelines() == PIPELINES


@respx.mock
def test_new_schema_is_returned_unchanged() -> None:
    respx.get(SCHEMA_URL).mock(return_value=httpx.Response(200, json={**SCHEMA, "newField": 1}))

    with _make_client() as client:
        assert client.processing.get_schema() == {**SCHEMA, "newField": 1}


@respx.mock
def test_a_404_raises_instead_of_falling_back_to_the_policy_routes() -> None:
    respx.get(SCHEMA_URL).mock(return_value=httpx.Response(404, json={"detail": "Not Found"}))
    legacy = respx.get(LEGACY_SCHEMA_URL).mock(return_value=httpx.Response(200, json=SCHEMA))

    with _make_client() as client, pytest.raises(NotFoundError):
        client.processing.get_schema()

    assert not legacy.called


@respx.mock
def test_server_errors_raise() -> None:
    respx.get(PIPELINES_URL).mock(return_value=httpx.Response(500, json={"detail": "boom"}))

    with _make_client() as client, pytest.raises(ServerError):
        client.processing.get_pipelines()
