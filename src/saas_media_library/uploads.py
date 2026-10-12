from __future__ import annotations

import contextlib
import hashlib
import io
import logging
import mimetypes
import os
import stat
import tempfile
from typing import Any, BinaryIO, Dict, Iterator, Mapping, Optional, Tuple, Union
from uuid import UUID

import httpx

from saas_media_library._http import raise_for_status
from saas_media_library.exceptions import MediaLibraryError
from saas_media_library.models import (
    AssetKind,
    ProcessingConfig,
    UploadAbortResult,
    UploadFinalizeResult,
    UploadPartResult,
    UploadSession,
    Visibility,
)
from saas_media_library.profiles import (
    UploadProfile,
    _file_mime_type,
    processing_config_payload,
)

logger = logging.getLogger(__name__)

UploadSource = Union[str, os.PathLike, bytes, bytearray, memoryview, BinaryIO]

DEFAULT_MIME_TYPE = "application/octet-stream"

# Non-seekable streams are copied to a temp file first (the size must be known
# before the session is created); up to this many bytes stay in memory.
_SPOOL_MAX_BYTES = 8 * 1024 * 1024


class UploadsAPI:
    """Chunked uploads without a resource policy.

    The app's ``UploadProfile`` supplies the kind, visibility and processing
    settings, and checks the file before anything is sent. The server accepts
    policy-less upload sessions (every call: create, get, parts, finalize,
    abort) only from a trusted caller: the tenant's service token, or a user
    of a ``token_exchange`` tenant. Others get 403 (``AuthenticationError``)
    unless the server sets ``POLICYLESS_UPLOADS_ALLOW_UNVERIFIED_USERS``.

    ``upload_file`` runs the whole flow; the lower-level methods map 1:1 to
    the ``/v1/manage/uploads/sessions`` endpoints.
    """

    BASE = "/v1/manage/uploads/sessions"

    def __init__(self, http: httpx.Client):
        self._http = http

    # ── Session endpoints ──────────────────────────────────────────────

    def create_session(
        self,
        *,
        mime_type: str,
        profile: Optional[UploadProfile] = None,
        kind: Optional[AssetKind | str] = None,
        visibility: Optional[Visibility | str] = None,
        processing_config: Optional[ProcessingConfig | Mapping[str, Any]] = None,
        original_filename: Optional[str] = None,
        expected_size_bytes: Optional[int] = None,
        expected_sha256_hex: Optional[str] = None,
        collection_name: Optional[str] = None,
        validate: bool = True,
    ) -> UploadSession:
        """Create an upload session.

        ``profile`` supplies ``kind``, ``visibility`` and ``processing_config``;
        explicit arguments override it. With a profile and ``validate=True``,
        ``profile.validate_file(mime_type, expected_size_bytes)`` runs first.
        ``resource_type`` is never sent, so no resource policy is involved.
        ``mime_type`` is sent normalized (``"Image/PNG; q=1"`` as
        ``"image/png"``), with or without a profile.

        Raises:
            TypeError: if neither ``kind`` nor ``profile`` is given.
            UploadRejected: if the profile rejects the file, or ``mime_type``
                is not ``type/subtype``.
        """
        fields: Dict[str, Any] = profile.to_session_fields() if profile is not None else {}
        if kind is not None:
            fields["kind"] = str(kind)
        if "kind" not in fields:
            raise TypeError("create_session() requires kind, either directly or via profile.")
        if visibility is not None:
            fields["visibility"] = str(visibility)
        if processing_config is not None:
            fields["processingConfig"] = processing_config_payload(processing_config)

        if profile is not None and validate:
            profile.validate_file(mime_type, expected_size_bytes)

        # Only processingConfig has a camelCase alias on the server; the other
        # request fields are snake_case.
        payload: Dict[str, Any] = {"mime_type": _file_mime_type(mime_type), **fields}
        if original_filename is not None:
            payload["original_filename"] = original_filename
        if expected_size_bytes is not None:
            payload["expected_size_bytes"] = expected_size_bytes
        if expected_sha256_hex is not None:
            payload["expected_sha256_hex"] = expected_sha256_hex
        if collection_name is not None:
            payload["collection_name"] = collection_name

        resp = self._http.post(self.BASE, json=payload)
        raise_for_status(resp)
        return UploadSession.model_validate(resp.json())

    def get_session(self, session_id: UUID | str) -> UploadSession:
        """Get an upload session's state and progress."""
        resp = self._http.get(f"{self.BASE}/{session_id}")
        raise_for_status(resp)
        return UploadSession.model_validate(resp.json())

    def upload_part(
        self,
        session_id: UUID | str,
        part_number: int,
        data: bytes,
        *,
        sha256_hex: Optional[str] = None,
        offset: Optional[int] = None,
        total_size_bytes: Optional[int] = None,
    ) -> UploadPartResult:
        """Upload one part (1-based ``part_number``).

        The server requires a part checksum: ``sha256_hex`` is computed from
        ``data`` when not given. Pass ``offset`` (and ``total_size_bytes``) to
        also send a ``Content-Range`` header. Re-sending a part with the same
        checksum is idempotent.
        """
        body = bytes(data)
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(body)),
            "X-Part-SHA256": sha256_hex or hashlib.sha256(body).hexdigest(),
        }
        if offset is not None and body:
            total = str(total_size_bytes) if total_size_bytes is not None else "*"
            headers["Content-Range"] = f"bytes {offset}-{offset + len(body) - 1}/{total}"

        resp = self._http.put(
            f"{self.BASE}/{session_id}/parts/{part_number}",
            content=body,
            headers=headers,
        )
        raise_for_status(resp)
        return UploadPartResult.model_validate(resp.json())

    def finalize(
        self,
        session_id: UUID | str,
        *,
        expected_sha256_hex: Optional[str] = None,
    ) -> UploadFinalizeResult:
        """Assemble the uploaded parts into an asset.

        With ``expected_sha256_hex`` the server verifies the whole file and
        answers 409 (``ConflictError``) on a mismatch.
        """
        params = {"expected_sha256_hex": expected_sha256_hex} if expected_sha256_hex else {}
        resp = self._http.post(f"{self.BASE}/{session_id}/finalize", params=params)
        raise_for_status(resp)
        return UploadFinalizeResult.model_validate(resp.json())

    def abort(self, session_id: UUID | str) -> UploadAbortResult:
        """Abort a session and delete its uploaded parts.

        Note: aborting a *finalized* session also deletes its blob.
        """
        resp = self._http.post(f"{self.BASE}/{session_id}/abort")
        raise_for_status(resp)
        return UploadAbortResult.model_validate(resp.json())

    # ── Convenience ────────────────────────────────────────────────────

    def upload_file(
        self,
        source: UploadSource,
        *,
        profile: UploadProfile,
        mime_type: Optional[str] = None,
        filename: Optional[str] = None,
        collection_name: Optional[str] = None,
        part_size_bytes: Optional[int] = None,
        verify_checksum: bool = True,
    ) -> UploadFinalizeResult:
        """Upload a file end to end: validate, create session, parts, finalize.

        ``source`` is a path, bytes, or a binary file object (read from its
        current position). ``filename`` defaults to the path's or file
        object's name; ``mime_type`` is guessed from it, falling back to
        ``application/octet-stream``.

        The file is read once: each part's checksum (always sent, the server
        requires it) and, with ``verify_checksum``, the whole-file SHA-256
        passed to ``finalize`` are computed while uploading. Only
        non-seekable streams, and paths that are not regular files with a
        known size (FIFOs, ``/dev/stdin``, ``/proc`` files), are first copied
        to a temp file to learn their size.

        ``part_size_bytes`` defaults to the size the server returns; a
        custom value must not be smaller than it.

        If anything fails after the session is created, the session is
        aborted (best effort) and the error re-raised. A failed ``finalize``
        is only followed by an abort when the session is confirmed not
        finalized, since aborting a finalized session deletes its blob.

        Raises:
            UploadRejected: if the profile rejects the file (nothing is sent).
        """
        if part_size_bytes is not None and part_size_bytes <= 0:
            raise ValueError("part_size_bytes must be positive")

        with _open_source(source) as (stream, size_bytes, source_name):
            filename = filename or source_name
            if mime_type is None:
                mime_type = _guess_mime_type(filename)
            profile.validate_file(mime_type, size_bytes)

            session = self.create_session(
                mime_type=mime_type,
                profile=profile,
                original_filename=filename,
                expected_size_bytes=size_bytes,
                collection_name=collection_name,
                validate=False,
            )
            finalizing = False
            try:
                part_size = part_size_bytes or session.part_size_bytes
                total_parts = -(-size_bytes // part_size)  # ceil
                if session.parts_expected is not None and total_parts > session.parts_expected:
                    raise ValueError(
                        f"part_size_bytes={part_size} is smaller than the server's part size "
                        f"{session.part_size_bytes}; {total_parts} parts would exceed the "
                        f"{session.parts_expected} the session accepts"
                    )

                file_hash = hashlib.sha256() if verify_checksum else None
                offset = 0
                for part_number in range(1, total_parts + 1):
                    expected = min(part_size, size_bytes - offset)
                    chunk = _read_exact(stream, expected)
                    if len(chunk) != expected:
                        raise MediaLibraryError(
                            f"Source ended after {offset + len(chunk)} of {size_bytes} bytes"
                        )
                    if file_hash is not None:
                        file_hash.update(chunk)
                    self.upload_part(
                        session.id,
                        part_number,
                        chunk,
                        sha256_hex=hashlib.sha256(chunk).hexdigest(),
                        offset=offset,
                        total_size_bytes=size_bytes,
                    )
                    offset += expected

                finalizing = True
                return self.finalize(
                    session.id,
                    expected_sha256_hex=file_hash.hexdigest() if file_hash is not None else None,
                )
            except Exception:
                self._abort_quietly(session.id, check_finalized=finalizing)
                raise

    # ── Private helpers ────────────────────────────────────────────────

    def _abort_quietly(self, session_id: UUID, *, check_finalized: bool) -> None:
        """Best-effort abort after a failed upload; never raises.

        With ``check_finalized`` (the failure came from ``finalize``, e.g. a
        timeout after the server committed), abort only when the session is
        confirmed not finalized. If that cannot be confirmed, leave the
        session for the server to expire.
        """
        try:
            if check_finalized and self.get_session(session_id).state == "finalized":
                return
            self.abort(session_id)
        except Exception as exc:
            logger.warning("Could not abort upload session %s: %s", session_id, exc)


@contextlib.contextmanager
def _open_source(source: UploadSource) -> Iterator[Tuple[BinaryIO, int, Optional[str]]]:
    """Yield ``(stream, size_bytes, filename)`` for an upload source."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        with memoryview(source) as view:
            size_bytes = view.nbytes
        yield io.BytesIO(source), size_bytes, None
    elif isinstance(source, (str, os.PathLike)):
        path = os.fspath(source)
        name = os.path.basename(path)
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            # FIFOs, character devices and /proc files report st_size 0 (or a
            # placeholder); only a non-empty regular file's size is trusted.
            if stat.S_ISREG(st.st_mode) and st.st_size > 0:
                yield f, st.st_size, name
            else:
                with _spooled(f) as (spool, size_bytes):
                    yield spool, size_bytes, name
    else:
        name = _stream_name(source)
        seekable = getattr(source, "seekable", None)
        if seekable is not None and seekable():
            start = source.tell()
            end = source.seek(0, io.SEEK_END)
            source.seek(start)
            yield source, end - start, name
        else:
            with _spooled(source) as (spool, size_bytes):
                yield spool, size_bytes, name


@contextlib.contextmanager
def _spooled(source: BinaryIO) -> Iterator[Tuple[BinaryIO, int]]:
    """Copy ``source`` to a temp file (in memory up to ``_SPOOL_MAX_BYTES``)
    and yield it rewound, with its size."""
    with tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX_BYTES) as spool:
        size_bytes = 0
        while chunk := source.read(1024 * 1024):
            spool.write(chunk)
            size_bytes += len(chunk)
        spool.seek(0)
        yield spool, size_bytes


def _stream_name(stream: BinaryIO) -> Optional[str]:
    # Real files carry their path; pseudo names such as "<stdin>" are skipped.
    name = getattr(stream, "name", None)
    if not isinstance(name, (str, os.PathLike)):
        return None
    name = os.path.basename(os.fspath(name))
    if not name or (name.startswith("<") and name.endswith(">")):
        return None
    return name


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    """Read ``size`` bytes, looping over short reads; fewer only at EOF."""
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _guess_mime_type(filename: Optional[str]) -> str:
    if filename:
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            return guessed
    return DEFAULT_MIME_TYPE
