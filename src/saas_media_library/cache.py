"""Per-asset cache of delivery links resolved with a service token.

Delivery links are bound to the file, not to the user who asked for it
(docs/asset-access/retirement.md), so one cached entry serves every user the
app lets open the asset. They are bound to the tenant, though: entries are
kept per tenant.

Entries are not kept per principal, and a hit makes no request, so the media
library never checks who reads it. Fill and read a cache only through clients
using a tenant's service token; a user-token client reading it would get the
service token's results, RESTRICTED links included.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple
from uuid import UUID

from saas_media_library.models import (
    AssetStatus,
    DeliveryAsset,
    DeliveryBatchResolveResult,
    ResolveStatus,
)

# (tenant id, asset id)
_Key = Tuple[Optional[str], UUID]


class _Entry(NamedTuple):
    result: DeliveryBatchResolveResult
    stored_at: float
    expires_at: float


class AssetUrlCache:
    """Thread-safe cache of ``ok`` delivery resolve results, keyed by tenant
    and asset id.

    An entry lives until its shortest link expires, minus ``safety_margin``
    seconds (request latency, clock skew, and time for the browser to start
    the download). "Its links" are the download URL, the HLS/DASH stream URLs
    (``stream_url_expires_in``) and every rendition and variant preview URL. Entries without any expiry (CDN links) live for
    ``default_ttl`` seconds. A result whose links expire within the margin is
    not cached.

    Only ``ok`` results for ``ready`` assets are cached: ``not_found``,
    ``forbidden`` and ``error``, and assets still being processed (whose
    renditions are not there yet), are always asked again.

    ``tenant_id`` scopes every lookup and update: a result stored for one
    tenant is never returned for another, since the media library resolves
    ids per tenant. ``AccessGate`` and ``client.delivery.resolve(ids,
    cache=...)`` pass their client's tenant, so one cache can be shared by
    clients of several tenants, as long as every one of them uses its
    tenant's service token (entries are not kept per principal). Calls
    without ``tenant_id`` use a namespace of
    their own, except ``invalidate``, which then drops the asset for every
    tenant.

    ``get`` returns the result with every ``*ExpiresIn`` value reduced by the
    entry's age, so callers (and their frontends) see how long the links
    really have left. Treat returned results as read-only: they may be shared
    between callers.

    ``max_entries`` bounds the cache; the least recently used entry is evicted
    first. Expired entries are also dropped as new ones are stored, so an
    unbounded cache grows with the live entries only. ``clock`` (seconds,
    monotonic) is injectable for tests.
    """

    DEFAULT_SAFETY_MARGIN = 30.0
    DEFAULT_TTL = 300.0
    # Expired entries are swept when the cache has grown to twice its size
    # after the previous sweep (at least this many entries): amortized O(1)
    # per stored entry.
    _MIN_SWEEP_SIZE = 64

    def __init__(
        self,
        *,
        safety_margin: float = DEFAULT_SAFETY_MARGIN,
        default_ttl: float = DEFAULT_TTL,
        max_entries: Optional[int] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        if safety_margin < 0:
            raise ValueError("safety_margin cannot be negative")
        if default_ttl < 0:
            raise ValueError("default_ttl cannot be negative")
        if max_entries is not None and max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self.safety_margin = safety_margin
        self.default_ttl = default_ttl
        self.max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[_Key, _Entry] = OrderedDict()
        # asset id -> tenants with an entry for it, for ``invalidate``.
        self._tenants_by_asset: Dict[UUID, Set[Optional[str]]] = {}
        self._sweep_at = self._MIN_SWEEP_SIZE
        self._lock = threading.Lock()

    # ── Lookups ────────────────────────────────────────────────────────

    def get(
        self,
        asset_id: UUID | str,
        *,
        tenant_id: Optional[str] = None,
    ) -> Optional[DeliveryBatchResolveResult]:
        """The cached result for ``asset_id``, or None if missing or expired."""
        return self.get_many([asset_id], tenant_id=tenant_id).get(_as_uuid(asset_id))

    def get_many(
        self,
        asset_ids: Iterable[UUID | str],
        *,
        tenant_id: Optional[str] = None,
    ) -> Dict[UUID, DeliveryBatchResolveResult]:
        """Cached results for the ids that have a live entry (misses are left out)."""
        tenant = _tenant(tenant_id)
        ids = [_as_uuid(a) for a in asset_ids]
        found: Dict[UUID, _Entry] = {}
        with self._lock:
            now = self._clock()
            for asset_id in ids:
                key = (tenant, asset_id)
                entry = self._entries.get(key)
                if entry is None:
                    continue
                if entry.expires_at <= now:
                    self._drop(key)
                    continue
                self._entries.move_to_end(key)
                found[asset_id] = entry
        return {
            asset_id: _with_remaining_expiry(entry.result, now - entry.stored_at)
            for asset_id, entry in found.items()
        }

    # ── Updates ────────────────────────────────────────────────────────

    def put(
        self,
        result: DeliveryBatchResolveResult,
        *,
        tenant_id: Optional[str] = None,
    ) -> bool:
        """Cache one result; returns whether it was cached.

        A result that cannot be cached (not ``ok``, asset not ``ready``, or
        links about to expire) drops any older entry for the same asset
        instead.
        """
        return self._put_all([result], tenant_id)[0]

    def put_many(
        self,
        results: Iterable[DeliveryBatchResolveResult],
        *,
        tenant_id: Optional[str] = None,
    ) -> None:
        """Cache each result, as ``put``."""
        self._put_all(results, tenant_id)

    def invalidate(self, asset_id: UUID | str, *, tenant_id: Optional[str] = None) -> None:
        """Drop the entries for ``asset_id`` (e.g. after changing its
        visibility): only ``tenant_id``'s when given, else every tenant's."""
        asset_id = _as_uuid(asset_id)
        with self._lock:
            if tenant_id is not None:
                self._drop((_tenant(tenant_id), asset_id))
                return
            for tenant in list(self._tenants_by_asset.get(asset_id, ())):
                self._drop((tenant, asset_id))

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._tenants_by_asset.clear()
            self._sweep_at = self._MIN_SWEEP_SIZE

    def __len__(self) -> int:
        """Number of live entries, over all tenants (expired ones are dropped first)."""
        with self._lock:
            self._drop_expired(self._clock())
            return len(self._entries)

    # ── TTL ────────────────────────────────────────────────────────────

    def ttl_for(self, result: DeliveryBatchResolveResult) -> Optional[float]:
        """Seconds ``result`` may be cached, or None if it must not be."""
        if result.status != ResolveStatus.OK or result.asset is None:
            return None
        if result.asset.status != AssetStatus.READY:
            # Still processing (or failed): renditions may appear at any time.
            return None
        expiries = _link_expiries(result.asset)
        if not expiries:
            return self.default_ttl or None
        ttl = min(expiries) - self.safety_margin
        return ttl if ttl > 0 else None

    # ── Private helpers ────────────────────────────────────────────────

    def _put_all(
        self,
        results: Iterable[DeliveryBatchResolveResult],
        tenant_id: Optional[str],
    ) -> List[bool]:
        tenant = _tenant(tenant_id)
        prepared = [((tenant, r.asset_id), r, self.ttl_for(r)) for r in results]
        cached: List[bool] = []
        with self._lock:
            now = self._clock()
            for key, result, ttl in prepared:
                if ttl is None:
                    self._drop(key)
                    cached.append(False)
                    continue
                self._entries[key] = _Entry(result, now, now + ttl)
                self._entries.move_to_end(key)
                self._tenants_by_asset.setdefault(key[1], set()).add(key[0])
                cached.append(True)
            if len(self._entries) >= self._sweep_at:
                self._drop_expired(now)
                self._sweep_at = max(2 * len(self._entries), self._MIN_SWEEP_SIZE)
            if self.max_entries is not None:
                while len(self._entries) > self.max_entries:
                    self._drop(next(iter(self._entries)))
        return cached

    def _drop(self, key: _Key) -> None:
        """Remove one entry (lock held)."""
        if self._entries.pop(key, None) is None:
            return
        tenant, asset_id = key
        tenants = self._tenants_by_asset.get(asset_id)
        if tenants is not None:
            tenants.discard(tenant)
            if not tenants:
                del self._tenants_by_asset[asset_id]

    def _drop_expired(self, now: float) -> None:
        """Remove every expired entry (lock held)."""
        for key in [k for k, e in self._entries.items() if e.expires_at <= now]:
            self._drop(key)


def _as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _tenant(tenant_id: Optional[str]) -> Optional[str]:
    return None if tenant_id is None else str(tenant_id)


def _link_expiries(asset: DeliveryAsset) -> List[int]:
    """Every positive ``*_expires_in`` of an asset's links (0 or None: no expiry)."""
    values = [asset.download_url_expires_in, asset.stream_url_expires_in]
    for rendition in asset.renditions:
        values.append(rendition.preview_url_expires_in)
        values.extend(v.preview_url_expires_in for v in rendition.variants)
    return [v for v in values if v is not None and v > 0]


def _with_remaining_expiry(
    result: DeliveryBatchResolveResult,
    age_seconds: float,
) -> DeliveryBatchResolveResult:
    """Copy of ``result`` with every link expiry reduced by ``age_seconds``.

    Rounded up, so the reported time left is never longer than the real one.
    """
    elapsed = math.ceil(age_seconds)
    asset = result.asset
    if elapsed <= 0 or asset is None:
        return result

    def left(expires_in: Optional[int]) -> Optional[int]:
        return expires_in - elapsed if expires_in else expires_in

    renditions = [
        r.model_copy(update={
            "preview_url_expires_in": left(r.preview_url_expires_in),
            "variants": [
                v.model_copy(update={"preview_url_expires_in": left(v.preview_url_expires_in)})
                for v in r.variants
            ],
        })
        for r in asset.renditions
    ]
    asset = asset.model_copy(update={
        "download_url_expires_in": left(asset.download_url_expires_in),
        "stream_url_expires_in": left(asset.stream_url_expires_in),
        "renditions": renditions,
    })
    return result.model_copy(update={"asset": asset})
