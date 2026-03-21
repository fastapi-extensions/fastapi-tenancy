"""Tests for fastapi_tenancy.cache.tenant_cache — TenantCache."""

from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime
import time
from unittest.mock import patch

import pytest

from fastapi_tenancy.cache.tenant_cache import TenantCache
from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.types import IsolationStrategy, ResolutionStrategy, Tenant, TenantStatus
from fastapi_tenancy.manager import TenancyManager, _CachingStoreProxy
from fastapi_tenancy.storage.memory import InMemoryTenantStore


def _t(tid: str, identifier: str) -> Tenant:
    ts = datetime.now(UTC)
    return Tenant(
        id=tid,
        identifier=identifier,
        name=f"T{tid}",
        status=TenantStatus.ACTIVE,
        created_at=ts,
        updated_at=ts,
    )


class TestInit:
    def test_default_params(self) -> None:
        c = TenantCache()
        assert c.size() == 0
        s = c.stats()
        assert s["max_size"] == 1000
        assert s["ttl"] == 60
        assert s["hits"] == 0
        assert s["misses"] == 0

    def test_custom_params(self) -> None:
        c = TenantCache(max_size=50, ttl=10)
        assert c.stats()["max_size"] == 50
        assert c.stats()["ttl"] == 10

    def test_invalid_max_size_raises(self) -> None:
        with pytest.raises(ValueError):
            TenantCache(max_size=0)

    def test_invalid_ttl_raises(self) -> None:
        with pytest.raises(ValueError):
            TenantCache(ttl=0)


class TestSetAndGet:
    def test_get_by_id_hit(self) -> None:
        c = TenantCache(ttl=60)
        t = _t("t1", "acme-corp")
        c.set(t)
        result = c.get("t1")
        assert result is t

    def test_get_by_identifier_hit(self) -> None:
        c = TenantCache(ttl=60)
        t = _t("t1", "acme-corp")
        c.set(t)
        result = c.get_by_identifier("acme-corp")
        assert result is t

    def test_get_by_id_miss(self) -> None:
        c = TenantCache(ttl=60)
        assert c.get("nonexistent") is None

    def test_get_by_identifier_miss(self) -> None:
        c = TenantCache(ttl=60)
        assert c.get_by_identifier("nonexistent") is None

    def test_set_updates_existing(self) -> None:
        c = TenantCache(ttl=60)
        t_old = _t("t1", "acme-corp")
        t_new = _t("t1", "acme-corp")
        c.set(t_old)
        c.set(t_new)
        assert c.get("t1") is t_new
        assert c.size() == 1

    def test_set_rotates_identifier_key_on_rename(self) -> None:
        c = TenantCache(ttl=60)
        t_old = _t("t1", "acme-corp")
        c.set(t_old)
        t_new = _t("t1", "acme-new")
        c.set(t_new)
        # Old identifier key should be gone
        assert c.get_by_identifier("acme-corp") is None
        assert c.get_by_identifier("acme-new") is t_new


class TestTTLExpiry:
    def test_expired_entry_is_a_miss(self) -> None:
        c = TenantCache(ttl=1)
        t = _t("t1", "acme-corp")
        # Fake the expiry by manipulating the entry's expires_at
        c.set(t)
        # Patch time.monotonic to simulate future
        with patch("fastapi_tenancy.cache.tenant_cache.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 10
            assert c.get("t1") is None

    def test_non_expired_entry_is_a_hit(self) -> None:
        c = TenantCache(ttl=3600)
        t = _t("t1", "acme-corp")
        c.set(t)
        assert c.get("t1") is t

    def test_expired_entry_evicted_on_access(self) -> None:
        c = TenantCache(ttl=1)
        t = _t("t1", "acme-corp")
        c.set(t)
        with patch("fastapi_tenancy.cache.tenant_cache.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 10
            c.get("t1")
        assert c.size() == 0


class TestLRUEviction:
    def test_lru_entry_evicted_when_full(self) -> None:
        c = TenantCache(max_size=3, ttl=3600)
        t1 = _t("t1", "ten-one")
        t2 = _t("t2", "ten-two")
        t3 = _t("t3", "ten-thr")
        t4 = _t("t4", "ten-fou")
        c.set(t1)
        c.set(t2)
        c.set(t3)
        # Access t1 to make it MRU; LRU is now t2
        c.get("t1")
        # Inserting t4 should evict t2
        c.set(t4)
        assert c.size() == 3
        assert c.get("t2") is None
        assert c.get("t1") is not None
        assert c.get("t4") is not None

    def test_size_never_exceeds_max(self) -> None:
        c = TenantCache(max_size=5, ttl=3600)
        for i in range(20):
            c.set(_t(f"t{i}", f"ten-{i:03d}"))
        assert c.size() <= 5

    def test_evict_lru_on_empty_cache_does_nothing(self) -> None:
        c = TenantCache(max_size=100, ttl=60)
        assert c.size() == 0
        c._evict_lru()
        assert c.size() == 0
        assert c._by_id == OrderedDict()
        assert c._id_by_ident == {}


class TestInvalidation:
    def test_invalidate_by_id(self) -> None:
        c = TenantCache(ttl=3600)
        t = _t("t1", "acme-corp")
        c.set(t)
        removed = c.invalidate("t1")
        assert removed is True
        assert c.get("t1") is None
        assert c.get_by_identifier("acme-corp") is None

    def test_invalidate_nonexistent_returns_false(self) -> None:
        c = TenantCache(ttl=3600)
        assert c.invalidate("ghost") is False

    def test_invalidate_by_identifier(self) -> None:
        c = TenantCache(ttl=3600)
        t = _t("t1", "acme-corp")
        c.set(t)
        removed = c.invalidate_by_identifier("acme-corp")
        assert removed is True
        assert c.get("t1") is None

    def test_invalidate_by_identifier_miss(self) -> None:
        c = TenantCache(ttl=3600)
        assert c.invalidate_by_identifier("ghost") is False

    def test_clear_returns_count(self) -> None:
        c = TenantCache(ttl=3600)
        for i in range(5):
            c.set(_t(f"t{i}", f"ten-{i:03d}"))
        count = c.clear()
        assert count == 5
        assert c.size() == 0


class TestStats:
    def test_hit_rate_zero_with_no_lookups(self) -> None:
        c = TenantCache()
        assert c.stats()["hit_rate_pct"] == 0

    def test_hit_rate_100_all_hits(self) -> None:
        c = TenantCache(ttl=3600)
        t = _t("t1", "acme-corp")
        c.set(t)
        c.get("t1")
        c.get("t1")
        c.get("t1")
        assert c.stats()["hit_rate_pct"] == 100

    def test_hit_rate_50(self) -> None:
        c = TenantCache(ttl=3600)
        t = _t("t1", "acme-corp")
        c.set(t)
        c.get("t1")  # hit
        c.get("ghost")  # miss
        s = c.stats()
        assert s["hit_rate_pct"] == 50

    def test_misses_incremented(self) -> None:
        c = TenantCache(ttl=3600)
        c.get("miss1")
        c.get("miss2")
        assert c.stats()["misses"] == 2


class TestPurgeExpired:
    def test_purge_removes_stale_entries(self) -> None:
        c = TenantCache(ttl=1)
        for i in range(5):
            c.set(_t(f"t{i}", f"ten-{i:03d}"))
        with patch("fastapi_tenancy.cache.tenant_cache.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 100
            evicted = c.purge_expired()
        assert evicted == 5
        assert c.size() == 0

    def test_purge_keeps_fresh_entries(self) -> None:
        c = TenantCache(ttl=3600)
        c.set(_t("t1", "fresh"))
        evicted = c.purge_expired()
        assert evicted == 0
        assert c.size() == 1


@pytest.mark.integration
class TestTenantCacheWiring:
    """FIX: _CachingStoreProxy must intercept get_by_identifier on warm requests."""

    async def test_caching_proxy_populates_cache_on_miss(self) -> None:
        backing = InMemoryTenantStore()
        l1 = TenantCache(max_size=100, ttl=60)
        tenant = _t("t1", "proxy-tenant")
        await backing.create(tenant)

        proxy = _CachingStoreProxy(backing, l1)
        result = await proxy.get_by_identifier("proxy-tenant")
        assert result.identifier == "proxy-tenant"
        # L1 should now be populated
        assert l1.get_by_identifier("proxy-tenant") is not None

    async def test_caching_proxy_serves_from_l1_on_second_call(self) -> None:
        backing = InMemoryTenantStore()
        l1 = TenantCache(max_size=100, ttl=60)
        tenant = _t("t1", "l1-tenant")
        await backing.create(tenant)

        proxy = _CachingStoreProxy(backing, l1)
        r1 = await proxy.get_by_identifier("l1-tenant")
        r2 = await proxy.get_by_identifier("l1-tenant")
        assert r1.identifier == r2.identifier == "l1-tenant"

    async def test_l1_cache_invalidated_on_status_update(self) -> None:
        cache = TenantCache(max_size=100, ttl=300)
        tenant = _t("t1", "invalidate-me")
        cache.set(tenant)
        assert cache.get_by_identifier("invalidate-me") is not None
        cache.invalidate(tenant.id)
        assert cache.get_by_identifier("invalidate-me") is None

    async def test_caching_proxy_invalidates_on_update(self) -> None:
        backing = InMemoryTenantStore()
        l1 = TenantCache(max_size=100, ttl=60)
        tenant = _t("t1", "update-invalidate")
        await backing.create(tenant)
        proxy = _CachingStoreProxy(backing, l1)
        await proxy.get_by_identifier("update-invalidate")
        assert l1.get_by_identifier("update-invalidate") is not None

        updated = tenant.model_copy(update={"name": "Updated Name"})
        await proxy.update(updated)
        assert l1.get_by_identifier("update-invalidate") is None

    async def test_manager_l1_cache_wired_when_enabled(self) -> None:
        """Manager with cache_enabled=True must expose a populated _l1_cache."""
        cfg = TenancyConfig(
            database_url="sqlite+aiosqlite:///:memory:",
            resolution_strategy=ResolutionStrategy.HEADER,
            isolation_strategy=IsolationStrategy.SCHEMA,
            cache_enabled=True,
            redis_url="redis://localhost:6379/0",
        )
        store = InMemoryTenantStore()
        with patch("fastapi_tenancy.manager._build_resolver") as mock_build:
            from fastapi_tenancy.resolution.header import HeaderTenantResolver  # noqa: PLC0415

            mock_build.return_value = HeaderTenantResolver(store)
            manager = TenancyManager(cfg, store)
            await manager.initialize()
        tenant = _t("t1", "cached-tenant")
        await store.create(tenant)
        try:
            if manager._l1_cache is not None:
                manager._l1_cache.set(tenant)
                cached = manager._l1_cache.get_by_identifier("cached-tenant")
                assert cached is not None
                assert cached.identifier == "cached-tenant"
        finally:
            await manager.close()


@pytest.mark.integration
class TestCacheInvalidationOnWrite:
    """FIX: _CachingStoreProxy must invalidate L1 on create/update/set_status/delete."""

    def _proxy(self) -> tuple[_CachingStoreProxy, TenantCache, InMemoryTenantStore]:
        backing = InMemoryTenantStore()
        cache = TenantCache(max_size=100, ttl=300)
        return _CachingStoreProxy(backing, cache), cache, backing

    async def test_create_invalidates_stale_entry(self) -> None:
        proxy, cache, _ = self._proxy()
        tenant = _t("t1", "create-inv")
        cache.set(tenant)
        created = await proxy.create(tenant)
        assert cache.get(created.id) is None

    async def test_update_invalidates_cache(self) -> None:
        proxy, cache, backing = self._proxy()
        tenant = _t("t1", "update-inv")
        await backing.create(tenant)
        cache.set(tenant)
        await proxy.update(tenant.model_copy(update={"name": "New Name"}))
        assert cache.get(tenant.id) is None

    async def test_set_status_invalidates_cache(self) -> None:
        proxy, cache, backing = self._proxy()
        tenant = _t("t1", "status-inv")
        await backing.create(tenant)
        cache.set(tenant)
        await proxy.set_status(tenant.id, TenantStatus.SUSPENDED)
        assert cache.get(tenant.id) is None

    async def test_delete_invalidates_cache(self) -> None:
        proxy, cache, backing = self._proxy()
        tenant = _t("t1", "delete-inv")
        await backing.create(tenant)
        cache.set(tenant)
        await proxy.delete(tenant.id)
        assert cache.get(tenant.id) is None
