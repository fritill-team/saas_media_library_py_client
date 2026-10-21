"""Wire paths of the ``fritill.media.v1`` methods.

One constant per method, ``/fritill.media.v1.<Service>/<Method>``: the key the
media server's access table and idempotency records use, and what interceptors
see as ``handler_call_details.method``. ``tests/test_media_contract.py`` checks
them against the generated descriptors.

``MUTATING_METHODS`` need the ``idempotency-key`` metadata (the client adds
one); the others are read-only. The client retries ``UNAVAILABLE`` for both
(read-only calls are safe to repeat, keyed calls are deduplicated by media).
"""

from __future__ import annotations

PACKAGE = "fritill.media.v1"


class Assets:
    SERVICE = "fritill.media.v1.Assets"
    LIST_ASSETS = "/fritill.media.v1.Assets/ListAssets"
    GET_ASSET = "/fritill.media.v1.Assets/GetAsset"
    RESOLVE_ASSETS = "/fritill.media.v1.Assets/ResolveAssets"
    DELETE_ASSET = "/fritill.media.v1.Assets/DeleteAsset"
    UPDATE_ASSET_VISIBILITY = "/fritill.media.v1.Assets/UpdateAssetVisibility"
    BULK_UPDATE_ASSET_VISIBILITY = "/fritill.media.v1.Assets/BulkUpdateAssetVisibility"
    LIST_DEPLOYS = "/fritill.media.v1.Assets/ListDeploys"
    ACTIVATE_DEPLOY = "/fritill.media.v1.Assets/ActivateDeploy"


class Uploads:
    SERVICE = "fritill.media.v1.Uploads"
    OPEN_SESSION = "/fritill.media.v1.Uploads/OpenSession"
    GET_SESSION = "/fritill.media.v1.Uploads/GetSession"
    FINALIZE_SESSION = "/fritill.media.v1.Uploads/FinalizeSession"
    ABORT_SESSION = "/fritill.media.v1.Uploads/AbortSession"


class Grants:
    SERVICE = "fritill.media.v1.Grants"
    ISSUE_GRANT = "/fritill.media.v1.Grants/IssueGrant"


class Permalinks:
    SERVICE = "fritill.media.v1.Permalinks"
    SET_PERMALINK = "/fritill.media.v1.Permalinks/SetPermalink"
    GET_PERMALINK = "/fritill.media.v1.Permalinks/GetPermalink"
    DELETE_PERMALINK = "/fritill.media.v1.Permalinks/DeletePermalink"


class Delivery:
    SERVICE = "fritill.media.v1.Delivery"
    LIST_DELIVERY_ASSETS = "/fritill.media.v1.Delivery/ListDeliveryAssets"
    GET_DELIVERY_ASSET = "/fritill.media.v1.Delivery/GetDeliveryAsset"
    RESOLVE_DELIVERY_ASSETS = "/fritill.media.v1.Delivery/ResolveDeliveryAssets"


class Processing:
    SERVICE = "fritill.media.v1.Processing"
    GET_PROCESSING_SCHEMA = "/fritill.media.v1.Processing/GetProcessingSchema"
    GET_PROCESSING_PIPELINES = "/fritill.media.v1.Processing/GetProcessingPipelines"


SERVICES = (Assets, Uploads, Grants, Permalinks, Delivery, Processing)

#: Every method's wire path.
ALL_METHODS = frozenset(
    value
    for service in SERVICES
    for name, value in vars(service).items()
    if name.isupper() and name != "SERVICE"
)

#: Methods that change state; each needs the ``idempotency-key`` metadata.
MUTATING_METHODS = frozenset({
    Assets.DELETE_ASSET,
    Assets.UPDATE_ASSET_VISIBILITY,
    Assets.BULK_UPDATE_ASSET_VISIBILITY,
    Assets.ACTIVATE_DEPLOY,
    Uploads.OPEN_SESSION,
    Uploads.FINALIZE_SESSION,
    Uploads.ABORT_SESSION,
    Grants.ISSUE_GRANT,
    Permalinks.SET_PERMALINK,
    Permalinks.DELETE_PERMALINK,
})

READ_ONLY_METHODS = ALL_METHODS - MUTATING_METHODS

#: The metadata keys of the contract (gRPC metadata keys are lower case).
AUTHORIZATION_METADATA = "authorization"
IDEMPOTENCY_KEY_METADATA = "idempotency-key"
