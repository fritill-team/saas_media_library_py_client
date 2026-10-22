from typing import List, Optional
from uuid import UUID, uuid4

import pytest

strawberry = pytest.importorskip("strawberry")

from pkg_media import MediaEmbedder  # noqa: E402
from pkg_media.integrations.strawberry import EmbeddedMediaType, StrawberryMedia  # noqa: E402

from tests.test_embed import DOWNLOAD, THUMB, _client, server  # noqa: E402,F401


def _schema(media: StrawberryMedia, articles):
    @strawberry.type
    class Article:
        title: str
        cover_id: strawberry.Private[Optional[UUID]]

        @strawberry.field
        async def cover(self, info: strawberry.Info) -> Optional[EmbeddedMediaType]:
            return await media.load(info, self.cover_id)

    @strawberry.type
    class Query:
        @strawberry.field
        def articles(self) -> List[Article]:
            return [Article(title=t, cover_id=c) for t, c in articles]

    return strawberry.Schema(query=Query)


QUERY = """{ articles { title cover {
    assetId kind url thumbnailUrl width height alt poster
    streams { hls dash } sources { url width height } expiresIn public
} } }"""


async def test_cover_field_batches_one_call_per_request(server) -> None:  # noqa: F811
    allowed, denied = uuid4(), uuid4()
    calls = []

    def can_access(user, ids):
        calls.append((user, list(ids)))
        return [allowed]

    media = StrawberryMedia(MediaEmbedder(_client(), can_access), get_user=lambda info: info.context["user"])
    schema = _schema(media, [("a", allowed), ("b", denied), ("c", None), ("d", allowed)])
    context = {"user": "alice"}

    result = await schema.execute(QUERY, context_value=context)

    assert result.errors is None
    covers = [a["cover"] for a in result.data["articles"]]
    assert covers[0]["url"] == DOWNLOAD.format(id=allowed)
    assert covers[0]["assetId"] == str(allowed)
    assert covers[0]["streams"]["hls"]
    assert covers[0]["expiresIn"] == 3600
    assert covers[0]["public"] is False
    assert covers[1]["url"] is None
    assert covers[1]["thumbnailUrl"] == THUMB.format(id=denied)
    assert covers[2] is None
    assert covers[3] == covers[0]
    # One can_access call and one media library call for the whole request.
    assert len(calls) == 1 and set(calls[0][1]) == {allowed, denied}
    assert len(server.requests) == 1
    assert media.tokened(_Info(context)) is True


async def test_loader_is_per_request(server) -> None:  # noqa: F811
    a = uuid4()
    media = StrawberryMedia(MediaEmbedder(_client(), lambda u, ids: ids, cache=False), get_user=lambda info: None)
    schema = _schema(media, [("a", a)])

    await schema.execute(QUERY, context_value={})
    await schema.execute(QUERY, context_value={})

    assert len(server.requests) == 2


async def test_object_context_and_load_many(server) -> None:  # noqa: F811
    a, b = uuid4(), uuid4()

    class Context:
        user = "alice"

    media = StrawberryMedia(MediaEmbedder(_client(), lambda u, ids: ids), get_user=lambda info: info.context.user)
    info = _Info(Context())

    loaded = await media.load_many(info, [a, None, b])

    assert [m.asset_id for m in loaded] == [a, b]
    assert await media.load(info, None) is None
    assert media.tokened(info) is True


class _Info:
    def __init__(self, context) -> None:
        self.context = context
