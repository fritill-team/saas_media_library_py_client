"""An in-process gRPC Delivery service that answers JSON built by the tests.

Media's delivery is gRPC only, so the gate, embed, router and cache tests
run a real ``MediaClient(transport="grpc")`` against this server. Items are
the camelCase JSON of ``DeliveryBatchResolveResult`` (protobuf's JSON names
are the same), so tests describe results as media sends them.

``FakeDelivery`` answers each id with ``items[id]`` or ``default(id)``,
records every request's ids (``requests``) and metadata (``metadata``), and
fails the next calls given to ``fail(status, ...)`` (None: that call passes).
"""

from __future__ import annotations

from concurrent import futures
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional

import grpc
from google.protobuf import json_format

from pkg_media import MediaClient, contract
from pkg_media.contract import delivery_pb2


class FakeDelivery:
    def __init__(self, default: Callable[[str], Optional[dict]]) -> None:
        self.default = default
        self.items: Dict[str, Optional[dict]] = {}
        self.requests: List[List[str]] = []
        self.metadata: List[Dict[str, str]] = []
        self.failures: List[Optional[grpc.StatusCode]] = []
        self.target = ""
        self._clients: List[MediaClient] = []

    def set(self, asset_id, item: Optional[dict]) -> None:
        """Answer ``asset_id`` with ``item``; None leaves it out of the response."""
        self.items[str(asset_id)] = item

    def fail(self, *statuses: Optional[grpc.StatusCode]) -> None:
        self.failures.extend(statuses)

    def client(self, **kwargs: Any) -> MediaClient:
        """A gRPC client of this server, closed by ``serve``."""
        kwargs.setdefault("token", "service-token")
        client = MediaClient(transport="grpc", target=self.target, retries=0, **kwargs)
        self._clients.append(client)
        return client

    def answer(self, asset_ids: List[str]) -> List[dict]:
        items = [self.items[a] if a in self.items else self.default(a) for a in asset_ids]
        return [i for i in items if i is not None]


class _Delivery(contract.DeliveryServicer):
    def __init__(self, fake: FakeDelivery) -> None:
        self.fake = fake

    def _enter(self, ids: List[str], context: grpc.ServicerContext) -> None:
        self.fake.requests.append(ids)
        self.fake.metadata.append(dict(context.invocation_metadata()))
        status = self.fake.failures.pop(0) if self.fake.failures else None
        if status is not None:
            context.abort(status, "failed by the test")

    def ResolveDeliveryAssets(self, request, context):
        ids = list(request.asset_ids)
        self._enter(ids, context)
        items = self.fake.answer(ids)
        return json_format.ParseDict(
            {"items": items, "total": len(items)},
            delivery_pb2.ResolveDeliveryAssetsResponse(),
            ignore_unknown_fields=True,
        )

    def GetDeliveryAsset(self, request, context):
        self._enter([request.asset_id], context)
        [item] = self.fake.answer([request.asset_id]) or [None]
        if item is None or item.get("asset") is None:
            context.abort(grpc.StatusCode.NOT_FOUND, "asset not found")
        return json_format.ParseDict(
            {"asset": item["asset"]}, delivery_pb2.GetDeliveryAssetResponse(), ignore_unknown_fields=True
        )


@contextmanager
def serve(default: Callable[[str], Optional[dict]]) -> Iterator[FakeDelivery]:
    """Run a ``FakeDelivery`` for the duration of the block (a fixture body)."""
    fake = FakeDelivery(default)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    contract.add_DeliveryServicer_to_server(_Delivery(fake), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    fake.target = f"127.0.0.1:{port}"
    try:
        yield fake
    finally:
        for client in fake._clients:
            client.close()
        server.stop(0)
