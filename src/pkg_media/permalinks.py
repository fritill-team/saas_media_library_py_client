"""Permalinks: stable public addresses (``{media base}/p/{name}``).

A permalink is a media-library record ``name -> assetId`` for a **public**
asset. The app picks the name (e.g. ``users/42/avatar``), stores the name,
not the URL, and re-points it whenever the asset changes; every page that
embedded the address follows. Service token only. See the server's
docs/api/permalinks.md.
"""

from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

from typing import Any

from pydantic import BaseModel, Field

#: Longest valid name, as on the server.
MAX_PERMALINK_NAME_LENGTH = 255

_SEGMENT = r"[A-Za-z0-9_-][A-Za-z0-9._-]*"
_NAME_RE = re.compile(rf"{_SEGMENT}(?:/{_SEGMENT})*", re.ASCII)


class Permalink(BaseModel):
    """A permalink as the server returns it.

    ``url`` is built by the server from its media host on every response;
    store ``name``, not ``url``.
    """

    name: str
    asset_id: UUID = Field(alias="assetId")
    url: str
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")

    model_config = {"populate_by_name": True}


def validate_permalink_name(name: str) -> str:
    """``name`` unchanged, or ``ValueError`` when the server would refuse it.

    One or more segments joined by ``/``; each of letters, digits, ``.``,
    ``_`` and ``-``, not starting with ``.``; at most 255 characters. A valid
    name needs no URL escaping.
    """
    if (
        not isinstance(name, str)
        or not 0 < len(name) <= MAX_PERMALINK_NAME_LENGTH
        or _NAME_RE.fullmatch(name) is None
    ):
        raise ValueError(
            f"invalid permalink name {name!r}: 1-255 characters, segments of letters, "
            "digits, '.', '_' or '-' (not starting with '.'), joined by '/'"
        )
    return name


def permalink_url(media_base_url: str, name: str) -> str:
    """The public address ``{media_base_url}/p/{name}``.

    ``media_base_url`` is the media host that serves files (the server's
    ``VOLUME_BASE_URL``), not the API base URL. Build the URL from the stored
    name when rendering, so a host change needs no data change.
    """
    return f"{media_base_url.rstrip('/')}/p/{validate_permalink_name(name)}"


class PermalinksAPI:
    """HTTP ``/manage/permalinks/{name}``, gRPC ``fritill.media.v1.Permalinks``.
    Needs a service caller.

    Names are validated before any request (``ValueError``), so a name such
    as ``../x`` never reaches another endpoint.
    """

    BASE = "/v1/manage/permalinks"

    def __init__(self, transport: Any):
        self._transport = transport

    async def set(self, name: str, asset_id: UUID | str) -> Permalink:
        """Create ``name`` or re-point it at ``asset_id``.

        Re-pointing purges the old edge copy; the same asset again changes
        nothing. A target that is public but not ready yet is accepted
        (``/p/{name}`` answers 404 until it is).

        Raises:
            ValueError: invalid name (checked before the request).
            NotFoundError: the asset is missing or deleted (404).
            ValidationError: the asset is not public (422).
            AuthenticationError: not a service token (401/403).
        """
        return await self._transport.set_permalink(validate_permalink_name(name), str(asset_id))

    async def get(self, name: str) -> Permalink:
        """The permalink ``name``; ``NotFoundError`` when there is none."""
        return await self._transport.get_permalink(validate_permalink_name(name))

    async def delete(self, name: str) -> None:
        """Remove ``name`` (its edge copy is purged); ``NotFoundError`` when unknown."""
        await self._transport.delete_permalink(validate_permalink_name(name))
