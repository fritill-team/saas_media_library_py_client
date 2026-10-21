"""The ``itqadem.media.v1`` contract: servicer base classes, stubs, messages
and refusal helpers.

The media server subclasses the servicers (thin: map in, call a use case, map
out) and registers them with the ``add_*_to_server`` functions; refusals go
out through ``refusal_status``. Clients use the stubs (``MediaClient`` does).
Messages live in the ``*_pb2`` modules re-exported here::

    from pkg_media.contract import AssetsServicer, assets_pb2, refusal_status

    class AssetsService(AssetsServicer):
        async def GetAsset(self, request, context):
            ...
            await context.abort_with_status(
                refusal_status(methods.Assets.SERVICE, reasons.ASSET_NOT_FOUND, "Asset not found")
            )
"""

from __future__ import annotations

from typing import Optional, Tuple

import grpc
from google.protobuf import any_pb2
from google.rpc import error_details_pb2, status_pb2
from grpc_status import rpc_status

from pkg_media.generated.itqadem.media.v1 import (
    assets_pb2,
    common_pb2,
    delivery_pb2,
    grants_pb2,
    permalinks_pb2,
    processing_pb2,
    uploads_pb2,
)
from pkg_media.generated.itqadem.media.v1.assets_pb2_grpc import (
    AssetsServicer,
    AssetsStub,
    add_AssetsServicer_to_server,
)
from pkg_media.generated.itqadem.media.v1.delivery_pb2_grpc import (
    DeliveryServicer,
    DeliveryStub,
    add_DeliveryServicer_to_server,
)
from pkg_media.generated.itqadem.media.v1.grants_pb2_grpc import (
    GrantsServicer,
    GrantsStub,
    add_GrantsServicer_to_server,
)
from pkg_media.generated.itqadem.media.v1.permalinks_pb2_grpc import (
    PermalinksServicer,
    PermalinksStub,
    add_PermalinksServicer_to_server,
)
from pkg_media.generated.itqadem.media.v1.processing_pb2_grpc import (
    ProcessingServicer,
    ProcessingStub,
    add_ProcessingServicer_to_server,
)
from pkg_media.generated.itqadem.media.v1.uploads_pb2_grpc import (
    UploadsServicer,
    UploadsStub,
    add_UploadsServicer_to_server,
)
from pkg_media.reasons import REFUSALS

__all__ = [
    "AssetsServicer", "AssetsStub", "add_AssetsServicer_to_server",
    "UploadsServicer", "UploadsStub", "add_UploadsServicer_to_server",
    "GrantsServicer", "GrantsStub", "add_GrantsServicer_to_server",
    "PermalinksServicer", "PermalinksStub", "add_PermalinksServicer_to_server",
    "DeliveryServicer", "DeliveryStub", "add_DeliveryServicer_to_server",
    "ProcessingServicer", "ProcessingStub", "add_ProcessingServicer_to_server",
    "assets_pb2", "common_pb2", "delivery_pb2", "grants_pb2", "permalinks_pb2",
    "processing_pb2", "uploads_pb2",
    "SERVICERS", "refusal_status", "error_info",
]

#: Service full name -> (servicer base class, registration function).
SERVICERS = {
    "itqadem.media.v1.Assets": (AssetsServicer, add_AssetsServicer_to_server),
    "itqadem.media.v1.Uploads": (UploadsServicer, add_UploadsServicer_to_server),
    "itqadem.media.v1.Grants": (GrantsServicer, add_GrantsServicer_to_server),
    "itqadem.media.v1.Permalinks": (PermalinksServicer, add_PermalinksServicer_to_server),
    "itqadem.media.v1.Delivery": (DeliveryServicer, add_DeliveryServicer_to_server),
    "itqadem.media.v1.Processing": (ProcessingServicer, add_ProcessingServicer_to_server),
}

_DETAILS_KEY = "grpc-status-details-bin"


def refusal_status(
    service: str,
    reason: str,
    message: str,
    *,
    code: Optional[grpc.StatusCode] = None,
) -> grpc.Status:
    """A status with ``ErrorInfo(domain=service, reason=reason)``, for
    ``context.abort_with_status``.

    ``code`` defaults to the reason's status in ``reasons.REFUSALS``. A reason
    the service does not declare raises ``ValueError``: every refusal must be
    in the service's table.
    """
    declared = REFUSALS.get(service)
    if declared is None or reason not in declared:
        raise ValueError(f"{service} does not declare the refusal reason {reason!r}")
    status_code = code or declared[reason]
    detail = any_pb2.Any()
    detail.Pack(error_details_pb2.ErrorInfo(domain=service, reason=reason))
    return rpc_status.to_status(
        status_pb2.Status(code=status_code.value[0], message=message, details=[detail])
    )


def error_info(error: grpc.RpcError) -> Optional[Tuple[str, str]]:
    """``(domain, reason)`` of a failed call's ``ErrorInfo``, or None."""
    trailing = getattr(error, "trailing_metadata", None)
    metadata = trailing() if callable(trailing) else None
    for key, value in metadata or ():
        if key != _DETAILS_KEY:
            continue
        status = status_pb2.Status.FromString(value)
        for detail in status.details:
            if detail.Is(error_details_pb2.ErrorInfo.DESCRIPTOR):
                info = error_details_pb2.ErrorInfo()
                detail.Unpack(info)
                return info.domain, info.reason
    return None
