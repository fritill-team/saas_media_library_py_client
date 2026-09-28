"""Per-asset cache of delivery links resolved with a service token.

Delivery links are bound to the file, not to the user who asked for it
(docs/asset-access/retirement.md), so one cached entry serves every user the
app lets open the asset.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional
from uuid import UUID

from saas_media_library.models import (
    DeliveryAsset,
    DeliveryBatchResolveResult,
    ResolveStatus,
)


class _Entry(NamedTuple):
    result: DeliveryBatchResolveResult
    stored_at: float
    expires_at: float


class AssetUrlCache:
    """Thread-safe cache of ``ok`` delivery resolve results, keyed by asset id.

    An entry lives until its shortest link expires, minus ``safety_margin``
    seconds (request latency, clock skew, and time for the browser to start
    the download). "Its links" are the download URL and every rendition and
    variant preview URL. Entries without any expiry (CDN links) live for
    ``default_ttl`` seconds. A result whose links expire within the margin is
    not cached.

    Only ``ok`` results are cached: ``not_found``, ``forbidden`` and ``error``
    are always asked again.

    ``get`` returns the result with every ``*ExpiresIn`` value reduced by the
    entry's age, so callers (and their frontends) see how long the links
    really have left. Treat returned results as read-only: they may be shared
    between callers.

    ``max_entries`` bounds the cache; the least recently used entry is evicted
    first. ``clock`` (seconds, monotonic) is injectable for tests.
    """

    DEFAULT_SAFETY_MARGIN = 30.0
    DEFAULT_TTL = 300.0

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
        self._entries: OrderedDict[UUID, _Entry] = OrderedDict()
        self._lock = threading.Lock()

    # ── Lookups ────────────────────────────────────────────────────────

    def get(self, asset_id: UUID | str) -> Optional[DeliveryBatchResolveResult]:
        """The cached result for ``asset_id``, or None if missing or expired."""
        return self.get_many([asset_id]).get(_as_uuid(asset_id))

    def get_many(self, asset_ids: Iterable[UUID | str]) -> Dict[UUID, DeliveryBatchResolveResult]:
        """Cached results for the ids that have a live entry (misses are left out)."""
        ids = [_as_uuid(a) for a in asset_ids]
        found: Dict[UUID, _Entry] = {}
        with self._lock:
            now = self._clock()
            for asset_id in ids:
                entry = self._entries.get(asset_id)
                if entry is None:
                    continue
                if entry.expires_at <= now:
                    del self._entries[asset_id]
                    continue
                self._entries.move_to_end(asset_id)
                found[asset_id] = entry
        return {
            asset_id: _with_remaining_expiry(entry.result, now - entry.stored_at)
            for asset_id, entry in found.items()
        }

    # ── Updates ────────────────────────────────────────────────────────

    def put(self, result: DeliveryBatchResolveResult) -> bool:
        """Cache one result; returns whether it was cached.

        A result that cannot be cached (not ``ok``, or links about to expire)
        drops any older entry for the same asset instead.
        """
        return self._put_all([result])[0]

    def put_many(self, results: Iterable[DeliveryBatchResolveResult]) -> None:
        """Cache each result, as ``put``."""
        self._put_all(results)

    def invalidate(self, asset_id: UUID | str) -> None:
        """Drop the entry for ``asset_id`` (e.g. after changing its visibility)."""
        with self._lock:
            self._entries.pop(_as_uuid(asset_id), None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        """Number of live entries (expired ones are dropped first)."""
        with self._lock:
            now = self._clock()
            for asset_id in [a for a, e in self._entries.items() if e.expires_at <= now]:
                del self._entries[asset_id]
            return len(self._entries)

    # ── TTL ────────────────────────────────────────────────────────────

    def ttl_for(self, result: DeliveryBatchResolveResult) -> Optional[float]:
        """Seconds ``result`` may be cached, or None if it must not be."""
        if result.status != ResolveStatus.OK or result.asset is None:
            return None
        expiries = _link_expiries(result.asset)
        if not expiries:
            return self.default_ttl or None
        ttl = min(expiries) - self.safety_margin
        return ttl if ttl > 0 else None

    # ── Private helpers ────────────────────────────────────────────────

    def _put_all(self, results: Iterable[DeliveryBatchResolveResult]) -> List[bool]:
        prepared = [(r.asset_id, r, self.ttl_for(r)) for r in results]
        cached: List[bool] = []
        with self._lock:
            now = self._clock()
            for asset_id, result, ttl in prepared:
                if ttl is None:
                    self._entries.pop(asset_id, None)
                    cached.append(False)
                    continue
                self._entries[asset_id] = _Entry(result, now, now + ttl)
                self._entries.move_to_end(asset_id)
                cached.append(True)
            if self.max_entries is not None:
                while len(self._entries) > self.max_entries:
                    self._entries.popitem(last=False)
        return cached


def _as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _link_expiries(asset: DeliveryAsset) -> List[int]:
    """Every positive ``*_expires_in`` of an asset's links (0 or None: no expiry)."""
    values = [asset.download_url_expires_in]
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
        "renditions": renditions,
    })
    return result.model_copy(update={"asset": asset})
