"""Unit tests for :class:`~fastapi_tenancy.storage.redis.RedisTenantStore`."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fastapi_tenancy.core.exceptions import TenantNotFoundError
from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.storage.redis import RedisTenantStore, _require_redis

if TYPE_CHECKING:
    from collections.abc import Callable


class TestRequireRedis:
    def test_import_error_message(self) -> None:
        with (
            patch.dict("sys.modules", {"redis": None}),
            pytest.raises(ImportError, match="pip install fastapi-tenancy\\[redis\\]"),
        ):
            _require_redis()


class TestGetById:
    async def test_cache_miss_fetches_from_primary(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await redis_store._primary.create(make_tenant())
        result = await redis_store.get_by_id(t.id)
        assert result.id == t.id

    async def test_cache_hit_served_from_redis(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await redis_store.create(make_tenant())
        # Second call — primary should NOT be invoked a second time
        primary_get = AsyncMock(wraps=redis_store._primary.get_by_id)
        redis_store._primary.get_by_id = primary_get  # type: ignore[method-assign]
        result = await redis_store.get_by_id(t.id)
        assert result.id == t.id
        primary_get.assert_not_called()

    async def test_missing_tenant_raises_not_found(self, redis_store: RedisTenantStore) -> None:
        with pytest.raises(TenantNotFoundError):
            await redis_store.get_by_id("nonexistent")

    async def test_corrupt_cache_entry_treated_as_miss(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
        fake_redis: MagicMock,
    ) -> None:
        t = await redis_store.create(make_tenant())
        # Corrupt the cached entry
        fake_redis._store[redis_store._id_key(t.id)] = b"INVALID_JSON"
        # Should fall back to primary without raising
        result = await redis_store.get_by_id(t.id)
        assert result.id == t.id


class TestGetByIdentifier:
    async def test_cache_miss_fetches_from_primary(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        await redis_store._primary.create(make_tenant(identifier="ident-slug"))
        result = await redis_store.get_by_identifier("ident-slug")
        assert result.identifier == "ident-slug"

    async def test_cache_hit_served_from_redis(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        await redis_store.create(make_tenant(identifier="warm-slug"))
        # After create the slug key is cached; primary must not be called again
        primary_get = AsyncMock(wraps=redis_store._primary.get_by_identifier)
        redis_store._primary.get_by_identifier = primary_get  # type: ignore[method-assign]
        result = await redis_store.get_by_identifier("warm-slug")
        assert result.identifier == "warm-slug"
        primary_get.assert_not_called()

    async def test_missing_raises_not_found(self, redis_store: RedisTenantStore) -> None:
        with pytest.raises(TenantNotFoundError):
            await redis_store.get_by_identifier("ghost-slug")


class TestCreate:
    async def test_creates_in_primary_and_caches(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
        fake_redis: MagicMock,
    ) -> None:
        t = await redis_store.create(make_tenant(identifier="create-me"))
        # Both cache keys must be populated
        assert fake_redis._store.get(redis_store._id_key(t.id)) is not None
        assert fake_redis._store.get(redis_store._slug_key("create-me")) is not None

    async def test_duplicate_raises_value_error(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant()
        await redis_store.create(t)
        with pytest.raises(ValueError):
            await redis_store.create(t)

    async def test_cache_write_failure_does_not_propagate(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
        fake_redis: MagicMock,
    ) -> None:
        # Make pipeline.execute raise
        def _bad_pipeline() -> MagicMock:
            pipe = MagicMock()
            pipe.setex = MagicMock(return_value=pipe)
            pipe.execute = AsyncMock(side_effect=RuntimeError("Redis down"))
            return pipe

        fake_redis.pipeline = MagicMock(side_effect=_bad_pipeline)
        # Must not raise even though cache write fails
        result = await redis_store.create(make_tenant())
        assert result.id is not None


class TestUpdate:
    async def test_update_invalidates_old_and_sets_new(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
        fake_redis: MagicMock,
    ) -> None:
        t = await redis_store.create(make_tenant(identifier="before-slug"))
        await redis_store.update(t.model_copy(update={"identifier": "after-slug"}))
        # Old identifier key must be gone
        assert fake_redis._store.get(redis_store._slug_key("before-slug")) is None
        # New identifier key must be present
        assert fake_redis._store.get(redis_store._slug_key("after-slug")) is not None

    async def test_update_missing_raises_not_found(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        ghost = make_tenant()
        with pytest.raises(TenantNotFoundError):
            await redis_store.update(ghost)

    async def test_cache_cold_update_reads_primary(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
        fake_redis: MagicMock,
    ) -> None:
        """When cache is cold, update() must fall back to primary for old identifier."""
        t = await redis_store._primary.create(make_tenant())
        # Cache is empty — no keys set
        assert len(fake_redis._store) == 0
        result = await redis_store.update(t.model_copy(update={"name": "Updated"}))
        assert result.name == "Updated"


class TestDelete:
    async def test_delete_removes_cache_keys(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
        fake_redis: MagicMock,
    ) -> None:
        t = await redis_store.create(make_tenant(identifier="del-slug"))
        await redis_store.delete(t.id)
        assert fake_redis._store.get(redis_store._id_key(t.id)) is None
        assert fake_redis._store.get(redis_store._slug_key("del-slug")) is None

    async def test_delete_missing_raises_not_found(self, redis_store: RedisTenantStore) -> None:
        with pytest.raises(TenantNotFoundError):
            await redis_store.delete("ghost-id")

    async def test_delete_cold_cache_reads_primary(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
        fake_redis: MagicMock,
    ) -> None:
        t = await redis_store._primary.create(make_tenant(identifier="cold-del"))
        assert len(fake_redis._store) == 0
        await redis_store.delete(t.id)
        with pytest.raises(TenantNotFoundError):
            await redis_store._primary.get_by_id(t.id)


class TestSetStatus:
    async def test_status_updated_in_primary_and_cache_refreshed(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await redis_store.create(make_tenant())
        result = await redis_store.set_status(t.id, TenantStatus.SUSPENDED)
        assert result.status == TenantStatus.SUSPENDED
        # Cache must reflect new status
        cached = await redis_store.get_by_id(t.id)
        assert cached.status == TenantStatus.SUSPENDED

    async def test_missing_raises_not_found(self, redis_store: RedisTenantStore) -> None:
        with pytest.raises(TenantNotFoundError):
            await redis_store.set_status("ghost", TenantStatus.ACTIVE)

    async def test_cold_cache_falls_back_to_primary(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await redis_store._primary.create(make_tenant())
        result = await redis_store.set_status(t.id, TenantStatus.DELETED)
        assert result.status == TenantStatus.DELETED


class TestUpdateMetadata:
    async def test_merge_and_cache_refresh(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await redis_store.create(make_tenant(metadata={"a": 1}))
        result = await redis_store.update_metadata(t.id, {"b": 2})
        assert result.metadata == {"a": 1, "b": 2}

    async def test_missing_raises_not_found(self, redis_store: RedisTenantStore) -> None:
        with pytest.raises(TenantNotFoundError):
            await redis_store.update_metadata("ghost", {"k": "v"})


class TestGetByIds:
    async def test_all_hits_served_from_cache(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t1 = await redis_store.create(make_tenant())
        t2 = await redis_store.create(make_tenant())
        primary_batch = AsyncMock(wraps=redis_store._primary.get_by_ids)
        redis_store._primary.get_by_ids = primary_batch  # type: ignore[method-assign]
        results = await redis_store.get_by_ids([t1.id, t2.id])
        assert {r.id for r in results} == {t1.id, t2.id}
        primary_batch.assert_not_called()

    async def test_misses_delegated_to_primary(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await redis_store._primary.create(make_tenant())
        results = await redis_store.get_by_ids([t.id])
        assert len(results) == 1

    async def test_empty_input_returns_empty(self, redis_store: RedisTenantStore) -> None:
        assert await redis_store.get_by_ids([]) == []

    async def test_all_missing_returns_empty(self, redis_store: RedisTenantStore) -> None:
        assert await redis_store.get_by_ids(["x", "y"]) == []

    async def test_input_order_preserved(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t1 = await redis_store.create(make_tenant())
        t2 = await redis_store.create(make_tenant())
        t3 = await redis_store.create(make_tenant())
        results = await redis_store.get_by_ids([t3.id, t1.id, t2.id])
        assert [r.id for r in results] == [t3.id, t1.id, t2.id]

    async def test_corrupt_pipeline_entry_treated_as_miss(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
        fake_redis: MagicMock,
    ) -> None:
        t = await redis_store.create(make_tenant())
        # Corrupt the id cache key
        fake_redis._store[redis_store._id_key(t.id)] = b"BAD_JSON"
        results = await redis_store.get_by_ids([t.id])
        # Falls back to primary; must still return the tenant
        assert len(results) == 1
        assert results[0].id == t.id


class TestBulkUpdateStatus:
    async def test_updates_primary_and_refreshes_cache(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t1 = await redis_store.create(make_tenant())
        t2 = await redis_store.create(make_tenant())
        result = await redis_store.bulk_update_status([t1.id, t2.id], TenantStatus.SUSPENDED)
        assert len(result) == 2
        assert all(t.status == TenantStatus.SUSPENDED for t in result)

    async def test_empty_input(self, redis_store: RedisTenantStore) -> None:
        assert await redis_store.bulk_update_status([], TenantStatus.ACTIVE) == []


class TestListCount:
    async def test_list_delegates_to_primary(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        await redis_store.create(make_tenant())
        results = await redis_store.list()
        assert len(results) == 1

    async def test_list_with_status_filter(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        await redis_store.create(make_tenant(status=TenantStatus.ACTIVE))
        await redis_store.create(make_tenant(status=TenantStatus.SUSPENDED))
        active = await redis_store.list(status=TenantStatus.ACTIVE)
        assert all(t.status == TenantStatus.ACTIVE for t in active)

    async def test_count_delegates_to_primary(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        await redis_store.create(make_tenant())
        await redis_store.create(make_tenant())
        assert await redis_store.count() == 2

    async def test_count_by_status(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        await redis_store.create(make_tenant(status=TenantStatus.ACTIVE))
        await redis_store.create(make_tenant(status=TenantStatus.SUSPENDED))
        assert await redis_store.count(status=TenantStatus.ACTIVE) == 1


class TestExists:
    async def test_exists_true_from_cache(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await redis_store.create(make_tenant())
        assert await redis_store.exists(t.id) is True

    async def test_exists_falls_back_to_primary(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
        fake_redis: MagicMock,
    ) -> None:
        # Create in primary only — cache is cold
        t = await redis_store._primary.create(make_tenant())
        assert len(fake_redis._store) == 0
        assert await redis_store.exists(t.id) is True

    async def test_exists_false_for_unknown(self, redis_store: RedisTenantStore) -> None:
        assert await redis_store.exists("ghost") is False


class TestCacheManagement:
    async def test_invalidate_all_clears_all_keys(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        await redis_store.create(make_tenant())
        await redis_store.create(make_tenant())
        deleted = await redis_store.invalidate_all()
        assert deleted == 4  # 2 tenants x 2 keys each

    async def test_invalidate_all_empty_store_returns_zero(
        self, redis_store: RedisTenantStore
    ) -> None:
        assert await redis_store.invalidate_all() == 0

    async def test_cache_stats_shape(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        await redis_store.create(make_tenant())
        stats = await redis_store.cache_stats()
        assert "total_keys" in stats
        assert stats["ttl_seconds"] == redis_store._ttl
        assert stats["key_prefix"] == redis_store._prefix


class TestLifecycle:
    async def test_initialize_delegates_to_primary(self, redis_store: RedisTenantStore) -> None:
        primary = redis_store._primary
        primary.initialize = AsyncMock()  # type: ignore
        await redis_store.initialize()
        primary.initialize.assert_awaited_once()  # type: ignore

    async def test_initialize_when_primary_has_no_initialize(
        self, redis_store: RedisTenantStore
    ) -> None:
        # Remove initialize from primary
        primary = redis_store._primary
        if hasattr(primary, "initialize"):
            del primary.initialize
        # Must not raise
        await redis_store.initialize()

    async def test_close_calls_redis_aclose_and_primary(
        self,
        redis_store: RedisTenantStore,
        fake_redis: MagicMock,
    ) -> None:
        primary = redis_store._primary
        primary.close = AsyncMock()  # type: ignore[method-assign]
        await redis_store.close()
        fake_redis.aclose.assert_awaited_once()
        primary.close.assert_awaited_once()


class TestKeyHelpers:
    def test_id_key_format(self, redis_store: RedisTenantStore) -> None:
        key = redis_store._id_key("tenant-123")
        assert key == "test-tenant:id:tenant-123"

    def test_slug_key_format(self, redis_store: RedisTenantStore) -> None:
        key = redis_store._slug_key("acme-corp")
        assert key == "test-tenant:identifier:acme-corp"

    def test_serialize_deserialize_roundtrip(
        self,
        redis_store: RedisTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant()
        data = redis_store._serialize(t)
        recovered = redis_store._deserialize(data)
        assert recovered.id == t.id
        assert recovered.identifier == t.identifier
        assert recovered.status == t.status
