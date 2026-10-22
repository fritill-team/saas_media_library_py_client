"""Media embedded in a service's own responses (``EmbeddedMedia``).

A service that returns records with media ids (``coverId``…) adds the media
next to them (``cover: {url, …}``), so the browser renders it without asking
anyone else. The service decides access with its own rule; ``MediaEmbedder``
runs that rule through ``AccessGate`` and turns the media library's delivery
results into one small shape, the same in every service. This model (and its
GraphQL type in ``pkg_media.integrations.strawberry``) is the only definition:
frontends take the type from each service's GraphQL schema by codegen.

Tokened links (gate ``/o/…?token``, stream tokens) are bearer secrets until
they expire: keep them out of logs and of any cache shared between users.
``EmbedResult.tokened`` says whether a response carries one, so the service
can send ``Cache-Control: private``. PUBLIC assets get tokenless links that
never expire, which edge caches may keep.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Literal, Optional, Tuple, Union
from uuid import UUID

from pydantic import BaseModel, Field

from pkg_media.cache import AssetUrlCache, _as_uuid
from pkg_media.delivery import _THUMBNAIL_RENDITION_TYPE
from pkg_media.gate import AccessGate, CanAccess
from pkg_media.models import (
    AssetKind,
    AssetStatus,
    DeliveryAsset,
    DeliveryBatchResolveResult,
    DeliveryRendition,
    ResolveStatus,
)

EmbeddedKind = Literal["image", "video", "audio", "document", "other"]

_KINDS = {
    AssetKind.IMAGE: "image",
    AssetKind.VIDEO: "video",
    AssetKind.AUDIO: "audio",
    AssetKind.DOCUMENT: "document",
}


class EmbeddedMediaStreams(BaseModel):
    """HLS/DASH entrypoints of a video or audio asset (stream token links)."""

    hls: Optional[str] = None
    dash: Optional[str] = None


class EmbeddedMediaSource(BaseModel):
    """One size of an image, for ``srcset`` (``url {width}w``)."""

    url: str
    width: int
    height: Optional[int] = None


class EmbeddedMedia(BaseModel):
    """One asset as a service embeds it in its response.

    - ``url``: the asset's file; None when the user may not open it, the
      asset does not exist, or it is not ``ready`` yet.
    - ``poster``: the image to show for the asset (a video's still, an
      image's thumbnail rendition); also set for a denied user, as a locked
      teaser.
    - ``streams``: HLS/DASH links of a video or audio, when it has a stream.
    - ``sources``: the sizes of ``poster`` (an image's thumbnails, a video's
      stills), smallest first, for ``srcset``; also set for a denied user.
    - ``width`` / ``height``: the largest size media has for the asset (its
      aspect ratio, to reserve space); None when unknown.
    - ``alt``: never set by media; the service fills it from its own data.
    - ``expires_in``: seconds until the first of the carried links expires;
      None when none of them expires (tokenless links).
    - ``public``: the item carries links and all of them are tokenless, so it
      is safe in shared caches. Read this, not the asset's visibility.

    Every link carried has a known expiry: a link whose expiry media did not
    report is left out.
    """

    asset_id: UUID = Field(alias="assetId")
    kind: EmbeddedKind = "other"
    url: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    alt: Optional[str] = None
    poster: Optional[str] = None
    streams: Optional[EmbeddedMediaStreams] = None
    sources: List[EmbeddedMediaSource] = Field(default_factory=list)
    expires_in: Optional[int] = Field(None, alias="expiresIn")
    public: bool = False

    model_config = {"populate_by_name": True}

    @property
    def tokened(self) -> bool:
        """Whether a carried link holds a token (and so expires)."""
        return self.expires_in is not None

    def to_json(self) -> Dict[str, Any]:
        """The camelCase JSON a service puts in its response."""
        return self.model_dump(mode="json", by_alias=True)

    @classmethod
    def from_result(
        cls,
        result: DeliveryBatchResolveResult,
        *,
        service_result: Optional[DeliveryBatchResolveResult] = None,
    ) -> "EmbeddedMedia":
        """Map one delivery result (``AccessGate`` or ``client.delivery``).

        An ``ok`` result with its asset gives every link. Any other result
        (``forbidden``, ``not_found``, ``error``) gives at most its
        ``thumbnail_url`` as ``poster``, and only when its expiry is known
        from ``service_result`` (the media library's result for the service,
        whose asset lists the thumbnail's expiry); it also gives the kind.
        """
        if result.status == ResolveStatus.OK and result.asset is not None:
            return _from_asset(result.asset_id, result.asset)

        known = service_result.asset if service_result is not None else None
        links: List[Tuple[str, Optional[int]]] = []
        thumbnail = None
        if result.thumbnail_url and known is not None:
            found, expires_in = _expiry_of(known, result.thumbnail_url)
            if found:
                thumbnail = result.thumbnail_url
                links.append((thumbnail, expires_in))
        # The teaser's sizes: the same still, from the service's own view.
        kind = _kind(known) if known is not None else "other"
        sources = (
            _poster_sources(_thumbnail_rendition(known), links, _still_shape(known, kind))
            if thumbnail else []
        )
        return cls(
            asset_id=result.asset_id,
            kind=kind,
            poster=thumbnail,
            sources=sources,
            **_expiry_fields(links),
        )


@dataclass(frozen=True)
class EmbedResult(Mapping):
    """``{asset_id: EmbeddedMedia}`` for one response, plus ``tokened``.

    Indexing takes a ``UUID`` or its string. Every requested id is present
    (denied and missing ones without ``url``).
    """

    media: Dict[UUID, EmbeddedMedia]

    def __getitem__(self, asset_id: Union[UUID, str]) -> EmbeddedMedia:
        return self.media[_as_uuid(asset_id)]

    def __iter__(self) -> Iterator[UUID]:
        return iter(self.media)

    def __len__(self) -> int:
        return len(self.media)

    def __contains__(self, asset_id: object) -> bool:
        try:
            return _as_uuid(asset_id) in self.media  # type: ignore[arg-type]
        except (TypeError, ValueError, AttributeError):
            return False

    def get(self, asset_id: Optional[Union[UUID, str]], default: Any = None) -> Any:  # type: ignore[override]
        """The item for ``asset_id``; ``default`` for None or an id not requested."""
        if asset_id is None or asset_id not in self:
            return default
        return self[asset_id]

    @property
    def tokened(self) -> bool:
        """Whether any link is tokened: send ``Cache-Control: private``."""
        return any(m.tokened for m in self.media.values())

    @property
    def cache_control(self) -> Optional[str]:
        """``"private"`` when a link is tokened, else None (the service's own choice)."""
        return "private" if self.tokened else None


class MediaEmbedder:
    """Ids in, ``EmbeddedMedia`` out, after the service's own access check.

    Build one per process (it holds the link cache) and call ``embed`` (sync)
    or ``aembed`` (async) while building a response::

        embedder = MediaEmbedder(media_client, can_access)

        def list_articles(user):
            articles = repo.list()
            embeds = embedder.embed(user, [a.cover_id for a in articles])
            if embeds.tokened:
                response.headers["Cache-Control"] = "private"
            return [{**a.to_json(), "cover": _json(embeds.get(a.cover_id))} for a in articles]

        def _json(media):
            return media.to_json() if media is not None else None

    ``client`` is a sync ``MediaClient`` calling as the service over gRPC
    (delivery is gRPC only); ``can_access(user, ids)`` is the service's rule, as for
    ``AccessGate``. ``cache``: by default the embedder keeps its own
    ``AssetUrlCache``; pass one to share it, or ``False`` for none.
    ``thumbnails=False`` does not resolve denied ids (no teaser).
    Batching (100 ids per call) is done by ``client.delivery.resolve``.
    """

    def __init__(
        self,
        client: Any,
        can_access: CanAccess,
        *,
        cache: Union[AssetUrlCache, None, Literal[False]] = None,
        thumbnails: bool = True,
    ):
        if cache is None:
            cache = AssetUrlCache()
        self.cache: Optional[AssetUrlCache] = None if cache is False else cache
        self.gate = AccessGate(
            client,
            can_access,
            cache=self.cache,
            hide_missing=True,
            resolve_denied=thumbnails,
        )

    def embed(
        self,
        user: Any,
        asset_ids: Iterable[Optional[Union[UUID, str]]],
    ) -> EmbedResult:
        """One ``EmbeddedMedia`` per distinct id; None ids are skipped.

        No id (or only None) calls neither ``can_access`` nor the media
        library. Errors are ``AccessGate.resolve``'s.
        """
        ids = list(dict.fromkeys(_as_uuid(a) for a in asset_ids if a is not None))
        results, service_results = self.gate._resolve_detailed(user, ids)
        return EmbedResult({
            r.asset_id: EmbeddedMedia.from_result(r, service_result=service_results.get(r.asset_id))
            for r in results
        })

    async def aembed(
        self,
        user: Any,
        asset_ids: Iterable[Optional[Union[UUID, str]]],
    ) -> EmbedResult:
        """``embed`` from async code (runs it in a worker thread)."""
        return await asyncio.to_thread(self.embed, user, list(asset_ids))


# ── Mapping ────────────────────────────────────────────────────────────


def _kind(asset: DeliveryAsset) -> EmbeddedKind:
    return _KINDS.get(asset.kind, "other")  # type: ignore[return-value]


def _from_asset(asset_id: UUID, asset: DeliveryAsset) -> EmbeddedMedia:
    kind = _kind(asset)
    links: List[Tuple[str, Optional[int]]] = []

    url = asset.download_url if asset.status == AssetStatus.READY else None
    if url:
        links.append((url, asset.download_url_expires_in))

    thumb = _thumbnail_rendition(asset)
    thumbnail = thumb.preview_url if thumb is not None else None
    if thumbnail:
        links.append((thumbnail, thumb.preview_url_expires_in))

    streams = None
    if asset.hls_url or asset.dash_url:
        streams = EmbeddedMediaStreams(hls=asset.hls_url, dash=asset.dash_url)
        links.extend((u, asset.stream_url_expires_in) for u in (asset.hls_url, asset.dash_url) if u)

    sources = _poster_sources(thumb, links, _still_shape(asset, kind)) if kind in ("image", "video") else []

    width, height = _dimensions(asset, kind)
    return EmbeddedMedia(
        asset_id=asset_id,
        kind=kind,
        url=url,
        width=width,
        height=height,
        poster=thumbnail,
        streams=streams,
        sources=sources,
        **_expiry_fields(links),
    )


# How far a still's width/height ratio may stray from the video's and still count.
_SHAPE_TOLERANCE = 0.02


def _still_shape(asset: DeliveryAsset, kind: str) -> Optional[float]:
    """The ratio a video's stills must have to be listed; None for an image.

    Media scales each still to a fixed size, so the 4:3 ones of a 16:9 video
    are squashed. Only the stills with the video's own shape are listed; a
    video of unknown size lists none (``poster`` still carries its still).
    """
    if kind != "video":
        return None
    width, height = _dimensions(asset, kind)
    return width / height if width and height else 0.0


def _poster_sources(
    thumb: Optional[DeliveryRendition],
    links: List[Tuple[str, Optional[int]]],
    shape: Optional[float] = None,
) -> List[EmbeddedMediaSource]:
    """The thumbnail rendition's sizes, smallest first, one per width (the
    first listed wins), limited to ``shape`` when given (see ``_still_shape``);
    each link joins ``links`` for the expiry."""
    if thumb is None:
        return []
    by_width: Dict[int, EmbeddedMediaSource] = {}
    for v in thumb.variants:
        if shape is not None and not (
            shape and v.width and v.height and abs(v.width / v.height / shape - 1) <= _SHAPE_TOLERANCE
        ):
            continue
        if v.preview_url and v.width and v.width not in by_width:
            by_width[v.width] = EmbeddedMediaSource(url=v.preview_url, width=v.width, height=v.height)
            links.append((v.preview_url, v.preview_url_expires_in))
    return [by_width[w] for w in sorted(by_width)]


def _thumbnail_rendition(asset: Optional[DeliveryAsset]) -> Optional[DeliveryRendition]:
    if asset is None:
        return None
    for rendition in asset.renditions:
        if rendition.rendition_type == _THUMBNAIL_RENDITION_TYPE and rendition.preview_url:
            return rendition
    return None


def _dimensions(asset: DeliveryAsset, kind: str) -> Tuple[Optional[int], Optional[int]]:
    """The largest variant with both sizes, outside thumbnails (a video's
    thumbnails have fixed sizes); an image's thumbnails keep its ratio."""
    sized = [
        v for r in asset.renditions if r.rendition_type != _THUMBNAIL_RENDITION_TYPE
        for v in r.variants if v.width and v.height
    ]
    if not sized and kind == "image":
        sized = [
            v for r in asset.renditions if r.rendition_type == _THUMBNAIL_RENDITION_TYPE
            for v in r.variants if v.width and v.height
        ]
    if not sized:
        return None, None
    best = max(sized, key=lambda v: v.width)
    return best.width, best.height


def _expiry_of(asset: DeliveryAsset, url: str) -> Tuple[bool, Optional[int]]:
    """``(found, expires_in)`` of ``url`` among the asset's links."""
    if url == asset.download_url:
        return True, asset.download_url_expires_in
    if url in (asset.hls_url, asset.dash_url):
        return True, asset.stream_url_expires_in
    for rendition in asset.renditions:
        if url == rendition.preview_url:
            return True, rendition.preview_url_expires_in
        for v in rendition.variants:
            if url == v.preview_url:
                return True, v.preview_url_expires_in
    return False, None


def _expiry_fields(links: List[Tuple[str, Optional[int]]]) -> Dict[str, Any]:
    """``expires_in`` (the smallest positive expiry; 0 or None: none) and ``public``."""
    expiries = [e for _, e in links if e is not None and e > 0]
    expires_in = min(expiries) if expiries else None
    return {"expires_in": expires_in, "public": bool(links) and expires_in is None}
