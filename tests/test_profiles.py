from __future__ import annotations

from uuid import uuid4

import pydantic
import pytest

from saas_media_library import (
    Asset,
    AssetKind,
    DeliveryAsset,
    ProcessingConfig,
    ResourcePolicy,
    UploadProfile,
    UploadRejected,
    ValidationError,
    Visibility,
)
from saas_media_library.profiles import processing_config_payload


def _profile(**overrides) -> UploadProfile:
    fields = {
        "name": "course-cover",
        "kind": AssetKind.IMAGE,
        "allowed_mime_types": {"image/png", "image/jpeg"},
        "min_size_bytes": 10,
        "max_size_bytes": 100,
    }
    fields.update(overrides)
    return UploadProfile(**fields)


def _asset(**overrides) -> Asset:
    data = {
        "id": str(uuid4()),
        "kind": "image",
        "originalFilename": "x.png",
        "mimeType": "image/png",
        "sizeBytes": 50,
        "hashSha256": "h",
        "status": "ready",
        "visibility": "private",
        "createdAt": "2026-05-08T00:00:00",
    }
    data.update(overrides)
    return Asset.model_validate(data)


def _delivery_asset(**overrides) -> DeliveryAsset:
    data = {
        "id": str(uuid4()),
        "kind": "image",
        "status": "ready",
        "visibility": "restricted",
        "mimeType": "image/png",
        "downloadType": "gate",
    }
    data.update(overrides)
    return DeliveryAsset.model_validate(data)


# ── Model construction ────────────────────────────────────────────────


def test_defaults() -> None:
    profile = UploadProfile(kind=AssetKind.VIDEO)

    assert profile.visibility == Visibility.PRIVATE
    assert profile.allowed_mime_types is None
    assert profile.min_size_bytes is None
    assert profile.max_size_bytes is None
    assert profile.allow_multiple is False
    assert profile.required_permissions == set()
    assert profile.allow_anonymous is False
    assert profile.processing_config is None
    assert profile.name is None


def test_min_above_max_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError, match="min_size_bytes cannot exceed"):
        UploadProfile(kind=AssetKind.IMAGE, min_size_bytes=10, max_size_bytes=5)


def test_negative_sizes_are_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        UploadProfile(kind=AssetKind.IMAGE, max_size_bytes=-1)
    with pytest.raises(pydantic.ValidationError):
        UploadProfile(kind=AssetKind.IMAGE, min_size_bytes=-1)


def test_unknown_field_is_rejected() -> None:
    # A typo must not silently drop a limit.
    with pytest.raises(pydantic.ValidationError):
        UploadProfile(kind=AssetKind.IMAGE, max_size=10)


def test_profile_is_immutable() -> None:
    profile = _profile()
    with pytest.raises(pydantic.ValidationError):
        profile.max_size_bytes = 1_000_000


def test_mime_types_are_normalized() -> None:
    profile = _profile(allowed_mime_types=[" Image/PNG "])
    assert profile.allowed_mime_types == {"image/png"}


def test_allowed_mime_type_parameters_are_dropped() -> None:
    profile = _profile(allowed_mime_types={"Text/Plain; charset=UTF-8", "image/svg+xml"})
    assert profile.allowed_mime_types == {"text/plain", "image/svg+xml"}


@pytest.mark.parametrize(
    "bad",
    ["png", "", " ", "image/", "/png", "image/png/x", "image / png", "image/p ng", "image/png,image/gif"],
)
def test_allowed_mime_types_must_be_type_subtype(bad) -> None:
    with pytest.raises(pydantic.ValidationError, match="not a MIME type"):
        _profile(allowed_mime_types={"image/png", bad})


def test_partial_wildcard_is_accepted_but_only_matches_itself() -> None:
    # As in the server's policies: "*/*" is the only wildcard.
    profile = _profile(allowed_mime_types={"Video/*"})

    assert profile.allowed_mime_types == {"video/*"}
    with pytest.raises(UploadRejected):
        profile.validate_file("video/mp4", 50)


# ── validate_file ─────────────────────────────────────────────────────


def test_validate_file_accepts_allowed_file() -> None:
    _profile().validate_file("image/png", 50)


def test_validate_file_rejects_mime_type() -> None:
    with pytest.raises(UploadRejected) as excinfo:
        _profile().validate_file("image/gif", 50)

    assert excinfo.value.reason == UploadRejected.MIME_TYPE
    assert "image/gif" in str(excinfo.value)
    assert "course-cover" in str(excinfo.value)
    assert excinfo.value.status_code is None


def test_validate_file_mime_type_is_case_insensitive() -> None:
    _profile().validate_file("IMAGE/PNG", 50)


@pytest.mark.parametrize("mime_type", [" image/png ", "Image/PNG; charset=binary", "image/png;q=1"])
def test_validate_file_ignores_parameters_and_whitespace(mime_type) -> None:
    _profile().validate_file(mime_type, 50)


@pytest.mark.parametrize("allowed", [None, {"*/*"}, {"image/png"}])
@pytest.mark.parametrize("mime_type", ["png", "", "image/", "*/*", "image/*", "image/png/x", "image /png"])
def test_validate_file_rejects_malformed_mime_type(allowed, mime_type) -> None:
    # Whatever the profile allows: a file's type must be a concrete type/subtype.
    with pytest.raises(UploadRejected) as excinfo:
        _profile(allowed_mime_types=allowed).validate_file(mime_type, 50)

    assert excinfo.value.reason == UploadRejected.MIME_TYPE


def test_verify_asset_normalizes_the_stored_mime_type() -> None:
    _profile().verify_asset(_asset(mimeType="Image/PNG"))
    with pytest.raises(UploadRejected):
        _profile().verify_asset(_asset(mimeType="png"))


@pytest.mark.parametrize("allowed", [None, {"*/*"}, {"*/*", "image/png"}])
def test_validate_file_any_mime_type(allowed) -> None:
    _profile(allowed_mime_types=allowed).validate_file("application/x-anything", 50)


def test_validate_file_empty_allowlist_accepts_nothing() -> None:
    # Same as the server: an empty set without "*/*" matches no type.
    with pytest.raises(UploadRejected):
        _profile(allowed_mime_types=set()).validate_file("image/png", 50)


def test_validate_file_too_large() -> None:
    with pytest.raises(UploadRejected) as excinfo:
        _profile().validate_file("image/png", 101)

    assert excinfo.value.reason == UploadRejected.TOO_LARGE
    assert "exceeds maximum 100" in str(excinfo.value)


def test_validate_file_too_small() -> None:
    with pytest.raises(UploadRejected) as excinfo:
        _profile().validate_file("image/png", 9)

    assert excinfo.value.reason == UploadRejected.TOO_SMALL
    assert "below minimum 10" in str(excinfo.value)


def test_validate_file_bounds_are_inclusive() -> None:
    profile = _profile()
    profile.validate_file("image/png", 10)
    profile.validate_file("image/png", 100)


@pytest.mark.parametrize(
    "bounds",
    [{"max_size_bytes": 100}, {"min_size_bytes": 1, "max_size_bytes": None}],
)
def test_validate_file_requires_size_when_bounds_are_set(bounds) -> None:
    profile = _profile(**{"min_size_bytes": None, **bounds})

    with pytest.raises(UploadRejected) as excinfo:
        profile.validate_file("image/png", None)

    assert excinfo.value.reason == UploadRejected.SIZE_REQUIRED


def test_validate_file_size_optional_without_bounds() -> None:
    _profile(min_size_bytes=None, max_size_bytes=None).validate_file("image/png")


def test_validate_file_checks_size_requirement_before_mime_type() -> None:
    # Mirrors the server, which checks for a missing size first.
    with pytest.raises(UploadRejected) as excinfo:
        _profile().validate_file("image/gif", None)

    assert excinfo.value.reason == UploadRejected.SIZE_REQUIRED


def test_upload_rejected_is_a_validation_error() -> None:
    with pytest.raises(ValidationError):
        _profile().validate_file("image/gif", 50)


# ── check_permissions ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "profile_kwargs, permissions, authenticated, expected",
    [
        # allow_anonymous wins over everything
        ({"allow_anonymous": True, "required_permissions": {"a"}}, None, False, True),
        # no requirement → allowed, even anonymously (server semantics)
        ({}, None, False, True),
        ({}, None, True, True),
        # requirement + anonymous → denied
        ({"required_permissions": {"a"}}, {"a"}, False, False),
        # subset match
        ({"required_permissions": {"a", "b"}}, {"a", "b", "c"}, True, True),
        ({"required_permissions": {"a", "b"}}, ["a"], True, False),
        ({"required_permissions": {"a"}}, None, True, False),
        ({"required_permissions": {"a"}}, [], True, False),
    ],
)
def test_check_permissions(profile_kwargs, permissions, authenticated, expected) -> None:
    profile = UploadProfile(kind=AssetKind.IMAGE, **profile_kwargs)
    assert profile.check_permissions(permissions, authenticated=authenticated) is expected


def test_check_permissions_accepts_a_single_string() -> None:
    profile = UploadProfile(kind=AssetKind.IMAGE, required_permissions={"media:upload"})
    assert profile.check_permissions("media:upload") is True
    # Not treated as the set of its characters.
    assert UploadProfile(kind=AssetKind.IMAGE, required_permissions={"m"}).check_permissions("media") is False


def test_check_permissions_accepts_generators() -> None:
    profile = UploadProfile(kind=AssetKind.IMAGE, required_permissions={"a"})
    assert profile.check_permissions(p for p in ["a", "b"]) is True


# ── verify_asset ──────────────────────────────────────────────────────


def test_verify_asset_accepts_matching_asset() -> None:
    _profile().verify_asset(_asset())


def test_verify_asset_rejects_kind_mismatch() -> None:
    with pytest.raises(UploadRejected) as excinfo:
        _profile().verify_asset(_asset(kind="video", mimeType="video/mp4"))

    assert excinfo.value.reason == UploadRejected.KIND


def test_verify_asset_rejects_mime_type() -> None:
    with pytest.raises(UploadRejected) as excinfo:
        _profile().verify_asset(_asset(mimeType="image/gif"))

    assert excinfo.value.reason == UploadRejected.MIME_TYPE


def test_verify_asset_checks_size_bounds() -> None:
    with pytest.raises(UploadRejected) as excinfo:
        _profile().verify_asset(_asset(sizeBytes=1000))
    assert excinfo.value.reason == UploadRejected.TOO_LARGE

    with pytest.raises(UploadRejected) as excinfo:
        _profile().verify_asset(_asset(sizeBytes=1))
    assert excinfo.value.reason == UploadRejected.TOO_SMALL


def test_verify_delivery_asset_skips_size() -> None:
    # DeliveryAsset has no size; bounds must not make it fail.
    _profile().verify_asset(_delivery_asset())


def test_verify_delivery_asset_checks_kind_and_mime() -> None:
    with pytest.raises(UploadRejected):
        _profile().verify_asset(_delivery_asset(kind="document", mimeType="application/pdf"))
    with pytest.raises(UploadRejected):
        _profile().verify_asset(_delivery_asset(mimeType="image/gif"))


# ── Serialization ─────────────────────────────────────────────────────


def test_to_session_fields_without_processing_config() -> None:
    profile = UploadProfile(kind=AssetKind.IMAGE, visibility=Visibility.PUBLIC)

    assert profile.to_session_fields() == {"kind": "image", "visibility": "public"}


def test_to_session_fields_sends_transcription_off_unless_set() -> None:
    step_options = {"image_derive": {"formats": ["webp"], "sizes": [256]}}
    profile = UploadProfile(
        kind=AssetKind.IMAGE,
        processing_config=ProcessingConfig(step_options=step_options),
    )

    assert profile.processing_config.enable_transcription is False
    assert profile.to_session_fields() == {
        "kind": "image",
        "visibility": "private",
        "processingConfig": {
            "stepOptions": step_options,
            "useFilenameAsAssetId": False,
            "enableTranscription": False,
        },
    }


def test_to_session_fields_keeps_explicit_transcription() -> None:
    profile = UploadProfile(
        kind=AssetKind.VIDEO,
        processing_config=ProcessingConfig(enable_transcription=True, use_filename_as_asset_id=True),
    )

    assert profile.to_session_fields()["processingConfig"] == {
        "stepOptions": None,
        "useFilenameAsAssetId": True,
        "enableTranscription": True,
    }


def test_processing_config_copied_from_policy_keeps_its_values() -> None:
    policy = ResourcePolicy.model_validate({
        "id": str(uuid4()),
        "tenantId": str(uuid4()),
        "resourceType": "courses",
        "collectionName": "video",
        "fileConstraints": {"allowedMimeTypes": ["video/mp4"]},
        "permissionRequirements": {},
        "processingConfig": {"stepOptions": {"video_package": {"crf": 23}}, "enableTranscription": True},
        "kind": "video",
        "visibility": "restricted",
        "isActive": True,
        "version": 1,
        "createdAt": "2026-05-08T00:00:00",
        "updatedAt": "2026-05-08T00:00:00",
    })

    profile = UploadProfile(kind=policy.kind, processing_config=policy.processing_config)

    assert profile.to_session_fields()["processingConfig"]["enableTranscription"] is True


def test_processing_config_payload_accepts_mappings() -> None:
    assert processing_config_payload({"step_options": {"a": {"x": 1}}}) == {
        "stepOptions": {"a": {"x": 1}},
        "useFilenameAsAssetId": False,
        "enableTranscription": False,
    }
    assert processing_config_payload({"enableTranscription": True})["enableTranscription"] is True
