"""FastAPI routes that serve media links through an ``AccessGate``.

Needs the ``fastapi`` extra: ``pip install "saas-media-library[fastapi]"``.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List
from uuid import UUID

try:
    from fastapi import APIRouter, Depends, HTTPException
    from fastapi.responses import JSONResponse
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError(
        "saas_media_library.integrations.fastapi requires FastAPI. "
        'Install it with: pip install "saas-media-library[fastapi]"'
    ) from exc
from pydantic import BaseModel, Field

from saas_media_library.exceptions import MediaLibraryError
from saas_media_library.gate import AccessGate
from saas_media_library.models import ResolveStatus

logger = logging.getLogger(__name__)

# Same limit as the server's POST /delivery/assets/resolve.
MAX_RESOLVE_IDS = 100


class ResolveRequest(BaseModel):
    """Body of ``POST {prefix}/resolve``, as the server's delivery endpoint."""

    asset_ids: List[UUID] = Field(..., alias="assetIds", min_length=1, max_length=MAX_RESOLVE_IDS)

    model_config = {"populate_by_name": True}


def create_media_router(
    gate: AccessGate,
    get_current_user: Callable[..., Any],
    *,
    prefix: str = "/media",
) -> APIRouter:
    """Routes that mirror the media library's delivery API for RESTRICTED assets.

    - ``POST {prefix}/resolve`` with ``{"assetIds": [...]}`` (1 to 100 ids):
      the same body as ``POST /delivery/assets/resolve``.
    - ``GET {prefix}/{asset_id}``: the asset, as ``GET /delivery/assets/{id}``.
      403 with ``{"detail", "thumbnailUrl"}`` when denied, 404 when not found,
      502 when the media library could not produce a link.

    ``get_current_user`` is the app's FastAPI dependency; whatever it returns
    is passed to ``gate``'s ``can_access``. Failures talking to the media
    library answer 502, so an expired service token is not mistaken for the
    user's own 401. Endpoints are sync and run in FastAPI's threadpool.

    Usage::

        app.include_router(create_media_router(gate, get_current_user))
    """
    router = APIRouter(prefix=prefix)

    @router.post("/resolve", response_model=None)
    def resolve_assets(
        payload: ResolveRequest,
        user: Any = Depends(get_current_user),
    ) -> Dict[str, Any]:
        with _media_library_errors():
            results = gate.resolve(user, payload.asset_ids)
        return gate.to_payload(results)

    @router.get("/{asset_id}", response_model=None)
    def get_asset(
        asset_id: UUID,
        user: Any = Depends(get_current_user),
    ) -> Any:
        with _media_library_errors():
            result = gate.resolve_one(user, asset_id)

        if result.status == ResolveStatus.OK and result.asset is not None:
            return result.asset.model_dump(mode="json", by_alias=True)
        if result.status == ResolveStatus.NOT_FOUND:
            raise HTTPException(status_code=404, detail=f"Asset {asset_id} not found")
        if result.status == ResolveStatus.FORBIDDEN:
            status_code, detail = 403, result.error or AccessGate.DENIED_ERROR
        else:
            status_code, detail = 502, result.error or "Could not resolve the asset's link"
        return JSONResponse(
            status_code=status_code,
            content={"detail": detail, "thumbnailUrl": result.thumbnail_url},
        )

    return router


@contextmanager
def _media_library_errors() -> Iterator[None]:
    try:
        yield
    except MediaLibraryError as exc:
        logger.warning("Media library request failed: %s", exc)
        raise HTTPException(status_code=502, detail="Media library request failed") from exc
