from __future__ import annotations

import hashlib
import io
import json
import os
import threading
from typing import List
from uuid import uuid4

import httpx
import pytest
import respx

from pkg_media import (
    AssetKind,
    AuthenticationError,
    ConflictError,
    MediaLibraryClient,
    ProcessingConfig,
    ServerError,
    UploadFinalizeResult,
    UploadProfile,
    UploadRejected,
    UploadSession,
    Visibility,
)


BASE_URL = "https://media.test"
TENANT_ID = str(uuid4())
SESSIONS_URL = f"{BASE_URL}/v1/manage/uploads/sessions"


def _make_client() -> MediaLibraryClient:
    return MediaLibraryClient(base_url=BASE_URL, tenant_id=TENANT_ID, token="t")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _session_response(session_id, *, part_size: int = 4, size=None, state: str = "in_progress") -> dict:
    return {
        "id": str(session_id),
        "state": state,
        "part_size_bytes": part_size,
        "parts_expected": None if size is None else -(-size // part_size),
        "parts_received": 0,
        "received_parts": [],
        "expires_at": "2026-05-08T01:00:00",
        "checksum_verified": False,
        "final_blob_id": None,
    }


def _finalize_response(session_id, sha256_hex: str) -> dict:
    return {
        "session_id": str(session_id),
        "final_blob_id": str(uuid4()),
        "asset_id": str(uuid4()),
        "checksum_verified": True,
        "sha256_hex": sha256_hex,
    }


def _part_response(session_id, part_number: int, data: bytes) -> dict:
    return {
        "session_id": str(session_id),
        "part_number": part_number,
        "size_bytes": len(data),
        "sha256_hex": _sha(data),
        "verified": True,
        "parts_received": part_number,
    }


IMAGE_PROFILE = UploadProfile(
    name="course-cover",
    kind=AssetKind.IMAGE,
    visibility=Visibility.PUBLIC,
    allowed_mime_types={"image/png", "image/jpeg"},
    max_size_bytes=1_000,
    processing_config=ProcessingConfig(step_options={"image_derive": {"formats": ["webp"]}}),
)


class _UploadServer:
    """Mocks the four upload endpoints and records what the client sent."""

    def __init__(self, router: respx.MockRouter, *, part_size: int = 4):
        self.session_id = uuid4()
        self.part_size = part_size
        self.create_body: dict = {}
        self.parts: List[httpx.Request] = []
        self.finalize_params: dict = {}
        base = f"{SESSIONS_URL}/{self.session_id}"

        self.create = router.post(SESSIONS_URL).mock(side_effect=self._create)
        self.part = router.put(url__regex=rf"{base}/parts/\d+").mock(side_effect=self._part)
        self.finalize = router.post(f"{base}/finalize").mock(side_effect=self._finalize)
        self.abort = router.post(f"{base}/abort").mock(
            return_value=httpx.Response(200, json={"session_id": str(self.session_id), "aborted": True})
        )
        self.get = router.get(base).mock(
            return_value=httpx.Response(200, json=_session_response(self.session_id))
        )

    def _create(self, request: httpx.Request) -> httpx.Response:
        self.create_body = json.loads(request.content)
        size = self.create_body.get("expected_size_bytes")
        return httpx.Response(
            201, json=_session_response(self.session_id, part_size=self.part_size, size=size)
        )

    def _part(self, request: httpx.Request) -> httpx.Response:
        self.parts.append(request)
        part_number = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json=_part_response(self.session_id, part_number, request.content))

    def _finalize(self, request: httpx.Request) -> httpx.Response:
        self.finalize_params = dict(request.url.params)
        received = b"".join(p.content for p in self.parts)
        return httpx.Response(200, json=_finalize_response(self.session_id, _sha(received)))


# ── create_session ────────────────────────────────────────────────────


@respx.mock
def test_create_session_with_profile_sends_policy_less_fields() -> None:
    server = _UploadServer(respx.mock)

    with _make_client() as client:
        session = client.uploads.create_session(
            mime_type="image/png",
            profile=IMAGE_PROFILE,
            original_filename="cover.png",
            expected_size_bytes=10,
            expected_sha256_hex="abc",
            collection_name="cover",
        )

    assert isinstance(session, UploadSession)
    assert session.id == server.session_id
    assert session.part_size_bytes == 4
    assert session.parts_expected == 3
    assert server.create_body == {
        "mime_type": "image/png",
        "kind": "image",
        "visibility": "public",
        "processingConfig": {
            "stepOptions": {"image_derive": {"formats": ["webp"]}},
            "useFilenameAsAssetId": False,
            "enableTranscription": False,
        },
        "original_filename": "cover.png",
        "expected_size_bytes": 10,
        "expected_sha256_hex": "abc",
        "collection_name": "cover",
    }
    assert "resource_type" not in server.create_body


@respx.mock
def test_create_session_explicit_args_override_profile() -> None:
    server = _UploadServer(respx.mock)

    with _make_client() as client:
        client.uploads.create_session(
            mime_type="image/png",
            profile=IMAGE_PROFILE,
            kind=AssetKind.OTHER,
            visibility=Visibility.RESTRICTED,
            processing_config={"enable_transcription": True},
            expected_size_bytes=10,
        )

    assert server.create_body["kind"] == "other"
    assert server.create_body["visibility"] == "restricted"
    assert server.create_body["processingConfig"] == {
        "stepOptions": None,
        "useFilenameAsAssetId": False,
        "enableTranscription": True,
    }


@respx.mock
def test_create_session_without_profile_sends_only_given_fields() -> None:
    server = _UploadServer(respx.mock)

    with _make_client() as client:
        client.uploads.create_session(mime_type="video/mp4", kind="video")

    # No visibility/processingConfig → the server defaults apply.
    assert server.create_body == {"mime_type": "video/mp4", "kind": "video"}


def test_create_session_requires_kind() -> None:
    with _make_client() as client, pytest.raises(TypeError, match="kind"):
        client.uploads.create_session(mime_type="image/png")


@respx.mock
def test_create_session_validates_against_profile_before_sending() -> None:
    server = _UploadServer(respx.mock)

    with _make_client() as client:
        with pytest.raises(UploadRejected) as excinfo:
            client.uploads.create_session(
                mime_type="image/gif", profile=IMAGE_PROFILE, expected_size_bytes=10
            )
        assert excinfo.value.reason == UploadRejected.MIME_TYPE

        # The profile has a max size, so the size is required.
        with pytest.raises(UploadRejected) as excinfo:
            client.uploads.create_session(mime_type="image/png", profile=IMAGE_PROFILE)
        assert excinfo.value.reason == UploadRejected.SIZE_REQUIRED

    assert not server.create.called


@respx.mock
def test_create_session_validate_false_skips_profile_checks() -> None:
    server = _UploadServer(respx.mock)

    with _make_client() as client:
        client.uploads.create_session(mime_type="image/gif", profile=IMAGE_PROFILE, validate=False)

    assert server.create.called
    assert server.create_body["mime_type"] == "image/gif"


@respx.mock
def test_create_session_403_raises_auth_error() -> None:
    respx.post(SESSIONS_URL).mock(
        return_value=httpx.Response(403, json={"detail": "Authentication is required to upload"})
    )

    with _make_client() as client, pytest.raises(AuthenticationError):
        client.uploads.create_session(mime_type="image/png", kind="image")


# ── get_session / upload_part / finalize / abort ──────────────────────


@respx.mock
def test_get_session() -> None:
    session_id = uuid4()
    body = _session_response(session_id, size=10)
    body["received_parts"] = [1, 3]
    respx.get(f"{SESSIONS_URL}/{session_id}").mock(return_value=httpx.Response(200, json=body))

    with _make_client() as client:
        session = client.uploads.get_session(session_id)

    assert session.state == "in_progress"
    assert session.received_parts == [1, 3]
    assert session.parts_expected == 3


@respx.mock
def test_upload_part_computes_checksum_and_sends_headers() -> None:
    session_id = uuid4()
    data = b"hello"
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json=_part_response(session_id, 2, request.content))

    respx.put(f"{SESSIONS_URL}/{session_id}/parts/2").mock(side_effect=handler)

    with _make_client() as client:
        result = client.uploads.upload_part(session_id, 2, data, offset=4, total_size_bytes=20)

    request = captured["request"]
    assert request.content == data
    assert request.headers["X-Part-SHA256"] == _sha(data)
    assert request.headers["Content-Length"] == "5"
    assert request.headers["Content-Range"] == "bytes 4-8/20"
    assert request.headers["Content-Type"] == "application/octet-stream"
    assert result.part_number == 2
    assert result.sha256_hex == _sha(data)


@respx.mock
def test_upload_part_uses_given_checksum_and_omits_range_without_offset() -> None:
    session_id = uuid4()
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200, json=_part_response(session_id, 1, request.content))

    respx.put(f"{SESSIONS_URL}/{session_id}/parts/1").mock(side_effect=handler)

    with _make_client() as client:
        client.uploads.upload_part(session_id, 1, b"abc", sha256_hex="given")

    assert captured["headers"]["X-Part-SHA256"] == "given"
    assert "Content-Range" not in captured["headers"]


@respx.mock
def test_upload_part_unknown_total_uses_star() -> None:
    session_id = uuid4()
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200, json=_part_response(session_id, 1, request.content))

    respx.put(f"{SESSIONS_URL}/{session_id}/parts/1").mock(side_effect=handler)

    with _make_client() as client:
        client.uploads.upload_part(session_id, 1, b"abc", offset=0)

    assert captured["headers"]["Content-Range"] == "bytes 0-2/*"


@respx.mock
def test_finalize_sends_expected_checksum_as_query_param() -> None:
    session_id = uuid4()
    route = respx.post(f"{SESSIONS_URL}/{session_id}/finalize").mock(
        return_value=httpx.Response(200, json=_finalize_response(session_id, "abc"))
    )

    with _make_client() as client:
        result = client.uploads.finalize(session_id, expected_sha256_hex="abc")
        client.uploads.finalize(session_id)

    assert isinstance(result, UploadFinalizeResult)
    assert result.sha256_hex == "abc"
    assert route.calls[0].request.url.params["expected_sha256_hex"] == "abc"
    assert "expected_sha256_hex" not in route.calls[1].request.url.params


@respx.mock
def test_finalize_checksum_mismatch_raises_conflict() -> None:
    session_id = uuid4()
    respx.post(f"{SESSIONS_URL}/{session_id}/finalize").mock(
        return_value=httpx.Response(409, json={"error": "Checksum Mismatch", "detail": "checksum mismatch"})
    )

    with _make_client() as client, pytest.raises(ConflictError):
        client.uploads.finalize(session_id, expected_sha256_hex="bad")


@respx.mock
def test_abort() -> None:
    session_id = uuid4()
    respx.post(f"{SESSIONS_URL}/{session_id}/abort").mock(
        return_value=httpx.Response(200, json={"session_id": str(session_id), "aborted": True})
    )

    with _make_client() as client:
        result = client.uploads.abort(session_id)

    assert result.session_id == session_id
    assert result.aborted is True


# ── upload_file ───────────────────────────────────────────────────────


@respx.mock
def test_upload_file_bytes_in_multiple_parts() -> None:
    server = _UploadServer(respx.mock, part_size=4)
    data = b"0123456789"  # 3 parts: 4 + 4 + 2

    with _make_client() as client:
        result = client.uploads.upload_file(
            data, profile=IMAGE_PROFILE, mime_type="image/png", filename="cover.png",
            collection_name="cover",
        )

    assert isinstance(result, UploadFinalizeResult)
    assert result.session_id == server.session_id

    assert server.create_body["expected_size_bytes"] == 10
    assert server.create_body["original_filename"] == "cover.png"
    assert server.create_body["collection_name"] == "cover"
    assert server.create_body["kind"] == "image"
    assert "resource_type" not in server.create_body
    # The whole-file checksum is verified at finalize, not sent up front.
    assert "expected_sha256_hex" not in server.create_body

    paths = [p.url.path.rsplit("/", 1)[1] for p in server.parts]
    assert paths == ["1", "2", "3"]
    chunks = [p.content for p in server.parts]
    assert chunks == [b"0123", b"4567", b"89"]
    assert [p.headers["X-Part-SHA256"] for p in server.parts] == [_sha(c) for c in chunks]
    assert [p.headers["Content-Length"] for p in server.parts] == ["4", "4", "2"]
    assert [p.headers["Content-Range"] for p in server.parts] == [
        "bytes 0-3/10",
        "bytes 4-7/10",
        "bytes 8-9/10",
    ]

    assert server.finalize_params == {"expected_sha256_hex": _sha(data)}
    assert not server.abort.called


@respx.mock
def test_upload_file_from_path_guesses_mime_type(tmp_path) -> None:
    server = _UploadServer(respx.mock, part_size=8)
    path = tmp_path / "cover.png"
    data = b"x" * 20
    path.write_bytes(data)

    with _make_client() as client:
        client.uploads.upload_file(path, profile=IMAGE_PROFILE)

    assert server.create_body["mime_type"] == "image/png"
    assert server.create_body["original_filename"] == "cover.png"
    assert server.create_body["expected_size_bytes"] == 20
    assert [p.content for p in server.parts] == [data[:8], data[8:16], data[16:]]
    assert server.finalize_params == {"expected_sha256_hex": _sha(data)}


@respx.mock
def test_upload_file_from_str_path(tmp_path) -> None:
    server = _UploadServer(respx.mock, part_size=100)
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"jpeg-bytes")

    with _make_client() as client:
        client.uploads.upload_file(str(path), profile=IMAGE_PROFILE)

    assert server.create_body["mime_type"] == "image/jpeg"
    assert [p.content for p in server.parts] == [b"jpeg-bytes"]


@respx.mock
def test_upload_file_from_seekable_stream_starts_at_current_position(tmp_path) -> None:
    server = _UploadServer(respx.mock, part_size=4)
    path = tmp_path / "cover.png"
    path.write_bytes(b"HEADER" + b"payload!!")

    with _make_client() as client, open(path, "rb") as f:
        f.seek(6)
        client.uploads.upload_file(f, profile=IMAGE_PROFILE)

    assert server.create_body["expected_size_bytes"] == 9
    assert server.create_body["original_filename"] == "cover.png"
    assert b"".join(p.content for p in server.parts) == b"payload!!"


@respx.mock
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_upload_file_from_fifo_path_reads_the_stream(tmp_path) -> None:
    # A FIFO reports st_size 0: its size must come from reading it.
    server = _UploadServer(respx.mock, part_size=2048)
    fifo = tmp_path / "export.bin"
    os.mkfifo(fifo)
    data = bytes(range(256)) * 20
    writer = threading.Thread(target=fifo.write_bytes, args=(data,), daemon=True)
    writer.start()

    with _make_client() as client:
        client.uploads.upload_file(fifo, profile=UploadProfile(kind=AssetKind.OTHER))
    writer.join(timeout=5)

    assert server.create_body["expected_size_bytes"] == len(data)
    assert server.create_body["original_filename"] == "export.bin"
    assert [len(p.content) for p in server.parts] == [2048, 2048, 1024]
    assert b"".join(p.content for p in server.parts) == data
    assert server.finalize_params == {"expected_sha256_hex": _sha(data)}


@respx.mock
@pytest.mark.skipif(not os.path.isfile("/proc/self/status"), reason="needs procfs")
def test_upload_file_from_procfs_path_is_not_empty() -> None:
    # /proc files are "regular" but report st_size 0.
    server = _UploadServer(respx.mock, part_size=1 << 20)

    with _make_client() as client:
        client.uploads.upload_file("/proc/self/status", profile=UploadProfile(kind=AssetKind.OTHER),
                                   mime_type="text/plain")

    sent = b"".join(p.content for p in server.parts)
    assert server.create_body["expected_size_bytes"] == len(sent) > 0
    assert server.finalize_params == {"expected_sha256_hex": _sha(sent)}


@respx.mock
def test_upload_file_from_empty_regular_file(tmp_path) -> None:
    server = _UploadServer(respx.mock)
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")

    with _make_client() as client:
        client.uploads.upload_file(path, profile=UploadProfile(kind=AssetKind.OTHER))

    assert server.create_body["expected_size_bytes"] == 0
    assert server.parts == []
    assert server.finalize_params == {"expected_sha256_hex": _sha(b"")}


class _NonSeekable(io.RawIOBase):
    """A pipe-like stream: no seek, short reads."""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def readinto(self, b) -> int:
        chunk = self._buf.read(min(len(b), 3))
        b[: len(chunk)] = chunk
        return len(chunk)


@respx.mock
def test_upload_file_from_non_seekable_stream() -> None:
    server = _UploadServer(respx.mock, part_size=4)
    data = b"stream-data"

    with _make_client() as client:
        client.uploads.upload_file(_NonSeekable(data), profile=IMAGE_PROFILE, mime_type="image/png")

    assert server.create_body["expected_size_bytes"] == len(data)
    assert "original_filename" not in server.create_body
    assert [p.content for p in server.parts] == [b"stre", b"am-d", b"ata"]
    assert server.finalize_params == {"expected_sha256_hex": _sha(data)}


@respx.mock
def test_upload_file_unknown_extension_falls_back_to_octet_stream() -> None:
    server = _UploadServer(respx.mock)
    profile = UploadProfile(kind=AssetKind.OTHER)

    with _make_client() as client:
        client.uploads.upload_file(b"abc", profile=profile, filename="blob.unknownext")

    assert server.create_body["mime_type"] == "application/octet-stream"


@respx.mock
def test_upload_file_custom_part_size() -> None:
    server = _UploadServer(respx.mock, part_size=4)

    with _make_client() as client:
        client.uploads.upload_file(
            b"0123456789", profile=IMAGE_PROFILE, mime_type="image/png", part_size_bytes=6
        )

    assert [p.content for p in server.parts] == [b"012345", b"6789"]


@respx.mock
def test_upload_file_part_size_below_server_size_aborts() -> None:
    server = _UploadServer(respx.mock, part_size=4)

    with _make_client() as client, pytest.raises(ValueError, match="smaller than the server's part size"):
        client.uploads.upload_file(
            b"0123456789", profile=IMAGE_PROFILE, mime_type="image/png", part_size_bytes=2
        )

    assert not server.part.called
    assert server.abort.called


def test_upload_file_rejects_non_positive_part_size() -> None:
    with _make_client() as client, pytest.raises(ValueError, match="positive"):
        client.uploads.upload_file(b"abc", profile=IMAGE_PROFILE, mime_type="image/png", part_size_bytes=0)


@respx.mock
def test_upload_file_without_checksum_verification() -> None:
    server = _UploadServer(respx.mock)

    with _make_client() as client:
        client.uploads.upload_file(b"abcdef", profile=IMAGE_PROFILE, mime_type="image/png", verify_checksum=False)

    assert server.finalize_params == {}
    # Part checksums are always sent: the server requires them.
    assert all("X-Part-SHA256" in p.headers for p in server.parts)


@respx.mock
def test_upload_file_empty_source_uploads_no_parts() -> None:
    server = _UploadServer(respx.mock)
    profile = UploadProfile(kind=AssetKind.OTHER)

    with _make_client() as client:
        client.uploads.upload_file(b"", profile=profile, mime_type="text/plain")

    assert server.create_body["expected_size_bytes"] == 0
    assert not server.part.called
    assert server.finalize_params == {"expected_sha256_hex": _sha(b"")}


@respx.mock
def test_upload_file_rejected_by_profile_sends_nothing() -> None:
    server = _UploadServer(respx.mock)

    with _make_client() as client:
        with pytest.raises(UploadRejected) as excinfo:
            client.uploads.upload_file(b"GIF89a", profile=IMAGE_PROFILE, filename="anim.gif")
        assert excinfo.value.reason == UploadRejected.MIME_TYPE

        with pytest.raises(UploadRejected) as excinfo:
            client.uploads.upload_file(b"x" * 1_001, profile=IMAGE_PROFILE, mime_type="image/png")
        assert excinfo.value.reason == UploadRejected.TOO_LARGE

    assert not server.create.called
    assert not server.part.called


@respx.mock
def test_upload_file_part_failure_aborts_and_reraises() -> None:
    server = _UploadServer(respx.mock, part_size=4)
    calls = {"n": 0}

    def flaky_part(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.Response(500, json={"detail": "disk full"})
        return httpx.Response(200, json=_part_response(server.session_id, calls["n"], request.content))

    server.part.mock(side_effect=flaky_part)

    with _make_client() as client, pytest.raises(ServerError, match="disk full"):
        client.uploads.upload_file(b"0123456789", profile=IMAGE_PROFILE, mime_type="image/png")

    assert calls["n"] == 2  # stopped at the failing part
    assert server.abort.called
    assert not server.finalize.called
    assert not server.get.called


@respx.mock
def test_upload_file_abort_failure_does_not_mask_original_error() -> None:
    server = _UploadServer(respx.mock)
    server.part.mock(return_value=httpx.Response(500, json={"detail": "boom"}))
    server.abort.mock(side_effect=httpx.ConnectError("down"))

    with _make_client() as client, pytest.raises(ServerError, match="boom"):
        client.uploads.upload_file(b"abc", profile=IMAGE_PROFILE, mime_type="image/png")

    assert server.abort.called


@respx.mock
def test_upload_file_source_read_error_aborts() -> None:
    server = _UploadServer(respx.mock)

    class _Broken(io.BytesIO):
        def read(self, *args):
            raise OSError("read failed")

    with _make_client() as client, pytest.raises(OSError, match="read failed"):
        client.uploads.upload_file(_Broken(b"abcdef"), profile=IMAGE_PROFILE, mime_type="image/png")

    assert server.create.called
    assert server.abort.called


@respx.mock
def test_upload_file_finalize_rejection_aborts() -> None:
    server = _UploadServer(respx.mock)
    server.finalize.mock(return_value=httpx.Response(409, json={"detail": "checksum mismatch"}))

    with _make_client() as client, pytest.raises(ConflictError):
        client.uploads.upload_file(b"abcdef", profile=IMAGE_PROFILE, mime_type="image/png")

    # Session confirmed not finalized → aborted.
    assert server.get.called
    assert server.abort.called


@respx.mock
def test_upload_file_finalize_timeout_after_commit_does_not_abort() -> None:
    server = _UploadServer(respx.mock)
    server.finalize.mock(side_effect=httpx.ReadTimeout("timed out"))
    server.get.mock(
        return_value=httpx.Response(200, json=_session_response(server.session_id, state="finalized"))
    )

    with _make_client() as client, pytest.raises(httpx.ReadTimeout):
        client.uploads.upload_file(b"abcdef", profile=IMAGE_PROFILE, mime_type="image/png")

    # Aborting a finalized session would delete its blob.
    assert server.get.called
    assert not server.abort.called


@respx.mock
def test_upload_file_finalize_failure_with_unknown_state_does_not_abort() -> None:
    server = _UploadServer(respx.mock)
    server.finalize.mock(side_effect=httpx.ReadTimeout("timed out"))
    server.get.mock(side_effect=httpx.ConnectError("down"))

    with _make_client() as client, pytest.raises(httpx.ReadTimeout):
        client.uploads.upload_file(b"abcdef", profile=IMAGE_PROFILE, mime_type="image/png")

    assert not server.abort.called


# ── MIME type normalization ───────────────────────────────────────────


@respx.mock
def test_create_session_sends_the_normalized_mime_type() -> None:
    server = _UploadServer(respx.mock)

    with _make_client() as client:
        client.uploads.create_session(mime_type=" Image/PNG; charset=binary", profile=IMAGE_PROFILE,
                                      expected_size_bytes=10)

    assert server.create_body["mime_type"] == "image/png"


@respx.mock
def test_create_session_without_profile_sends_the_normalized_mime_type() -> None:
    server = _UploadServer(respx.mock)

    with _make_client() as client:
        client.uploads.create_session(mime_type="Video/MP4", kind="video")

    assert server.create_body["mime_type"] == "video/mp4"


@respx.mock
@pytest.mark.parametrize("validate", [True, False])
def test_create_session_rejects_malformed_mime_type_before_any_request(validate) -> None:
    server = _UploadServer(respx.mock)

    with _make_client() as client, pytest.raises(UploadRejected) as excinfo:
        client.uploads.create_session(mime_type="png", profile=IMAGE_PROFILE,
                                      expected_size_bytes=10, validate=validate)

    assert excinfo.value.reason == UploadRejected.MIME_TYPE
    assert not server.create.called


@respx.mock
def test_create_session_without_profile_rejects_malformed_mime_type() -> None:
    server = _UploadServer(respx.mock)

    with _make_client() as client, pytest.raises(UploadRejected):
        client.uploads.create_session(mime_type="", kind="other")

    assert not server.create.called


@respx.mock
def test_upload_file_sends_the_normalized_mime_type() -> None:
    server = _UploadServer(respx.mock)

    with _make_client() as client:
        client.uploads.upload_file(b"abcdef", profile=IMAGE_PROFILE, mime_type=" IMAGE/JPEG ")

    assert server.create_body["mime_type"] == "image/jpeg"
