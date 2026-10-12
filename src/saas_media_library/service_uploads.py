"""Browser uploads started by the app's backend.

The flow, for each of the app's upload slots (a course cover, an avatar…):

1. The browser asks the app to start an upload (the app's own rule decides).
   The app creates the upload session with its **service token** and the
   slot's ``UploadProfile``, records who started it (``UploadRecord`` in an
   ``UploadSessionStore``), and returns an ``upload`` grant limited to that
   one session. ``integrations.fastapi.create_upload_router`` does this.
2. The browser uploads the parts and finalizes directly with the media
   library, refreshing the grant through the app when it gets close to
   expiring.
3. The browser sends ``{sessionId, assetId}`` back to the app, which calls
   ``claim_upload`` before it stores the asset id.

The browser never chooses kind, visibility or processing (the app created
the session) and cannot attach someone else's upload (``claim_upload``
checks the record's owner and attaches a session once).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Protocol, Union
from uuid import UUID

from saas_media_library.exceptions import UploadRejected
from saas_media_library.models import Asset
from saas_media_library.profiles import UploadProfile


@dataclass(frozen=True)
class UploadRecord:
    """Who started an upload session, for what.

    ``owner`` is the app's user reference; ``profile`` the upload profile's
    name; ``context`` what the start request asked for and the app's rule
    approved (e.g. ``{"courseId": "…"}``).
    """

    session_id: UUID
    owner: str
    profile: str
    context: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    attached_asset_id: Optional[UUID] = None


class UploadSessionStore(Protocol):
    """Where the app keeps ``UploadRecord``s (its database, Redis…).

    ``mark_attached`` must be atomic: set ``attached_asset_id`` only if it is
    not set yet, and return whether it did (e.g. ``UPDATE … SET
    attached_asset_id = :asset WHERE session_id = :id AND attached_asset_id
    IS NULL``, in the same transaction as the app's own write).
    """

    def save(self, record: UploadRecord) -> None: ...

    def get(self, session_id: UUID) -> Optional[UploadRecord]: ...

    def mark_attached(self, session_id: UUID, asset_id: UUID) -> bool: ...


class InMemoryUploadStore:
    """An ``UploadSessionStore`` in this process's memory.

    For tests and single-process development only: records are lost on
    restart and not shared between workers or replicas.
    """

    def __init__(self) -> None:
        self._records: Dict[UUID, UploadRecord] = {}
        self._lock = threading.Lock()

    def save(self, record: UploadRecord) -> None:
        with self._lock:
            self._records[record.session_id] = record

    def get(self, session_id: UUID) -> Optional[UploadRecord]:
        with self._lock:
            return self._records.get(session_id)

    def mark_attached(self, session_id: UUID, asset_id: UUID) -> bool:
        with self._lock:
            record = self._records.get(session_id)
            if record is None or record.attached_asset_id is not None:
                return False
            self._records[session_id] = replace(record, attached_asset_id=asset_id)
            return True


@dataclass(frozen=True)
class ClaimedUpload:
    """What ``claim_upload`` returns: the record (with the start request's
    ``context``) and the verified asset."""

    record: UploadRecord
    asset: Asset


def claim_upload(
    client: Any,
    store: UploadSessionStore,
    session_id: Union[UUID, str],
    asset_id: Union[UUID, str],
    *,
    owner: str,
    profile: UploadProfile,
) -> ClaimedUpload:
    """Verify and attach a browser upload, once.

    Checks that ``store`` has a record of ``session_id`` started by
    ``owner`` with ``profile`` (by name) and not attached yet, then
    ``client.uploads.verify_upload`` (finalized, the asset's blob is the
    session's, visibility and profile checks), then marks the record
    attached. ``client`` is a ``MediaLibraryClient`` with the service token.

    Raises:
        UploadRejected: ``reason`` is ``UNKNOWN_SESSION`` (no record, or
            another owner's), ``PROFILE_MISMATCH``, ``ALREADY_ATTACHED``,
            or one from ``verify_upload``.
        NotFoundError: the media library has no such session or asset.
    """
    session_id = UUID(str(session_id))
    asset_id = UUID(str(asset_id))
    record = store.get(session_id)
    if record is None or record.owner != owner:
        raise UploadRejected(
            f"Unknown upload session {session_id}",
            reason=UploadRejected.UNKNOWN_SESSION,
        )
    if record.profile != profile.name:
        raise UploadRejected(
            f"Upload session {session_id} was started for upload profile "
            f"{record.profile!r}, not {profile.name!r}",
            reason=UploadRejected.PROFILE_MISMATCH,
        )
    if record.attached_asset_id is not None:
        raise UploadRejected(
            f"Upload session {session_id} is already attached",
            reason=UploadRejected.ALREADY_ATTACHED,
        )
    asset = client.uploads.verify_upload(session_id, asset_id, profile=profile)
    if not store.mark_attached(session_id, asset_id):
        raise UploadRejected(
            f"Upload session {session_id} is already attached",
            reason=UploadRejected.ALREADY_ATTACHED,
        )
    return ClaimedUpload(record=replace(record, attached_asset_id=asset_id), asset=asset)
