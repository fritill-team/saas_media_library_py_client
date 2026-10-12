from __future__ import annotations

import threading
from typing import List, Optional
from uuid import uuid4

import pytest

from saas_media_library import AssetUrlCache, DeliveryBatchResolveResult


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _ok(
    asset_id=None,
    *,
    download_expires: Optional[int] = 3600,
    rendition_expires: Optional[int] = None,
    variant_expires: List[Optional[int]] = (),
    asset_status: str = "ready",
) -> DeliveryBatchResolveResult:
    asset_id = asset_id or uuid4()
    renditions = []
    if rendition_expires is not None or variant_expires:
        renditions.append({
            "id": str(uuid4()),
            "renditionType": "hls",
            "status": "ready",
            "entrypoint": "master.m3u8",
            "previewUrl": "https://media.test/hls/master.m3u8?token=r",
            "previewUrlType": "volume_token",
            "previewUrlExpiresIn": rendition_expires,
            "variants": [
                {"name": f"v{i}", "previewUrl": f"https://media.test/hls/{i}?token=v",
                 "previewUrlExpiresIn": e}
                for i, e in enumerate(variant_expires)
            ],
        })
    return DeliveryBatchResolveResult.model_validate({
        "assetId": str(asset_id),
        "status": "ok",
        "thumbnailUrl": "https://cdn.test/thumb.webp",
        "asset": {
            "id": str(asset_id),
            "kind": "video",
            "status": asset_status,
            "visibility": "restricted",
            "mimeType": "video/mp4",
            "downloadType": "volume_token",
            "downloadUrl": "https://media.test/v/x?token=d",
            "downloadUrlExpiresIn": download_expires,
            "renditions": renditions,
        },
    })


def _cache(clock: FakeClock, **kwargs) -> AssetUrlCache:
    return AssetUrlCache(clock=clock, **kwargs)


# ── TTL ────────────────────────────────────────────────────────────────


def test_ttl_is_shortest_link_expiry_minus_margin() -> None:
    cache = _cache(FakeClock())
    result = _ok(download_expires=3600, rendition_expires=900, variant_expires=[600, 1200])

    assert cache.ttl_for(result) == 600 - 30


def test_entry_expires_at_ttl() -> None:
    clock = FakeClock()
    cache = _cache(clock)
    result = _ok(download_expires=100)  # ttl 70
    assert cache.put(result)

    clock.advance(69.5)
    assert cache.get(result.asset_id) is not None
    clock.advance(0.5)
    assert cache.get(result.asset_id) is None
    assert len(cache) == 0


def test_rendition_expiry_bounds_the_entry() -> None:
    clock = FakeClock()
    cache = _cache(clock)
    result = _ok(download_expires=3600, variant_expires=[120])
    cache.put(result)

    clock.advance(91)
    assert cache.get(result.asset_id) is None


def test_safety_margin_is_configurable() -> None:
    clock = FakeClock()
    cache = _cache(clock, safety_margin=5)
    result = _ok(download_expires=100)
    cache.put(result)

    assert cache.ttl_for(result) == 95
    clock.advance(94)
    assert cache.get(result.asset_id) is not None
    clock.advance(1)
    assert cache.get(result.asset_id) is None


@pytest.mark.parametrize("expires", [None, 0])
def test_links_without_expiry_use_default_ttl(expires) -> None:
    clock = FakeClock()
    cache = _cache(clock, default_ttl=60)
    result = _ok(download_expires=expires, rendition_expires=expires, variant_expires=[expires])

    assert cache.ttl_for(result) == 60  # no margin on the default
    cache.put(result)
    clock.advance(59)
    assert cache.get(result.asset_id) is not None
    clock.advance(1)
    assert cache.get(result.asset_id) is None


def test_default_ttl_defaults_to_five_minutes() -> None:
    assert AssetUrlCache().ttl_for(_ok(download_expires=0)) == 300


def test_zero_default_ttl_does_not_cache_links_without_expiry() -> None:
    cache = _cache(FakeClock(), default_ttl=0)
    assert not cache.put(_ok(download_expires=None))
    assert len(cache) == 0


def test_link_expiring_within_margin_is_not_cached() -> None:
    cache = _cache(FakeClock())
    result = _ok(download_expires=30)

    assert cache.ttl_for(result) is None
    assert cache.put(result) is False
    assert cache.get(result.asset_id) is None


@pytest.mark.parametrize("status", ["not_found", "forbidden", "error", "something_new"])
def test_only_ok_results_are_cached(status) -> None:
    cache = _cache(FakeClock())
    result = DeliveryBatchResolveResult.model_validate({
        "assetId": str(uuid4()), "status": status, "thumbnailUrl": "https://cdn.test/t.webp",
    })

    assert cache.put(result) is False
    assert cache.get(result.asset_id) is None


def test_ok_without_asset_is_not_cached() -> None:
    cache = _cache(FakeClock())
    result = DeliveryBatchResolveResult.model_validate({"assetId": str(uuid4()), "status": "ok"})

    assert cache.put(result) is False


@pytest.mark.parametrize(
    "asset_status", ["pending", "uploaded", "processing", "awaiting_input", "failed"]
)
def test_assets_that_are_not_ready_are_not_cached(asset_status) -> None:
    # Renditions (HLS, thumbnails...) may still appear: always ask again.
    cache = _cache(FakeClock())
    result = _ok(asset_status=asset_status)

    assert cache.ttl_for(result) is None
    assert cache.put(result) is False
    assert cache.get(result.asset_id) is None


def test_not_ready_result_drops_older_entry() -> None:
    cache = _cache(FakeClock())
    asset_id = uuid4()
    cache.put(_ok(asset_id))

    cache.put(_ok(asset_id, asset_status="processing"))

    assert cache.get(asset_id) is None


def test_uncacheable_result_drops_older_entry() -> None:
    cache = _cache(FakeClock())
    result = _ok()
    cache.put(result)

    cache.put(DeliveryBatchResolveResult(asset_id=result.asset_id, status="not_found"))

    assert cache.get(result.asset_id) is None


def test_put_replaces_entry_and_restarts_ttl() -> None:
    clock = FakeClock()
    cache = _cache(clock)
    asset_id = uuid4()
    cache.put(_ok(asset_id, download_expires=100))
    clock.advance(60)
    fresh = _ok(asset_id, download_expires=100)
    cache.put(fresh)

    clock.advance(60)
    assert cache.get(asset_id) is not None


# ── Returned expiries ──────────────────────────────────────────────────


def test_get_reports_remaining_link_lifetime() -> None:
    clock = FakeClock()
    cache = _cache(clock)
    result = _ok(download_expires=3600, rendition_expires=900, variant_expires=[600, None])
    cache.put(result)

    clock.advance(99.2)  # rounded up to 100
    hit = cache.get(result.asset_id)

    assert hit.asset.download_url_expires_in == 3500
    assert hit.asset.renditions[0].preview_url_expires_in == 800
    assert [v.preview_url_expires_in for v in hit.asset.renditions[0].variants] == [500, None]
    assert hit.asset.download_url == result.asset.download_url
    assert hit.thumbnail_url == result.thumbnail_url
    # The stored result is left untouched.
    assert result.asset.download_url_expires_in == 3600
    assert result.asset.renditions[0].variants[0].preview_url_expires_in == 600


def test_get_without_elapsed_time_returns_result_unchanged() -> None:
    cache = _cache(FakeClock())
    result = _ok()
    cache.put(result)

    assert cache.get(result.asset_id) == result


def test_links_without_expiry_stay_without_expiry() -> None:
    clock = FakeClock()
    cache = _cache(clock)
    result = _ok(download_expires=0)
    cache.put(result)
    clock.advance(10)

    assert cache.get(result.asset_id).asset.download_url_expires_in == 0


# ── Lookups / eviction ─────────────────────────────────────────────────


def test_get_many_returns_hits_only_and_accepts_strings() -> None:
    cache = _cache(FakeClock())
    a, b = _ok(), _ok()
    cache.put_many([a, b])
    missing = uuid4()

    found = cache.get_many([str(a.asset_id), missing, b.asset_id])

    assert set(found) == {a.asset_id, b.asset_id}
    assert cache.get(str(a.asset_id)) == a


def test_lru_eviction() -> None:
    cache = _cache(FakeClock(), max_entries=2)
    a, b, c = _ok(), _ok(), _ok()
    cache.put(a)
    cache.put(b)
    assert cache.get(a.asset_id) is not None  # a is now most recently used

    cache.put(c)

    assert cache.get(b.asset_id) is None
    assert cache.get(a.asset_id) is not None
    assert cache.get(c.asset_id) is not None
    assert len(cache) == 2


def test_put_many_respects_max_entries() -> None:
    cache = _cache(FakeClock(), max_entries=3)
    results = [_ok() for _ in range(5)]

    cache.put_many(results)

    assert len(cache) == 3
    assert set(cache.get_many(r.asset_id for r in results)) == {r.asset_id for r in results[2:]}


def test_invalidate_and_clear() -> None:
    cache = _cache(FakeClock())
    a, b = _ok(), _ok()
    cache.put_many([a, b])

    cache.invalidate(str(a.asset_id))
    cache.invalidate(uuid4())  # unknown id is fine
    assert cache.get(a.asset_id) is None
    assert cache.get(b.asset_id) is not None

    cache.clear()
    assert len(cache) == 0


def test_len_drops_expired_entries() -> None:
    clock = FakeClock()
    cache = _cache(clock)
    cache.put(_ok(download_expires=100))
    cache.put(_ok(download_expires=1000))

    clock.advance(80)

    assert len(cache) == 1


def test_expired_entries_are_purged_as_new_ones_are_stored() -> None:
    # Without max_entries, ids that are never looked up again must not pile up.
    clock = FakeClock()
    cache = _cache(clock)
    for _ in range(20):
        cache.put_many([_ok(download_expires=100) for _ in range(50)])
        clock.advance(100)
    for _ in range(50):
        cache.put(_ok(download_expires=100))

    # Internal: only the live entries plus at most one sweep interval remain.
    assert len(cache._entries) <= 2 * max(50, AssetUrlCache._MIN_SWEEP_SIZE)
    assert set(cache._tenants_by_asset) == {asset_id for _, asset_id in cache._entries}
    assert len(cache) == 50


def test_purge_keeps_live_entries() -> None:
    clock = FakeClock()
    cache = _cache(clock)
    live = _ok(download_expires=10_000)
    cache.put(live, tenant_id="t1")
    cache.put_many([_ok(download_expires=100) for _ in range(200)])
    clock.advance(100)

    cache.put_many([_ok(download_expires=100) for _ in range(250)])  # triggers a sweep

    assert len(cache._entries) == 251  # internal: the 200 expired entries are gone
    assert cache.get(live.asset_id, tenant_id="t1") is not None


# ── Tenants ────────────────────────────────────────────────────────────


def test_entries_are_scoped_by_tenant() -> None:
    cache = _cache(FakeClock())
    result = _ok()

    assert cache.put(result, tenant_id="tenant-b")

    assert cache.get(result.asset_id, tenant_id="tenant-b") == result
    assert cache.get(result.asset_id, tenant_id="tenant-a") is None
    assert cache.get(result.asset_id) is None
    assert cache.get_many([result.asset_id], tenant_id="tenant-a") == {}


def test_same_asset_id_is_kept_per_tenant() -> None:
    cache = _cache(FakeClock())
    asset_id = uuid4()
    a, b = _ok(asset_id, download_expires=1000), _ok(asset_id, download_expires=2000)

    cache.put(a, tenant_id="tenant-a")
    cache.put(b, tenant_id="tenant-b")

    assert cache.get(asset_id, tenant_id="tenant-a") == a
    assert cache.get(asset_id, tenant_id="tenant-b") == b
    assert len(cache) == 2


def test_invalidate_without_tenant_drops_every_tenants_entry() -> None:
    cache = _cache(FakeClock())
    asset_id, other = uuid4(), _ok()
    for tenant in ("tenant-a", "tenant-b", None):
        cache.put(_ok(asset_id), tenant_id=tenant)
    cache.put(other, tenant_id="tenant-a")

    cache.invalidate(str(asset_id))

    for tenant in ("tenant-a", "tenant-b", None):
        assert cache.get(asset_id, tenant_id=tenant) is None
    assert cache.get(other.asset_id, tenant_id="tenant-a") is not None
    assert len(cache) == 1


def test_invalidate_with_tenant_drops_only_that_tenants_entry() -> None:
    cache = _cache(FakeClock())
    asset_id = uuid4()
    cache.put(_ok(asset_id), tenant_id="tenant-a")
    cache.put(_ok(asset_id), tenant_id="tenant-b")

    cache.invalidate(asset_id, tenant_id="tenant-a")

    assert cache.get(asset_id, tenant_id="tenant-a") is None
    assert cache.get(asset_id, tenant_id="tenant-b") is not None


def test_lru_eviction_counts_entries_of_every_tenant() -> None:
    cache = _cache(FakeClock(), max_entries=2)
    asset_id = uuid4()
    cache.put(_ok(asset_id), tenant_id="tenant-a")
    cache.put(_ok(asset_id), tenant_id="tenant-b")
    cache.put(_ok(), tenant_id="tenant-a")

    assert cache.get(asset_id, tenant_id="tenant-a") is None
    assert cache.get(asset_id, tenant_id="tenant-b") is not None
    cache.invalidate(asset_id)
    assert len(cache) == 1


@pytest.mark.parametrize(
    "kwargs",
    [{"safety_margin": -1}, {"default_ttl": -1}, {"max_entries": 0}],
)
def test_invalid_settings_are_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        AssetUrlCache(**kwargs)


def test_thread_safety_smoke() -> None:
    clock = FakeClock()
    cache = _cache(clock, max_entries=50)
    results = [_ok(download_expires=100 + i) for i in range(200)]
    errors: list = []

    def worker(offset: int) -> None:
        try:
            for i in range(1000):
                result = results[(offset * 37 + i) % len(results)]
                if i % 3 == 0:
                    cache.put(result)
                elif i % 3 == 1:
                    cache.get_many([result.asset_id, results[i % 7].asset_id])
                else:
                    hit = cache.get(result.asset_id)
                    assert hit is None or hit.asset_id == result.asset_id
                if i % 250 == 0:
                    cache.invalidate(result.asset_id)
                    len(cache)
        except Exception as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(cache) <= 50
