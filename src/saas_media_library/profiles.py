"""Upload rules defined in the consuming app's code.

``UploadProfile`` replaces a resource policy (being retired, see the server's
docs/resource-policy/retirement.md). The server no longer checks MIME types,
sizes or permissions for policy-less uploads; the app does, with the same
rules the server's ``ResourcePolicy.validate_file`` / ``check_permission``
applied. One difference: MIME types are normalized (parameters and
whitespace dropped, lowercased) before they are compared and before they are
sent, so the server stores the canonical type its processors match on.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Mapping, Optional, Set, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from saas_media_library.exceptions import UploadRejected
from saas_media_library.models import (
    Asset,
    AssetKind,
    DeliveryAsset,
    ProcessingConfig,
    Visibility,
)

ANY_MIME_TYPE = "*/*"

# RFC 9110 token characters (lowercase), without "*".
_MIME_TOKEN = r"[!#$%&'+\-.^_`|~0-9a-z]+"
_MIME_TYPE_RE = re.compile(rf"{_MIME_TOKEN}/{_MIME_TOKEN}")
# In allowed_mime_types a part may also be "*" ("*/*", or "video/*", which is
# compared literally, as the server's policies did).
_ALLOWED_MIME_TYPE_RE = re.compile(rf"(?:{_MIME_TOKEN}|\*)/(?:{_MIME_TOKEN}|\*)")


def processing_config_payload(
    config: Union[ProcessingConfig, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Serialize a processing config for ``POST /manage/uploads/sessions``.

    Always writes every key (camelCase, as the server's policy
    ``processingConfig``). ``enable_transcription`` that was never set is sent
    as ``False``, the server's policy-less default: the client model defaults
    it to ``True`` only for resource-policy compatibility.
    """
    if not isinstance(config, ProcessingConfig):
        config = ProcessingConfig.model_validate(config)
    return _with_upload_defaults(config).model_dump(by_alias=True)


def _with_upload_defaults(config: ProcessingConfig) -> ProcessingConfig:
    if "enable_transcription" in config.model_fields_set:
        return config
    return config.model_copy(update={"enable_transcription": False})


class UploadProfile(BaseModel):
    """What one kind of upload in the app accepts and how it is processed.

    Define profiles once in code (they are immutable) and pass them to
    ``client.uploads.create_session`` / ``upload_file``::

        COURSE_COVER = UploadProfile(
            name="course-cover",
            kind=AssetKind.IMAGE,
            visibility=Visibility.PUBLIC,
            allowed_mime_types={"image/png", "image/jpeg"},
            max_size_bytes=5_000_000,
            processing_config=ProcessingConfig(
                step_options={"image_derive": {"formats": ["webp"], "sizes": [256, 512]}},
            ),
        )

    ``allowed_mime_types``: None or a set containing ``"*/*"`` accepts any
    type; an empty set accepts none. Every entry must be ``type/subtype``;
    entries and file types are normalized (``" Image/PNG; q=1"`` becomes
    ``"image/png"``) before they are compared. ``"*/*"`` is the only
    wildcard: an entry such as ``"video/*"`` only matches itself.

    ``processing_config``: None lets the server default apply (no step
    options, transcription off). An ``enable_transcription`` left unset is
    likewise normalized to ``False``.

    ``allow_multiple`` is informational (whether a form field may take
    several files); nothing here enforces it.
    """

    kind: AssetKind
    visibility: Visibility = Visibility.PRIVATE
    allowed_mime_types: Optional[Set[str]] = None
    min_size_bytes: Optional[int] = Field(None, ge=0)
    max_size_bytes: Optional[int] = Field(None, ge=0)
    allow_multiple: bool = False
    required_permissions: Set[str] = Field(default_factory=set)
    allow_anonymous: bool = False
    processing_config: Optional[ProcessingConfig] = None
    name: Optional[str] = None

    # Profiles are shared constants: forbid typos (a misspelled
    # ``max_size_bytes`` would silently drop the limit) and mutation.
    model_config = {"frozen": True, "extra": "forbid"}

    @field_validator("allowed_mime_types")
    @classmethod
    def _normalize_mime_types(cls, v: Optional[Set[str]]) -> Optional[Set[str]]:
        if v is None:
            return None
        normalized = set()
        for mime_type in v:
            value = _normalize_mime(mime_type, pattern=_ALLOWED_MIME_TYPE_RE)
            if value is None:
                raise ValueError(f"{mime_type!r} is not a MIME type (type/subtype)")
            normalized.add(value)
        return normalized

    @field_validator("processing_config")
    @classmethod
    def _apply_upload_defaults(cls, v: Optional[ProcessingConfig]) -> Optional[ProcessingConfig]:
        return _with_upload_defaults(v) if v is not None else None

    @model_validator(mode="after")
    def _check_size_bounds(self) -> UploadProfile:
        if (
            self.min_size_bytes is not None
            and self.max_size_bytes is not None
            and self.min_size_bytes > self.max_size_bytes
        ):
            raise ValueError("min_size_bytes cannot exceed max_size_bytes")
        return self

    # ── Checks ─────────────────────────────────────────────────────────

    def validate_file(self, mime_type: str, size_bytes: Optional[int] = None) -> None:
        """Check a file before uploading it.

        Mirrors the server's resource-policy check: ``size_bytes`` is
        required when the profile sets a min or max size. ``mime_type`` must
        be ``type/subtype`` (parameters are ignored), whatever the profile
        allows.

        Raises:
            UploadRejected: with ``reason`` set to the rule that failed.
        """
        if size_bytes is None and (
            self.min_size_bytes is not None or self.max_size_bytes is not None
        ):
            raise UploadRejected(
                f"File size is required: upload profile {self._label} sets size limits",
                reason=UploadRejected.SIZE_REQUIRED,
            )
        self._check_mime_type(mime_type)
        if size_bytes is not None:
            self._check_size(size_bytes)

    def check_permissions(
        self,
        permissions: Optional[Iterable[str]],
        *,
        authenticated: bool = True,
    ) -> bool:
        """Whether a user with ``permissions`` may upload with this profile.

        Same order as the server's ``ResourcePolicy.check_permission``:

        1. ``allow_anonymous`` → allowed.
        2. No ``required_permissions`` → allowed, even when unauthenticated.
        3. Unauthenticated → denied.
        4. Allowed only if the user has every required permission.
        """
        if self.allow_anonymous:
            return True
        if not self.required_permissions:
            return True
        if not authenticated:
            return False
        if isinstance(permissions, str):
            permissions = {permissions}
        return self.required_permissions.issubset(set(permissions or ()))

    def verify_asset(self, asset: Union[Asset, DeliveryAsset]) -> None:
        """Check an asset a browser uploaded directly against this profile.

        Checks kind and MIME type, and size bounds when the asset carries a
        size (``Asset`` does, ``DeliveryAsset`` does not).

        Raises:
            UploadRejected: with ``reason`` set to the rule that failed.
        """
        if str(asset.kind) != str(self.kind):
            raise UploadRejected(
                f"Asset kind {asset.kind} does not match upload profile "
                f"{self._label} kind {self.kind}",
                reason=UploadRejected.KIND,
            )
        self._check_mime_type(asset.mime_type)
        size_bytes = getattr(asset, "size_bytes", None)
        if size_bytes is not None:
            self._check_size(size_bytes)

    # ── Serialization ──────────────────────────────────────────────────

    def to_session_fields(self) -> Dict[str, Any]:
        """JSON fields this profile contributes to ``POST /manage/uploads/sessions``.

        ``processingConfig`` is omitted when ``processing_config`` is None so
        the server default applies.
        """
        fields: Dict[str, Any] = {
            "kind": str(self.kind),
            "visibility": str(self.visibility),
        }
        if self.processing_config is not None:
            fields["processingConfig"] = processing_config_payload(self.processing_config)
        return fields

    # ── Private helpers ────────────────────────────────────────────────

    @property
    def _label(self) -> str:
        return repr(self.name) if self.name else f"for kind {self.kind}"

    def _check_mime_type(self, mime_type: str) -> None:
        normalized = _file_mime_type(mime_type)
        allowed = self.allowed_mime_types
        if allowed is None or ANY_MIME_TYPE in allowed:
            return
        if normalized not in allowed:
            raise UploadRejected(
                f"MIME type {mime_type} is not allowed by upload profile {self._label}",
                reason=UploadRejected.MIME_TYPE,
            )

    def _check_size(self, size_bytes: int) -> None:
        if self.max_size_bytes is not None and size_bytes > self.max_size_bytes:
            raise UploadRejected(
                f"File size {size_bytes} bytes exceeds maximum {self.max_size_bytes} bytes",
                reason=UploadRejected.TOO_LARGE,
            )
        if self.min_size_bytes is not None and size_bytes < self.min_size_bytes:
            raise UploadRejected(
                f"File size {size_bytes} bytes below minimum {self.min_size_bytes} bytes",
                reason=UploadRejected.TOO_SMALL,
            )


def _file_mime_type(mime_type: str) -> str:
    """A file's MIME type as compared and sent: ``type/subtype``, lowercased,
    without parameters or surrounding whitespace.

    Raises:
        UploadRejected: (``reason="mime_type"``) if it is not ``type/subtype``.
    """
    normalized = _normalize_mime(mime_type, pattern=_MIME_TYPE_RE)
    if normalized is None:
        raise UploadRejected(
            f"MIME type {mime_type!r} is not a valid type/subtype",
            reason=UploadRejected.MIME_TYPE,
        )
    return normalized


def _normalize_mime(mime_type: Any, *, pattern: re.Pattern) -> Optional[str]:
    if not isinstance(mime_type, str):
        return None
    value = mime_type.split(";", 1)[0].strip().lower()
    return value if pattern.fullmatch(value) else None
