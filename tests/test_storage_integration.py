"""Storage integration tests using real Redis (optional) and SQLite SQLAlchemy store.

Redis tests are skipped when REDIS_URL is not set or Redis is unreachable.
SQLAlchemy tests use in-memory SQLite — no external services required.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio

from fastapi_tenancy.core.exceptions import TenantNotFoundError
from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.storage.memory import InMemoryTenantStore
from fastapi_tenancy.storage.tenant_store import TenantStore


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SQLITE_URL = "sqlite+aiosqlite:///:memory:"


def _t(identifier: str | None = None) -> Tenant:
    uid = uuid.uuid4().hex[:12]
    ident = identifier or f"t-{uid}"
    return Tenant(
        id=f"id-{uid}", identifier=ident, name=ident.replace("-", " ").title(),
        status=TenantStatus.ACTIVE, metadata={"plan": "basic"},
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )


async def _redis_reachable() -> bool:
    try:
        from redis import asyncio as aioredis  # noqa: PLC0415
        r = aioredis.from_url(REDIS_URL, socket_connect_timeout=1)
        await r.ping()
        await r.aclose()
        return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════
# SQLAlchemyTenantStore (SQLite in-memory — always available)
# ══════════════════════════════════════════════════════════════════

class TestSQLAlchemyTenantStore:
    @pytest_asyncio.fixture
    async def store(self):
        from fastapi_tenancy.storage.database import SQLAlchemyTenantStore
        s = SQLAlchemyTenantStore(SQLITE_URL)
        await s.initialize()
        yield s
        await s.close()

    async def test_create_and_get_by_id(self, store):
        t = _t()
        created = await store.create(t)
        fetched = await store.get_by_id(created.id)
        assert fetched.id == created.id

    async def test_create_and_get_by_identifier(self, store):
        t = _t()
        created = await store.create(t)
        fetched = await store.get_by_identifier(created.identifier)
        assert fetched.identifier == created.identifier

    async def test_get_by_id_not_found_raises(self, store):
        with pytest.raises(TenantNotFoundError):
            await store.get_by_id("nonexistent-id")

    async def test_get_by_identifier_not_found_raises(self, store):
        with pytest.raises(TenantNotFoundError):
            await store.get_by_identifier("no-such-tenant")

    async def test_list_returns_all(self, store):
        t1 = await store.create(_t())
        t2 = await store.create(_t())
        tenants = await store.list()
        ids = {t.id for t in tenants}
        assert t1.id in ids and t2.id in ids

    async def test_list_with_status_filter(self, store):
        t = await store.create(_t())
        await store.set_status(t.id, TenantStatus.SUSPENDED)
        active = await store.list(status=TenantStatus.ACTIVE)
        suspended = await store.list(status=TenantStatus.SUSPENDED)
        assert not any(x.id == t.id for x in active)
        assert any(x.id == t.id for x in suspended)

    async def test_count(self, store):
        before = await store.count()
        await store.create(_t())
        await store.create(_t())
        assert await store.count() == before + 2

    async def test_exists_true(self, store):
        t = await store.create(_t())
        assert await store.exists(t.id) is True

    async def test_exists_false(self, store):
        assert await store.exists("ghost-id") is False

    async def test_update(self, store):
        t = await store.create(_t())
        updated = t.model_copy(update={"name": "Updated Name"})
        result = await store.update(updated)
        assert result.name == "Updated Name"

    async def test_update_not_found_raises(self, store):
        t = _t()
        with pytest.raises(TenantNotFoundError):
            await store.update(t)

    async def test_delete(self, store):
        t = await store.create(_t())
        await store.delete(t.id)
        with pytest.raises(TenantNotFoundError):
            await store.get_by_id(t.id)

    async def test_delete_not_found_raises(self, store):
        with pytest.raises(TenantNotFoundError):
            await store.delete("nonexistent")

    async def test_set_status(self, store):
        t = await store.create(_t())
        updated = await store.set_status(t.id, TenantStatus.SUSPENDED)
        assert updated.status == TenantStatus.SUSPENDED

    async def test_update_metadata(self, store):
        t = await store.create(_t())
        result = await store.update_metadata(t.id, {"plan": "enterprise", "seats": 100})
        assert result.metadata.get("plan") == "enterprise"
        assert result.metadata.get("seats") == 100

    async def test_search_by_identifier(self, store):
        uid = uuid.uuid4().hex[:8]
        t = await store.create(_t(identifier=f"search-{uid}"))
        results = await store.search(uid[:6])
        assert any(r.id == t.id for r in results)

    async def test_get_by_ids_batch(self, store):
        t1 = await store.create(_t())
        t2 = await store.create(_t())
        results = await store.get_by_ids([t1.id, t2.id, "missing"])
        found_ids = {r.id for r in results}
        assert t1.id in found_ids and t2.id in found_ids

    async def test_bulk_update_status(self, store):
        t1 = await store.create(_t())
        t2 = await store.create(_t())
        updated = await store.bulk_update_status([t1.id, t2.id], TenantStatus.SUSPENDED)
        assert all(t.status == TenantStatus.SUSPENDED for t in updated)

    async def test_duplicate_id_raises(self, store):
        t = await store.create(_t())
        dup = t.model_copy(update={"identifier": f"new-{uuid.uuid4().hex[:8]}"})
        with pytest.raises(Exception):  # ValueError or TenancyError
            await store.create(dup)

    async def test_duplicate_identifier_raises(self, store):
        t = await store.create(_t())
        dup = t.model_copy(update={"id": f"new-{uuid.uuid4().hex[:8]}"})
        with pytest.raises(Exception):
            await store.create(dup)

    async def test_warm_cache_is_no_op_for_sqlite(self, store):
        # SQLAlchemy store has warm_cache but it may be a no-op
        if hasattr(store, "warm_cache"):
            await store.warm_cache()


# ══════════════════════════════════════════════════════════════════
# TenantStore base class fallbacks (N+1 implementations)
# ══════════════════════════════════════════════════════════════════

class TestTenantStoreBaseFallbacks:
    @pytest_asyncio.fixture
    async def store(self):
        # InMemoryTenantStore uses the base fallbacks for batch ops
        return InMemoryTenantStore()

    async def test_get_by_ids_n_plus_1(self, store):
        t1 = await store.create(_t())
        t2 = await store.create(_t())
        results = await store.get_by_ids([t1.id, t2.id, "missing"])
        found = {r.id for r in results}
        assert t1.id in found and t2.id in found

    async def test_get_by_ids_empty(self, store):
        results = await store.get_by_ids([])
        assert list(results) == []

    async def test_bulk_update_status_n_plus_1(self, store):
        t1 = await store.create(_t())
        t2 = await store.create(_t())
        updated = await store.bulk_update_status([t1.id, t2.id], TenantStatus.SUSPENDED)
        assert all(t.status == TenantStatus.SUSPENDED for t in updated)

    async def test_bulk_update_status_skips_missing(self, store):
        t = await store.create(_t())
        updated = await store.bulk_update_status([t.id, "missing-id"], TenantStatus.SUSPENDED)
        assert len(updated) == 1

    async def test_search_in_memory_filter(self, store):
        t = await store.create(_t(identifier="searchable-slug"))
        results = await store.search("searchable")
        assert any(r.id == t.id for r in results)

    async def test_close_noop(self, store):
        await store.close()  # base no-op — should not raise


# ══════════════════════════════════════════════════════════════════
# RedisTenantStore — requires Redis
# ══════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestRedisTenantStoreIntegration:
    @pytest_asyncio.fixture(autouse=True)
    async def _skip_no_redis(self):
        if not await _redis_reachable():
            pytest.skip("Redis not reachable — set REDIS_URL or start docker-compose.test.yml")

    @pytest_asyncio.fixture
    async def redis_store(self):
        from fastapi_tenancy.storage.redis import RedisTenantStore
        primary = InMemoryTenantStore()
        prefix = f"test-{uuid.uuid4().hex[:8]}"
        store = RedisTenantStore(REDIS_URL, primary, ttl=30, key_prefix=prefix)
        yield store
        await store.invalidate_all()
        await store.close()

    async def test_create_and_cache(self, redis_store):
        t = _t()
        created = await redis_store.create(t)
        assert created.id == t.id
        # Second fetch should be cache hit
        fetched = await redis_store.get_by_id(created.id)
        assert fetched.id == created.id

    async def test_cache_hit_by_identifier(self, redis_store):
        t = await redis_store.create(_t())
        result = await redis_store.get_by_identifier(t.identifier)
        assert result.identifier == t.identifier

    async def test_cache_miss_populates_cache(self, redis_store):
        # Create in primary store directly (bypassing cache)
        t = _t()
        await redis_store._primary.create(t)
        # First fetch → miss → populates
        fetched = await redis_store.get_by_id(t.id)
        assert fetched.id == t.id
        # Second fetch → hit
        fetched2 = await redis_store.get_by_id(t.id)
        assert fetched2.id == t.id

    async def test_update_invalidates_cache(self, redis_store):
        t = await redis_store.create(_t())
        updated = t.model_copy(update={"name": "New Name"})
        result = await redis_store.update(updated)
        assert result.name == "New Name"
        # Should refetch from primary after invalidation
        fetched = await redis_store.get_by_id(t.id)
        assert fetched.name == "New Name"

    async def test_delete_invalidates_cache(self, redis_store):
        t = await redis_store.create(_t())
        await redis_store.delete(t.id)
        with pytest.raises(TenantNotFoundError):
            await redis_store.get_by_id(t.id)

    async def test_set_status_invalidates_and_repopulates(self, redis_store):
        t = await redis_store.create(_t())
        updated = await redis_store.set_status(t.id, TenantStatus.SUSPENDED)
        assert updated.status == TenantStatus.SUSPENDED

    async def test_update_metadata_refreshes_cache(self, redis_store):
        t = await redis_store.create(_t())
        result = await redis_store.update_metadata(t.id, {"plan": "premium"})
        assert result.metadata.get("plan") == "premium"

    async def test_corrupt_cache_entry_treated_as_miss(self, redis_store):
        t = await redis_store.create(_t())
        # Inject corrupt data directly into Redis
        key = redis_store._id_key(t.id)
        await redis_store._redis.setex(key, 30, b"not-json{{{")
        # Should handle gracefully by falling back to primary
        result = await redis_store.get_by_id(t.id)
        assert result.id == t.id

    async def test_get_by_ids_batch_with_pipeline(self, redis_store):
        t1 = await redis_store.create(_t())
        t2 = await redis_store.create(_t())
        results = await redis_store.get_by_ids([t1.id, t2.id, "missing"])
        found_ids = {r.id for r in results}
        assert t1.id in found_ids and t2.id in found_ids

    async def test_get_by_ids_empty(self, redis_store):
        assert list(await redis_store.get_by_ids([])) == []

    async def test_bulk_update_status(self, redis_store):
        t1 = await redis_store.create(_t())
        t2 = await redis_store.create(_t())
        updated = await redis_store.bulk_update_status([t1.id, t2.id], TenantStatus.SUSPENDED)
        assert all(t.status == TenantStatus.SUSPENDED for t in updated)

    async def test_invalidate_all_clears_keys(self, redis_store):
        await redis_store.create(_t())
        await redis_store.create(_t())
        deleted = await redis_store.invalidate_all()
        assert deleted > 0

    async def test_cache_stats(self, redis_store):
        await redis_store.create(_t())
        stats = await redis_store.cache_stats()
        assert "total_keys" in stats
        assert stats["total_keys"] >= 0

    async def test_get_old_tenant_from_cache(self, redis_store):
        t = await redis_store.create(_t())
        old = await redis_store._get_old_tenant(t.id)
        assert old is not None
        assert old.id == t.id

    async def test_get_old_tenant_miss_returns_none(self, redis_store):
        result = await redis_store._get_old_tenant("nonexistent-id")
        assert result is None

    async def test_get_old_tenant_corrupt_returns_none(self, redis_store):
        await redis_store._redis.setex(redis_store._id_key("bad-id"), 30, b"corrupt")
        result = await redis_store._get_old_tenant("bad-id")
        assert result is None

    async def test_list_delegates_to_primary(self, redis_store):
        t = await redis_store.create(_t())
        results = await redis_store.list()
        assert any(r.id == t.id for r in results)

    async def test_count_delegates_to_primary(self, redis_store):
        before = await redis_store.count()
        await redis_store.create(_t())
        assert await redis_store.count() == before + 1

    async def test_exists_checks_cache_then_primary(self, redis_store):
        t = await redis_store.create(_t())
        assert await redis_store.exists(t.id) is True
        assert await redis_store.exists("ghost") is False

    async def test_initialize_delegates_to_primary(self, redis_store):
        # primary is InMemoryTenantStore which has initialize if present
        await redis_store.initialize()

    async def test_get_by_ids_with_cache_mix(self, redis_store):
        # t1 in cache, t2 not in cache
        t1 = await redis_store.create(_t())  # cached
        t2 = _t()
        await redis_store._primary.create(t2)  # only in primary
        results = await redis_store.get_by_ids([t1.id, t2.id])
        found_ids = {r.id for r in results}
        assert t1.id in found_ids and t2.id in found_ids