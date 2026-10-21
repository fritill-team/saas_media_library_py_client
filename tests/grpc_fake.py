"""An in-process media gRPC server built from the contract's base classes.

Every method records the call (metadata included) and answers with canned
messages; ``FakeMedia.fail(path, *statuses)`` makes the next calls of a method
fail with those statuses (``grpc.StatusCode`` or a ``grpc.Status`` from
``refusal_status``).
"""

from __future__ import annotations

import threading
from collections import defaultdict
from concurrent import futures
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Union
from uuid import uuid4

import grpc
from google.protobuf import struct_pb2, timestamp_pb2

from pkg_media import contract, methods
from pkg_media.contract import (
    assets_pb2,
    common_pb2,
    delivery_pb2,
    grants_pb2,
    permalinks_pb2,
    processing_pb2,
    uploads_pb2,
)

CREATED = datetime(2026, 10, 21, 9, 30, tzinfo=timezone.utc)


def ts(value: datetime = CREATED) -> timestamp_pb2.Timestamp:
    stamp = timestamp_pb2.Timestamp()
    stamp.FromDatetime(value)
    return stamp


def struct(value: Dict[str, Any]) -> struct_pb2.Struct:
    s = struct_pb2.Struct()
    s.update(value)
    return s


def asset_message(asset_id: str, **overrides: Any) -> common_pb2.Asset:
    fields: Dict[str, Any] = dict(
        id=asset_id,
        kind="video",
        title="Lesson 1",
        original_filename="lesson.mp4",
        mime_type="video/mp4",
        size_bytes=5_000_000_000,
        hash_sha256="ab" * 32,
        status="ready",
        visibility="restricted",
        created_at=ts(),
        upload_session_id=str(uuid4()),
        renditions=[
            common_pb2.Rendition(
                id=str(uuid4()),
                rendition_type="thumbnail",
                entrypoint="thumb.jpg",
                status="ready",
                extras=struct({"width": 320, "ratio": 1.5}),
                created_at=ts(),
                updated_at=ts(),
                variants=[common_pb2.RenditionVariant(name="320", entrypoint="320.jpg", width=320)],
                preview_url="https://media.test/o/thumb",
                preview_url_type="gate",
            )
        ],
        preview_url="https://media.test/o/file",
        preview_url_type="gate",
        preview_url_expires_in=3600,
    )
    fields.update(overrides)
    return common_pb2.Asset(**fields)


def delivery_asset_message(asset_id: str) -> delivery_pb2.DeliveryAsset:
    return delivery_pb2.DeliveryAsset(
        id=asset_id,
        kind="image",
        status="ready",
        visibility="public",
        mime_type="image/png",
        download_type="gate",
        download_url=f"https://media.test/o/{asset_id}",
        renditions=[
            delivery_pb2.DeliveryRendition(
                id=str(uuid4()),
                rendition_type="thumbnail",
                status="ready",
                entrypoint="t.jpg",
                preview_url=f"https://media.test/t/{asset_id}",
            )
        ],
    )


def session_message(session_id: str, state: str = "initiated", **overrides: Any) -> uploads_pb2.UploadSession:
    fields: Dict[str, Any] = dict(
        id=session_id,
        state=state,
        part_size_bytes=8 * 1024 * 1024,
        parts_received=0,
        expires_at=ts(),
        checksum_verified=False,
    )
    fields.update(overrides)
    return uploads_pb2.UploadSession(**fields)


def grant_message(scopes: List[str]) -> common_pb2.Grant:
    return common_pb2.Grant(grant="mlg_test", grant_id=str(uuid4()), scopes=scopes, expires_in=600, expires_at=ts())


@dataclass
class Call:
    path: str
    request: Any
    metadata: Dict[str, str]


@dataclass
class FakeMedia:
    calls: List[Call] = field(default_factory=list)
    failures: Dict[str, List[Union[grpc.StatusCode, grpc.Status]]] = field(default_factory=lambda: defaultdict(list))
    sessions: Dict[str, uploads_pb2.UploadSession] = field(default_factory=dict)
    assets: Dict[str, common_pb2.Asset] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def fail(self, path: str, *outcomes: Union[grpc.StatusCode, grpc.Status]) -> None:
        self.failures[path].extend(outcomes)

    def calls_to(self, path: str) -> List[Call]:
        return [c for c in self.calls if c.path == path]

    def enter(self, path: str, request: Any, context: grpc.ServicerContext) -> None:
        with self.lock:
            self.calls.append(Call(path, request, dict(context.invocation_metadata())))
            outcome = self.failures[path].pop(0) if self.failures[path] else None
        if outcome is None:
            return
        if isinstance(outcome, grpc.StatusCode):
            context.abort(outcome, f"planned {outcome.name}")
        context.abort_with_status(outcome)


class _Assets(contract.AssetsServicer):
    def __init__(self, fake: FakeMedia):
        self.fake = fake

    def ListAssets(self, request, context):
        self.fake.enter(methods.Assets.LIST_ASSETS, request, context)
        items = [asset_message(str(uuid4()))]
        return assets_pb2.ListAssetsResponse(items=items, total=41, page=request.page or 1, page_size=request.page_size or 10)

    def GetAsset(self, request, context):
        self.fake.enter(methods.Assets.GET_ASSET, request, context)
        return assets_pb2.GetAssetResponse(asset=self.fake.assets.get(request.asset_id) or asset_message(request.asset_id))

    def ResolveAssets(self, request, context):
        self.fake.enter(methods.Assets.RESOLVE_ASSETS, request, context)
        items = [
            assets_pb2.ResolveAssetsItem(asset_id=a, status="ok", asset=asset_message(a)) if i == 0
            else assets_pb2.ResolveAssetsItem(asset_id=a, status="not_found")
            for i, a in enumerate(request.asset_ids)
        ]
        return assets_pb2.ResolveAssetsResponse(items=items, total=len(items))

    def DeleteAsset(self, request, context):
        self.fake.enter(methods.Assets.DELETE_ASSET, request, context)
        return assets_pb2.DeleteAssetResponse()

    def UpdateAssetVisibility(self, request, context):
        self.fake.enter(methods.Assets.UPDATE_ASSET_VISIBILITY, request, context)
        return assets_pb2.UpdateAssetVisibilityResponse(asset=asset_message(request.asset_id, visibility=request.visibility))

    def BulkUpdateAssetVisibility(self, request, context):
        self.fake.enter(methods.Assets.BULK_UPDATE_ASSET_VISIBILITY, request, context)
        items = [assets_pb2.BulkUpdateAssetVisibilityItem(asset_id=request.asset_ids[0], ok=True, changed=True)]
        items += [
            assets_pb2.BulkUpdateAssetVisibilityItem(asset_id=a, ok=False, error="not_found")
            for a in request.asset_ids[1:]
        ]
        return assets_pb2.BulkUpdateAssetVisibilityResponse(items=items, updated=1, failed=len(items) - 1)

    def ListDeploys(self, request, context):
        self.fake.enter(methods.Assets.LIST_DEPLOYS, request, context)
        deploy = assets_pb2.WebAppDeploy(id=str(uuid4()), status="live", created_at=ts(), is_live=True, file_count=3)
        return assets_pb2.ListDeploysResponse(
            live_deploy_id=deploy.id, deploys=[deploy], limit=request.limit, offset=request.offset
        )

    def ActivateDeploy(self, request, context):
        self.fake.enter(methods.Assets.ACTIVATE_DEPLOY, request, context)
        deploy = assets_pb2.WebAppDeploy(id=request.deploy_id, status="live", created_at=ts(), is_live=True, file_count=1)
        return assets_pb2.ActivateDeployResponse(live_deploy_id=request.deploy_id, deploys=[deploy], limit=50, offset=0)


class _Uploads(contract.UploadsServicer):
    def __init__(self, fake: FakeMedia):
        self.fake = fake

    def OpenSession(self, request, context):
        self.fake.enter(methods.Uploads.OPEN_SESSION, request, context)
        session = session_message(str(uuid4()))
        self.fake.sessions[session.id] = session
        return uploads_pb2.OpenSessionResponse(session=session, grant=grant_message(["upload"]))

    def GetSession(self, request, context):
        self.fake.enter(methods.Uploads.GET_SESSION, request, context)
        session = self.fake.sessions.get(request.session_id)
        if session is None:
            from pkg_media import reasons

            context.abort_with_status(
                contract.refusal_status(methods.Uploads.SERVICE, reasons.SESSION_NOT_FOUND, "Upload session not found")
            )
        return uploads_pb2.GetSessionResponse(session=session)

    def FinalizeSession(self, request, context):
        self.fake.enter(methods.Uploads.FINALIZE_SESSION, request, context)
        return uploads_pb2.FinalizeSessionResponse(
            session_id=request.session_id, final_blob_id=str(uuid4()), asset_id=str(uuid4()),
            checksum_verified=request.HasField("expected_sha256_hex"), sha256_hex="cd" * 32,
        )

    def AbortSession(self, request, context):
        self.fake.enter(methods.Uploads.ABORT_SESSION, request, context)
        return uploads_pb2.AbortSessionResponse(session_id=request.session_id, aborted=True)


class _Grants(contract.GrantsServicer):
    def __init__(self, fake: FakeMedia):
        self.fake = fake

    def IssueGrant(self, request, context):
        self.fake.enter(methods.Grants.ISSUE_GRANT, request, context)
        return grants_pb2.IssueGrantResponse(grant=grant_message(list(request.scopes)))


class _Permalinks(contract.PermalinksServicer):
    def __init__(self, fake: FakeMedia):
        self.fake = fake

    def _permalink(self, name: str, asset_id: str) -> permalinks_pb2.Permalink:
        return permalinks_pb2.Permalink(
            name=name, asset_id=asset_id, url=f"https://media.test/p/{name}", created_at=ts(), updated_at=ts()
        )

    def SetPermalink(self, request, context):
        self.fake.enter(methods.Permalinks.SET_PERMALINK, request, context)
        return permalinks_pb2.SetPermalinkResponse(permalink=self._permalink(request.name, request.asset_id))

    def GetPermalink(self, request, context):
        self.fake.enter(methods.Permalinks.GET_PERMALINK, request, context)
        return permalinks_pb2.GetPermalinkResponse(permalink=self._permalink(request.name, str(uuid4())))

    def DeletePermalink(self, request, context):
        self.fake.enter(methods.Permalinks.DELETE_PERMALINK, request, context)
        return permalinks_pb2.DeletePermalinkResponse()


class _Delivery(contract.DeliveryServicer):
    def __init__(self, fake: FakeMedia):
        self.fake = fake

    def ListDeliveryAssets(self, request, context):
        self.fake.enter(methods.Delivery.LIST_DELIVERY_ASSETS, request, context)
        return delivery_pb2.ListDeliveryAssetsResponse(
            items=[delivery_asset_message(str(uuid4()))], total=1, page=1, page_size=10
        )

    def GetDeliveryAsset(self, request, context):
        self.fake.enter(methods.Delivery.GET_DELIVERY_ASSET, request, context)
        return delivery_pb2.GetDeliveryAssetResponse(asset=delivery_asset_message(request.asset_id))

    def ResolveDeliveryAssets(self, request, context):
        self.fake.enter(methods.Delivery.RESOLVE_DELIVERY_ASSETS, request, context)
        items = []
        for a in request.asset_ids:
            if a.endswith("0"):
                items.append(delivery_pb2.ResolveDeliveryAssetsItem(
                    asset_id=a, status="forbidden", thumbnail_url=f"https://media.test/t/{a}"
                ))
            else:
                items.append(delivery_pb2.ResolveDeliveryAssetsItem(
                    asset_id=a, status="ok", asset=delivery_asset_message(a)
                ))
        return delivery_pb2.ResolveDeliveryAssetsResponse(items=items, total=len(items))


SCHEMA = {
    "kinds": ["image", "video"],
    "pipelinesByKind": {"image": {"pipeline": ["image_derive"], "processors": {
        "image_derive": {"options": {"quality": {"type": "integer", "default": 80}, "scale": {"default": 0.5}}}
    }, "configurableSteps": ["image_derive"]}},
    "transcriptionAvailable": False,
}


class _Processing(contract.ProcessingServicer):
    def __init__(self, fake: FakeMedia):
        self.fake = fake

    def GetProcessingSchema(self, request, context):
        self.fake.enter(methods.Processing.GET_PROCESSING_SCHEMA, request, context)
        return processing_pb2.GetProcessingSchemaResponse(schema=struct(SCHEMA))

    def GetProcessingPipelines(self, request, context):
        self.fake.enter(methods.Processing.GET_PROCESSING_PIPELINES, request, context)
        return processing_pb2.GetProcessingPipelinesResponse(pipelines=struct({"image": SCHEMA["pipelinesByKind"]["image"]}))


def start(fake: FakeMedia) -> tuple[grpc.Server, str]:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    contract.add_AssetsServicer_to_server(_Assets(fake), server)
    contract.add_UploadsServicer_to_server(_Uploads(fake), server)
    contract.add_GrantsServicer_to_server(_Grants(fake), server)
    contract.add_PermalinksServicer_to_server(_Permalinks(fake), server)
    contract.add_DeliveryServicer_to_server(_Delivery(fake), server)
    contract.add_ProcessingServicer_to_server(_Processing(fake), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    return server, f"127.0.0.1:{port}"
