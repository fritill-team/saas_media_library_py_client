"""Strawberry GraphQL support for ``EmbeddedMedia``: the GraphQL type and a
per-request dataloader, so a service adds a ``cover`` field in a few lines.

Needs the ``strawberry`` extra: ``pip install "pkg-media[strawberry]"``.

Usage::

    from pkg_media import MediaEmbedder
    from pkg_media.integrations.strawberry import EmbeddedMediaType, StrawberryMedia

    media = StrawberryMedia(
        MediaEmbedder(media_client, can_access),
        get_user=lambda info: info.context["auth_context"],
    )

    @strawberry.type
    class Article:
        cover_id: strawberry.Private[Optional[UUID]]

        @strawberry.field
        async def cover(self, info: strawberry.Info) -> Optional[EmbeddedMediaType]:
            return await media.load(info, self.cover_id)

Every ``cover`` of one request is resolved in one ``can_access`` call and one
media library call (by the embedder, through its cache). ``media.tokened(info)``
then says whether the response carries a tokened link (``Cache-Control:
private``).
"""

from typing import Any, Callable, List, Optional, Union
from uuid import UUID

try:
    import strawberry
    from strawberry.dataloader import DataLoader
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError(
        "pkg_media.integrations.strawberry requires Strawberry. "
        'Install it with: pip install "pkg-media[strawberry]"'
    ) from exc

from pkg_media.cache import _as_uuid
from pkg_media.embed import EmbeddedMedia, MediaEmbedder


@strawberry.type(name="EmbeddedMediaStreams")
class EmbeddedMediaStreamsType:
    hls: Optional[str] = None
    dash: Optional[str] = None


@strawberry.type(name="EmbeddedMediaSource")
class EmbeddedMediaSourceType:
    url: str
    width: int
    height: Optional[int] = None


@strawberry.type(name="EmbeddedMedia", description="An asset as embedded in a response (pkg_media EmbeddedMedia).")
class EmbeddedMediaType:
    asset_id: UUID
    kind: str
    url: Optional[str]
    thumbnail_url: Optional[str]
    width: Optional[int]
    height: Optional[int]
    alt: Optional[str]
    poster: Optional[str]
    streams: Optional[EmbeddedMediaStreamsType]
    sources: List[EmbeddedMediaSourceType]
    expires_in: Optional[int]
    public: bool

    @classmethod
    def from_model(cls, media: EmbeddedMedia) -> "EmbeddedMediaType":
        streams = media.streams
        return cls(
            asset_id=media.asset_id,
            kind=media.kind,
            url=media.url,
            thumbnail_url=media.thumbnail_url,
            width=media.width,
            height=media.height,
            alt=media.alt,
            poster=media.poster,
            streams=EmbeddedMediaStreamsType(hls=streams.hls, dash=streams.dash) if streams else None,
            sources=[EmbeddedMediaSourceType(url=s.url, width=s.width, height=s.height) for s in media.sources],
            expires_in=media.expires_in,
            public=media.public,
        )


class _RequestState:
    def __init__(self, loader: DataLoader) -> None:
        self.loader = loader
        self.tokened = False


class StrawberryMedia:
    """Per-request ``EmbeddedMedia`` loading for strawberry resolvers.

    ``get_user(info)`` returns the user passed to ``can_access``. The loader
    lives in ``info.context`` (a dict or an object), so it is never shared
    between requests.
    """

    def __init__(self, embedder: MediaEmbedder, get_user: Callable[[Any], Any]):
        self.embedder = embedder
        self.get_user = get_user
        self._key = f"_pkg_media_embed_{id(self)}"

    def loader(self, info: Any) -> DataLoader:
        """This request's ``DataLoader[UUID, EmbeddedMediaType]``."""
        return self._state(info).loader

    async def load(self, info: Any, asset_id: Optional[Union[UUID, str]]) -> Optional[EmbeddedMediaType]:
        """The embedded asset, or None when ``asset_id`` is None."""
        if asset_id is None:
            return None
        return await self.loader(info).load(_as_uuid(asset_id))

    async def load_many(
        self, info: Any, asset_ids: List[Optional[Union[UUID, str]]]
    ) -> List[EmbeddedMediaType]:
        """The embedded assets, None ids left out."""
        ids = [_as_uuid(a) for a in asset_ids if a is not None]
        return list(await self.loader(info).load_many(ids)) if ids else []

    def tokened(self, info: Any) -> bool:
        """Whether this request loaded a tokened link so far."""
        return self._state(info).tokened

    def _state(self, info: Any) -> _RequestState:
        context = info.context
        state = _get(context, self._key)
        if state is None:
            state = _RequestState(DataLoader(load_fn=lambda ids: self._load(info, ids)))
            _set(context, self._key, state)
        return state

    async def _load(self, info: Any, asset_ids: List[UUID]) -> List[EmbeddedMediaType]:
        embeds = await self.embedder.aembed(self.get_user(info), asset_ids)
        if embeds.tokened:
            self._state(info).tokened = True
        return [EmbeddedMediaType.from_model(embeds[a]) for a in asset_ids]


def _get(context: Any, key: str) -> Optional[_RequestState]:
    if isinstance(context, dict):
        return context.get(key)
    return getattr(context, key, None)


def _set(context: Any, key: str, value: _RequestState) -> None:
    if isinstance(context, dict):
        context[key] = value
    else:
        setattr(context, key, value)
