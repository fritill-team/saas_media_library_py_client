"""The gRPC transport: ``fritill.media.v1`` over a ``grpc.aio`` channel.

- Credential: ``authorization: Bearer <token>`` metadata on every call, from
  the token source (``ClientCredentials`` for a Keycloak client). When media
  answers UNAUTHENTICATED the cached token is dropped and the call is made
  once more with a fresh one.
- Idempotency: every mutating method (``methods.MUTATING_METHODS``) gets an
  ``idempotency-key`` metadata, the same one on each retry of that call.
- Retries (AD-10): only UNAVAILABLE, and only for read-only or keyed calls
  (every call this transport makes is one or the other), with backoff.
- Errors: one exception class per status (``pkg_media.exceptions``), with
  ``reason`` and ``domain`` from the status's ``google.rpc.ErrorInfo``.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import timezone
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Type, TypeVar

import grpc
from google.protobuf import json_format, struct_pb2
from google.protobuf.message import Message
from pydantic import BaseModel

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
from pkg_media.exceptions import (
    DeadlineExceededError,
    FailedPreconditionError,
    InternalError,
    InvalidArgumentError,
    MediaLibraryError,
    NotFoundError,
    PermissionDeniedError,
    ResourceExhaustedError,
    UnauthenticatedError,
    UnavailableError,
    UnimplementedError,
)
from pkg_media.grants import MediaGrant
from pkg_media.models import (
    Asset,
    BatchResolveResult,
    DeliveryAsset,
    DeliveryBatchResolveResult,
    PaginatedAssets,
    PaginatedDeliveryAssets,
    UploadAbortResult,
    UploadFinalizeResult,
    UploadPartResult,
    UploadSession,
    WebAppDeployList,
)
from pkg_media.permalinks import Permalink

TokenSource = Callable[[], Awaitable[str]]
M = TypeVar("M", bound=BaseModel)

GRANTS_METADATA = "x-media-grants"

_ERRORS: Dict[grpc.StatusCode, Type[MediaLibraryError]] = {
    grpc.StatusCode.INVALID_ARGUMENT: InvalidArgumentError,
    grpc.StatusCode.UNAUTHENTICATED: UnauthenticatedError,
    grpc.StatusCode.PERMISSION_DENIED: PermissionDeniedError,
    grpc.StatusCode.NOT_FOUND: NotFoundError,
    grpc.StatusCode.FAILED_PRECONDITION: FailedPreconditionError,
    grpc.StatusCode.RESOURCE_EXHAUSTED: ResourceExhaustedError,
    grpc.StatusCode.UNAVAILABLE: UnavailableError,
    grpc.StatusCode.DEADLINE_EXCEEDED: DeadlineExceededError,
    grpc.StatusCode.UNIMPLEMENTED: UnimplementedError,
    grpc.StatusCode.INTERNAL: InternalError,
}

#: The JSON keys of a session/grant payload the contract carries.
_PROCESSING_KEYS = {"stepOptions", "useFilenameAsAssetId", "enableTranscription"}
_PROFILE_KEYS = {"kind", "visibility", "processingConfig", "collectionName", "allowedMimeTypes", "maxSizeBytes"}


def error_from_rpc(error: grpc.RpcError) -> MediaLibraryError:
    """The client exception for a failed call."""
    code = error.code() if callable(getattr(error, "code", None)) else grpc.StatusCode.UNKNOWN
    details = error.details() if callable(getattr(error, "details", None)) else None
    info = contract.error_info(error)
    domain, reason = info if info is not None else (None, None)
    cls = _ERRORS.get(code, MediaLibraryError)
    return cls(details or code.name, reason=reason, grpc_status=code, domain=domain)


class GrpcTransport:
    """Media's gRPC role (``target`` such as ``media-library-grpc:50051``)."""

    name = "grpc"

    def __init__(
        self,
        target: str,
        *,
        token: Optional[TokenSource] = None,
        on_rejected_token: Optional[Callable[[], None]] = None,
        timeout: Optional[float] = 30.0,
        retries: int = 2,
        backoff: float = 0.1,
        secure: bool = False,
        channel: Optional[grpc.aio.Channel] = None,
        channel_options: Sequence[Tuple[str, Any]] = (),
    ):
        if retries < 0:
            raise ValueError("retries cannot be negative")
        self.target = target
        self._token = token
        self._on_rejected_token = on_rejected_token
        self._timeout = timeout
        self.retries = retries
        self._backoff = backoff
        self._secure = secure
        self._channel = channel
        self._owns_channel = channel is None
        self._channel_options = list(channel_options)
        self._stubs: Dict[str, Any] = {}

    async def aclose(self) -> None:
        if self._channel is not None and self._owns_channel:
            await self._channel.close()
        self._channel = None
        self._stubs.clear()

    # ── Assets ─────────────────────────────────────────────────────────

    async def list_assets(self, params: Dict[str, Any]) -> PaginatedAssets:
        if params.get("resourcePolicyId") is not None:
            raise ValueError("resource_policy_id (resource policies are deprecated) is HTTP-only")
        resp = await self._call(methods.Assets.LIST_ASSETS, assets_pb2.ListAssetsRequest(**_list_fields(params)))
        return _model(PaginatedAssets, resp)

    async def get_asset(self, asset_id: str) -> Asset:
        resp = await self._call(methods.Assets.GET_ASSET, assets_pb2.GetAssetRequest(asset_id=asset_id))
        return _model(Asset, resp.asset)

    async def resolve_assets(self, asset_ids: List[str]) -> List[BatchResolveResult]:
        resp = await self._call(methods.Assets.RESOLVE_ASSETS, assets_pb2.ResolveAssetsRequest(asset_ids=asset_ids))
        return [_model(BatchResolveResult, item) for item in resp.items]

    async def delete_asset(self, asset_id: str) -> None:
        await self._call(methods.Assets.DELETE_ASSET, assets_pb2.DeleteAssetRequest(asset_id=asset_id))

    async def update_asset_visibility(self, asset_id: str, visibility: str) -> Asset:
        request = assets_pb2.UpdateAssetVisibilityRequest(asset_id=asset_id, visibility=visibility)
        resp = await self._call(methods.Assets.UPDATE_ASSET_VISIBILITY, request)
        return _model(Asset, resp.asset)

    async def bulk_update_asset_visibility(self, asset_ids: List[str], visibility: str) -> Dict[str, Any]:
        request = assets_pb2.BulkUpdateAssetVisibilityRequest(asset_ids=asset_ids, visibility=visibility)
        resp = await self._call(methods.Assets.BULK_UPDATE_ASSET_VISIBILITY, request)
        items = [
            {
                "assetId": item.asset_id,
                "ok": item.ok,
                "changed": item.changed if item.HasField("changed") else None,
                "error": item.error if item.HasField("error") else None,
            }
            for item in resp.items
        ]
        return {"items": items, "updated": resp.updated, "failed": resp.failed}

    async def list_deploys(self, asset_id: str, limit: int, offset: int) -> WebAppDeployList:
        request = assets_pb2.ListDeploysRequest(asset_id=asset_id, limit=limit, offset=offset)
        return _model(WebAppDeployList, await self._call(methods.Assets.LIST_DEPLOYS, request))

    async def activate_deploy(self, asset_id: str, deploy_id: str) -> WebAppDeployList:
        request = assets_pb2.ActivateDeployRequest(asset_id=asset_id, deploy_id=deploy_id)
        return _model(WebAppDeployList, await self._call(methods.Assets.ACTIVATE_DEPLOY, request))

    # ── Delivery ───────────────────────────────────────────────────────

    async def list_delivery_assets(self, params: Dict[str, Any]) -> PaginatedDeliveryAssets:
        request = delivery_pb2.ListDeliveryAssetsRequest(**_list_fields(params))
        return _model(PaginatedDeliveryAssets, await self._call(methods.Delivery.LIST_DELIVERY_ASSETS, request))

    async def get_delivery_asset(self, asset_id: str, grants: List[str]) -> DeliveryAsset:
        request = delivery_pb2.GetDeliveryAssetRequest(asset_id=asset_id)
        resp = await self._call(methods.Delivery.GET_DELIVERY_ASSET, request, _grant_metadata(grants))
        return _model(DeliveryAsset, resp.asset)

    async def resolve_delivery_assets(self, asset_ids: List[str], grants: List[str]) -> List[DeliveryBatchResolveResult]:
        request = delivery_pb2.ResolveDeliveryAssetsRequest(asset_ids=asset_ids)
        resp = await self._call(methods.Delivery.RESOLVE_DELIVERY_ASSETS, request, _grant_metadata(grants))
        return [_model(DeliveryBatchResolveResult, item) for item in resp.items]

    # ── Uploads ────────────────────────────────────────────────────────

    async def create_session(self, payload: Dict[str, Any]) -> UploadSession:
        """``Uploads.OpenSession``; the session's grant is not needed here."""
        session, _ = await self.open_session(payload, {})
        return session

    async def open_session(
        self,
        payload: Dict[str, Any],
        grant_payload: Dict[str, Any],
    ) -> Tuple[UploadSession, MediaGrant]:
        request = uploads_pb2.OpenSessionRequest(
            mime_type=payload["mime_type"],
            kind=payload["kind"],
            visibility=payload.get("visibility"),
            original_filename=payload.get("original_filename"),
            expected_size_bytes=payload.get("expected_size_bytes"),
            expected_sha256_hex=payload.get("expected_sha256_hex"),
            collection_name=payload.get("collection_name"),
            subject=grant_payload.get("subject"),
            grant_ttl_seconds=grant_payload.get("ttlSeconds"),
        )
        if payload.get("processingConfig") is not None:
            request.processing_config.CopyFrom(_processing_config(payload["processingConfig"]))
        resp = await self._call(methods.Uploads.OPEN_SESSION, request)
        return _model(UploadSession, resp.session), _grant(resp.grant)

    async def get_session(self, session_id: str) -> UploadSession:
        resp = await self._call(methods.Uploads.GET_SESSION, uploads_pb2.GetSessionRequest(session_id=session_id))
        return _model(UploadSession, resp.session)

    async def upload_part(self, session_id: str, part_number: int, body: bytes, headers: Dict[str, str]) -> UploadPartResult:
        raise MediaLibraryError(
            "File bytes never travel over gRPC: browsers upload parts to media's HTTP API with an "
            "upload grant (uploads.open_session). A backend that uploads bytes itself needs transport='http'."
        )

    async def finalize_session(self, session_id: str, expected_sha256_hex: Optional[str]) -> UploadFinalizeResult:
        request = uploads_pb2.FinalizeSessionRequest(session_id=session_id, expected_sha256_hex=expected_sha256_hex)
        return _model(UploadFinalizeResult, await self._call(methods.Uploads.FINALIZE_SESSION, request))

    async def abort_session(self, session_id: str) -> UploadAbortResult:
        request = uploads_pb2.AbortSessionRequest(session_id=session_id)
        return _model(UploadAbortResult, await self._call(methods.Uploads.ABORT_SESSION, request))

    # ── Grants ─────────────────────────────────────────────────────────

    async def issue_grant(self, payload: Dict[str, Any]) -> MediaGrant:
        request = grants_pb2.IssueGrantRequest(
            scopes=payload["scopes"],
            asset_ids=payload.get("assetIds") or [],
            session_ids=payload.get("sessionIds") or [],
            subject=payload.get("subject"),
            ttl_seconds=payload.get("ttlSeconds"),
        )
        if payload.get("profile") is not None:
            request.profile.CopyFrom(_grant_profile(payload["profile"]))
        resp = await self._call(methods.Grants.ISSUE_GRANT, request)
        return _grant(resp.grant)

    # ── Permalinks ─────────────────────────────────────────────────────

    async def set_permalink(self, name: str, asset_id: str) -> Permalink:
        request = permalinks_pb2.SetPermalinkRequest(name=name, asset_id=asset_id)
        resp = await self._call(methods.Permalinks.SET_PERMALINK, request)
        return _model(Permalink, resp.permalink)

    async def get_permalink(self, name: str) -> Permalink:
        resp = await self._call(methods.Permalinks.GET_PERMALINK, permalinks_pb2.GetPermalinkRequest(name=name))
        return _model(Permalink, resp.permalink)

    async def delete_permalink(self, name: str) -> None:
        await self._call(methods.Permalinks.DELETE_PERMALINK, permalinks_pb2.DeletePermalinkRequest(name=name))

    # ── Processing ─────────────────────────────────────────────────────

    async def get_processing_schema(self) -> Dict[str, Any]:
        request = processing_pb2.GetProcessingSchemaRequest()
        resp = await self._call(methods.Processing.GET_PROCESSING_SCHEMA, request)
        return _whole_numbers(json_format.MessageToDict(resp.schema))

    async def get_processing_pipelines(self) -> Dict[str, Any]:
        request = processing_pb2.GetProcessingPipelinesRequest()
        resp = await self._call(methods.Processing.GET_PROCESSING_PIPELINES, request)
        return _whole_numbers(json_format.MessageToDict(resp.pipelines))

    # ── Calls ──────────────────────────────────────────────────────────

    def _channel_or_open(self) -> grpc.aio.Channel:
        # Opened lazily, inside the running loop that will use it.
        if self._channel is None:
            options = [("grpc.enable_retries", 0), *self._channel_options]
            if self._secure:
                self._channel = grpc.aio.secure_channel(self.target, grpc.ssl_channel_credentials(), options=options)
            else:
                self._channel = grpc.aio.insecure_channel(self.target, options=options)
            self._owns_channel = True
        return self._channel

    def _callable(self, path: str) -> Any:
        callable_ = self._stubs.get(path)
        if callable_ is None:
            service, method = path.rsplit("/", 1)
            stub_class = _STUBS[service.lstrip("/")]
            callable_ = getattr(stub_class(self._channel_or_open()), method)
            self._stubs[path] = callable_
        return callable_

    async def _call(self, path: str, request: Message, extra: Sequence[Tuple[str, str]] = ()) -> Any:
        key = uuid.uuid4().hex if path in methods.MUTATING_METHODS else None
        retryable = key is not None or path in methods.READ_ONLY_METHODS
        attempt = 0
        renewed_token = False
        while True:
            metadata: List[Tuple[str, str]] = []
            if self._token is not None:
                metadata.append((methods.AUTHORIZATION_METADATA, f"Bearer {await self._token()}"))
            if key is not None:
                metadata.append((methods.IDEMPOTENCY_KEY_METADATA, key))
            metadata.extend(extra)
            try:
                return await self._callable(path)(request, metadata=metadata, timeout=self._timeout)
            except grpc.RpcError as exc:
                code = exc.code()
                if code == grpc.StatusCode.UNAUTHENTICATED and self._on_rejected_token and not renewed_token:
                    # A token revoked or rotated before its expiry: fetch a new one once.
                    self._on_rejected_token()
                    renewed_token = True
                    continue
                if code == grpc.StatusCode.UNAVAILABLE and retryable and attempt < self.retries:
                    attempt += 1
                    await asyncio.sleep(self._backoff * (2 ** (attempt - 1)) * (1 + random.random()))
                    continue
                raise error_from_rpc(exc) from None


_STUBS = {
    methods.Assets.SERVICE: contract.AssetsStub,
    methods.Uploads.SERVICE: contract.UploadsStub,
    methods.Grants.SERVICE: contract.GrantsStub,
    methods.Permalinks.SERVICE: contract.PermalinksStub,
    methods.Delivery.SERVICE: contract.DeliveryStub,
    methods.Processing.SERVICE: contract.ProcessingStub,
}


# ── Mapping ────────────────────────────────────────────────────────────


def _model(model: Type[M], message: Message) -> M:
    """``message`` as ``model``: proto field names are the models' snake_case
    names (every model has ``populate_by_name``)."""
    data = json_format.MessageToDict(
        message,
        preserving_proto_field_name=True,
        always_print_fields_with_no_presence=True,
    )
    return model.model_validate(_whole_numbers(data))


def _whole_numbers(value: Any) -> Any:
    """Struct carries every number as a double: 80.0 back to 80."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _whole_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_whole_numbers(v) for v in value]
    return value


def _grant(message: common_pb2.Grant) -> MediaGrant:
    return MediaGrant(
        grant=message.grant,
        grant_id=message.grant_id,
        scopes=list(message.scopes),
        expires_in=message.expires_in,
        expires_at=message.expires_at.ToDatetime(tzinfo=timezone.utc),
    )


def _list_fields(params: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "kind": params.get("kind"),
        "status": params.get("status"),
        "query": params.get("query"),
        "page": params.get("page", 0),
        "page_size": params.get("pageSize", 0),
    }


def _processing_config(payload: Mapping[str, Any]) -> common_pb2.ProcessingConfig:
    unknown = set(payload) - _PROCESSING_KEYS
    if unknown:
        raise ValueError(f"processing config keys not in the gRPC contract: {sorted(unknown)}")
    config = common_pb2.ProcessingConfig(
        use_filename_as_asset_id=bool(payload.get("useFilenameAsAssetId", False)),
        enable_transcription=bool(payload.get("enableTranscription", False)),
    )
    if payload.get("stepOptions") is not None:
        config.step_options.CopyFrom(_struct(payload["stepOptions"]))
    return config


def _grant_profile(payload: Mapping[str, Any]) -> grants_pb2.GrantUploadProfile:
    unknown = set(payload) - _PROFILE_KEYS
    if unknown:
        raise ValueError(f"grant profile keys not in the gRPC contract: {sorted(unknown)}")
    allowed = payload.get("allowedMimeTypes")
    if allowed is not None and not allowed:
        raise ValueError("an empty allowedMimeTypes means any type over gRPC; omit it instead")
    profile = grants_pb2.GrantUploadProfile(
        kind=payload["kind"],
        visibility=payload.get("visibility"),
        collection_name=payload.get("collectionName"),
        allowed_mime_types=allowed or [],
        max_size_bytes=payload.get("maxSizeBytes"),
    )
    if payload.get("processingConfig") is not None:
        profile.processing_config.CopyFrom(_processing_config(payload["processingConfig"]))
    return profile


def _struct(value: Mapping[str, Any]) -> struct_pb2.Struct:
    struct = struct_pb2.Struct()
    struct.update(dict(value))
    return struct


def _grant_metadata(grants: List[str]) -> List[Tuple[str, str]]:
    return [(GRANTS_METADATA, ", ".join(grants))] if grants else []

