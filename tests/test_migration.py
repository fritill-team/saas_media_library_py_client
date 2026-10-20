from __future__ import annotations

import subprocess
import sys
import warnings
from uuid import uuid4

import httpx
import pytest
import respx

from saas_media_library import (
    AssetKind,
    ProcessingConfig,
    ResourcePolicy,
    UploadProfile,
    Visibility,
    find_profile,
    policy_key,
    profiles_from_policies,
    render_profiles_module,
)
from saas_media_library.migrate_policies import main


BASE_URL = "https://media.test"
POLICIES_URL = f"{BASE_URL}/v1/manage/resource-policies"

STEP_OPTIONS = {
    "video_package": {"crf": 23, "preset": "fast", "resolutions": ["360p", "720p", "1080p"]},
    "video_thumbnail": {"timestamp": 1.0},
}


def _policy_json(
    resource_type: str = "courses",
    collection_name=None,
    *,
    kind: str = "video",
    visibility: str = "restricted",
    processing_config=None,
    is_active: bool = True,
    **constraints,
) -> dict:
    file_constraints = {
        "allowedMimeTypes": ["video/mp4", "video/quicktime"],
        "maxSizeBytes": 2_000_000_000,
        "minSizeBytes": 1024,
        "allowMultiple": True,
    }
    file_constraints.update(constraints)
    return {
        "id": str(uuid4()),
        "tenantId": str(uuid4()),
        "resourceType": resource_type,
        "collectionName": collection_name,
        "fileConstraints": file_constraints,
        "permissionRequirements": {"requiredPermissions": ["courses:write", "media:upload"],
                                   "allowAnonymous": False},
        "processingConfig": processing_config if processing_config is not None else {
            "stepOptions": STEP_OPTIONS,
            "useFilenameAsAssetId": False,
            "enableTranscription": True,
        },
        "kind": kind,
        "visibility": visibility,
        "isActive": is_active,
        "version": 3,
        "createdAt": "2026-05-08T00:00:00",
        "updatedAt": "2026-05-08T00:00:00",
    }


def _policy(*args, **kwargs) -> ResourcePolicy:
    return ResourcePolicy.model_validate(_policy_json(*args, **kwargs))


def _load(source: str) -> dict:
    namespace: dict = {}
    exec(compile(source, "upload_profiles.py", "exec"), namespace)
    return namespace["UPLOAD_PROFILES"]


# ── profiles_from_policies ─────────────────────────────────────────────


def test_policy_key() -> None:
    assert policy_key("courses", "cover") == "courses/cover"
    assert policy_key("courses") == "courses"
    assert policy_key("courses", None) == "courses"


def test_profiles_carry_every_policy_field() -> None:
    [(key, profile)] = profiles_from_policies([_policy("courses", "video")]).items()

    assert key == "courses/video"
    assert profile == UploadProfile(
        name="courses/video",
        kind=AssetKind.VIDEO,
        visibility=Visibility.RESTRICTED,
        allowed_mime_types={"video/mp4", "video/quicktime"},
        min_size_bytes=1024,
        max_size_bytes=2_000_000_000,
        allow_multiple=True,
        required_permissions={"courses:write", "media:upload"},
        allow_anonymous=False,
        processing_config=ProcessingConfig(
            step_options=STEP_OPTIONS,
            use_filename_as_asset_id=False,
            enable_transcription=True,
        ),
    )


def test_policy_without_collection_is_keyed_by_resource_type() -> None:
    profiles = profiles_from_policies([_policy("avatars")])

    assert list(profiles) == ["avatars"]
    assert profiles["avatars"].name == "avatars"


@pytest.mark.parametrize("enabled", [True, False])
def test_enable_transcription_is_kept_as_the_policy_had_it(enabled) -> None:
    policy = _policy(processing_config={"stepOptions": None, "useFilenameAsAssetId": True,
                                        "enableTranscription": enabled})

    profile = profiles_from_policies([policy])["courses"]

    assert profile.processing_config.enable_transcription is enabled
    assert profile.processing_config.use_filename_as_asset_id is True
    assert profile.to_session_fields()["processingConfig"]["enableTranscription"] is enabled


def test_processing_config_defaults_to_the_upload_default() -> None:
    # The server always sends enableTranscription for a policy; a config
    # without it is the server's upload default (off).
    policy = _policy(processing_config={"stepOptions": None})

    profile = profiles_from_policies([policy])["courses"]

    assert ProcessingConfig().enable_transcription is False
    assert policy.processing_config.enable_transcription is False
    assert profile.processing_config.enable_transcription is False


def test_other_policy_settings_carry_over() -> None:
    policy = ResourcePolicy.model_validate({
        **_policy_json("public", "files", kind="image", visibility="public",
                       allowedMimeTypes=["*/*"], maxSizeBytes=None, minSizeBytes=None,
                       allowMultiple=False),
        "permissionRequirements": {"requiredPermissions": [], "allowAnonymous": True},
    })

    profile = profiles_from_policies([policy])["public/files"]

    assert profile.kind == AssetKind.IMAGE and profile.visibility == Visibility.PUBLIC
    assert profile.allowed_mime_types == {"*/*"}
    assert profile.min_size_bytes is None and profile.max_size_bytes is None
    assert profile.allow_multiple is False
    assert profile.required_permissions == set() and profile.allow_anonymous is True


def test_step_options_are_copied() -> None:
    policy = _policy()
    profile = profiles_from_policies([policy])["courses"]

    policy.processing_config.step_options["video_package"]["crf"] = 99

    assert profile.processing_config.step_options["video_package"]["crf"] == 23


def test_profiles_are_sorted_by_key() -> None:
    policies = [_policy("courses", "video"), _policy("avatars"), _policy("courses", "cover")]

    assert list(profiles_from_policies(policies)) == ["avatars", "courses/cover", "courses/video"]


def test_duplicate_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="courses/cover"):
        profiles_from_policies([_policy("courses", "cover"), _policy("courses", "cover")])


def test_no_policies() -> None:
    assert profiles_from_policies([]) == {}


def test_policy_with_malformed_mime_type_names_the_policy() -> None:
    policy = _policy("courses", "cover", allowedMimeTypes=["image/png", "png"])

    with pytest.raises(ValueError, match=r"(?s)'courses/cover'.*'png' is not a MIME type"):
        profiles_from_policies([policy])


def test_policy_mime_types_are_normalized() -> None:
    policy = _policy("courses", "cover", allowedMimeTypes=["Image/PNG", "*/*", "video/*"])

    profile = profiles_from_policies([policy])["courses/cover"]

    assert profile.allowed_mime_types == {"image/png", "*/*", "video/*"}


# ── find_profile ───────────────────────────────────────────────────────


def _profiles_for_lookup() -> dict:
    return profiles_from_policies([_policy("courses"), _policy("courses", "cover"), _policy("avatars", "big")])


def test_find_profile_exact_match_wins() -> None:
    profiles = _profiles_for_lookup()

    assert find_profile(profiles, "courses", "cover") is profiles["courses/cover"]


def test_find_profile_falls_back_to_the_resource_type_default() -> None:
    # The server served ("courses", "banner") with the default "courses"
    # policy; the profile lookup must too.
    profiles = _profiles_for_lookup()

    assert find_profile(profiles, "courses", "banner") is profiles["courses"]
    assert find_profile(profiles, "courses") is profiles["courses"]
    assert find_profile(profiles, "courses", None) is profiles["courses"]


def test_find_profile_without_a_match_is_none() -> None:
    profiles = _profiles_for_lookup()

    # No default for "avatars", and never another collection's profile.
    assert find_profile(profiles, "avatars", "small") is None
    assert find_profile(profiles, "avatars") is None
    assert find_profile(profiles, "videos", "cover") is None
    assert find_profile({}, "courses", "cover") is None


def test_find_profile_works_on_a_generated_module() -> None:
    loaded = _load(render_profiles_module(_profiles_for_lookup()))

    assert find_profile(loaded, "courses", "banner") == loaded["courses"]


# ── render_profiles_module ─────────────────────────────────────────────


def test_rendered_module_round_trips() -> None:
    profiles = profiles_from_policies([
        _policy("courses", "video"),
        _policy("courses", "cover", kind="image", visibility="public",
                allowedMimeTypes=["image/png", "image/jpeg"], maxSizeBytes=5_000_000,
                minSizeBytes=None,
                processing_config={"stepOptions": {"image_derive": {"formats": ["webp"],
                                                                    "sizes": [256, 512]}},
                                   "useFilenameAsAssetId": False, "enableTranscription": False}),
        _policy("model_3d", kind="model_3d", allowedMimeTypes=[]),
    ])

    loaded = _load(render_profiles_module(profiles))

    assert loaded == profiles
    assert list(loaded) == list(profiles)
    assert loaded["courses/video"].processing_config.enable_transcription is True
    assert loaded["courses/cover"].processing_config.enable_transcription is False
    assert loaded["model_3d"].allowed_mime_types == set()
    # Loaded profiles send the same session fields as the originals.
    for key, profile in profiles.items():
        assert loaded[key].to_session_fields() == profile.to_session_fields()


def test_rendered_module_round_trips_hand_written_profiles() -> None:
    profiles = {
        "any": UploadProfile(kind=AssetKind.OTHER),
        "quoted 'name' \"x\"": UploadProfile(
            name="it's \"quoted\" — ünïcode",
            kind=AssetKind.DOCUMENT,
            processing_config=ProcessingConfig(step_options={"note": {"text": "a\nb", "n": None}}),
        ),
    }

    loaded = _load(render_profiles_module(profiles))

    assert loaded == profiles
    assert loaded["any"].allowed_mime_types is None
    assert loaded["any"].processing_config is None


def test_rendered_module_is_readable() -> None:
    source = render_profiles_module(profiles_from_policies([_policy("courses", "video")]))

    assert source.startswith('"""Upload profiles converted from resource policies.')
    assert "from saas_media_library import AssetKind, ProcessingConfig, UploadProfile, Visibility" in source
    assert "    'courses/video': UploadProfile(\n" in source
    assert "        kind=AssetKind.VIDEO,\n" in source
    assert "        visibility=Visibility.RESTRICTED,\n" in source
    assert "        allowed_mime_types={'video/mp4', 'video/quicktime'},\n" in source
    assert "            enable_transcription=True,\n" in source
    assert max(len(line) for line in source.splitlines()) <= 100


def test_rendered_module_is_deterministic() -> None:
    policies = [_policy("courses", "video"), _policy("avatars")]

    assert render_profiles_module(profiles_from_policies(policies)) == render_profiles_module(
        profiles_from_policies(list(reversed(policies)))
    )


def test_rendered_empty_module() -> None:
    assert _load(render_profiles_module({})) == {}


# ── CLI ────────────────────────────────────────────────────────────────


ARGS = ["--base-url", BASE_URL, "--tenant", "tenant-1", "--token", "svc"]


@respx.mock
def test_cli_prints_module_for_active_policies(capsys) -> None:
    route = respx.get(POLICIES_URL).mock(return_value=httpx.Response(
        200, json=[_policy_json("courses", "video"), _policy_json("avatars")]))

    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        assert main(ARGS) == 0

    assert not [w for w in record if issubclass(w.category, DeprecationWarning)]
    request = route.calls.last.request
    assert dict(request.url.params) == {"is_active": "true"}
    assert request.headers["X-Tenant"] == "tenant-1"
    assert request.headers["Authorization"] == "Bearer svc"
    loaded = _load(capsys.readouterr().out)
    assert list(loaded) == ["avatars", "courses/video"]


@respx.mock
def test_cli_include_inactive(capsys) -> None:
    route = respx.get(POLICIES_URL).mock(return_value=httpx.Response(
        200, json=[_policy_json("old", is_active=False)]))

    assert main(ARGS + ["--include-inactive"]) == 0

    assert "is_active" not in route.calls.last.request.url.params
    assert list(_load(capsys.readouterr().out)) == ["old"]


@respx.mock
def test_cli_token_from_environment(monkeypatch, capsys) -> None:
    monkeypatch.setenv("MEDIA_LIBRARY_TOKEN", "env-token")
    route = respx.get(POLICIES_URL).mock(return_value=httpx.Response(200, json=[]))

    assert main(["--base-url", BASE_URL, "--tenant", "t"]) == 0

    assert route.calls.last.request.headers["Authorization"] == "Bearer env-token"
    assert _load(capsys.readouterr().out) == {}


def test_cli_requires_token(monkeypatch, capsys) -> None:
    monkeypatch.delenv("MEDIA_LIBRARY_TOKEN", raising=False)

    with pytest.raises(SystemExit) as exc:
        main(["--base-url", BASE_URL, "--tenant", "t"])

    assert exc.value.code == 2
    assert "--token" in capsys.readouterr().err


@respx.mock
def test_cli_reports_server_errors(capsys) -> None:
    respx.get(POLICIES_URL).mock(return_value=httpx.Response(401, json={"detail": "bad token"}))

    assert main(ARGS) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "bad token" in captured.err


def test_cli_runs_as_module() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "saas_media_library.migrate_policies", "--help"],
        capture_output=True, text=True, check=False,
    )

    assert proc.returncode == 0
    assert "--include-inactive" in proc.stdout
    assert proc.stderr == ""
