"""Unit tests for :class:`~fastapi_tenancy.storage.memory.InMemoryTenantStore`."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from fastapi_tenancy.core.exceptions import TenantNotFoundError
from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.storage.memory import InMemoryTenantStore

if TYPE_CHECKING:
    from collections.abc import Callable


def _ts(offset_seconds: int = 0) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=offset_seconds)


@pytest.mark.unit
class TestCreate:
    async def test_returns_stored_tenant(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = make_tenant()
        result = await store.create(t)
        assert result.id == t.id
        assert result.identifier == t.identifier

    async def test_duplicate_id_raises_value_error(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        store = InMemoryTenantStore()
        t = make_tenant()
        await store.create(t)
        with pytest.raises(ValueError, match="already exists"):
            await store.create(t)

    async def test_duplicate_identifier_raises_value_error(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        store = InMemoryTenantStore()
        t1 = make_tenant(identifier="shared-slug")
        t2 = make_tenant(identifier="shared-slug")  # different id, same slug
        await store.create(t1)
        with pytest.raises(ValueError, match="already exists"):
            await store.create(t2)

    async def test_identifier_index_updated(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = make_tenant(identifier="acme-corp")
        await store.create(t)
        assert "acme-corp" in store._identifier_map
        assert store._identifier_map["acme-corp"] == t.id

    async def test_all_optional_fields_stored(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = make_tenant(
            metadata={"plan": "enterprise"},
            database_url="postgresql+asyncpg://u:p@h/db",
            schema_name="tenant_acme",
        )
        await store.create(t)
        stored = await store.get_by_id(t.id)
        assert stored.metadata == {"plan": "enterprise"}
        assert stored.database_url == "postgresql+asyncpg://u:p@h/db"
        assert stored.schema_name == "tenant_acme"


@pytest.mark.unit
class TestReads:
    async def test_get_by_id_returns_correct_tenant(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        store = InMemoryTenantStore()
        t = make_tenant()
        await store.create(t)
        result = await store.get_by_id(t.id)
        assert result == t

    async def test_get_by_id_missing_raises_not_found(self) -> None:
        store = InMemoryTenantStore()
        with pytest.raises(TenantNotFoundError):
            await store.get_by_id("does-not-exist")

    async def test_get_by_identifier_returns_correct_tenant(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        store = InMemoryTenantStore()
        t = make_tenant(identifier="my-tenant")
        await store.create(t)
        result = await store.get_by_identifier("my-tenant")
        assert result.id == t.id

    async def test_get_by_identifier_missing_raises_not_found(self) -> None:
        store = InMemoryTenantStore()
        with pytest.raises(TenantNotFoundError):
            await store.get_by_identifier("ghost")

    async def test_get_by_ids_all_found(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t1 = await store.create(make_tenant())
        t2 = await store.create(make_tenant())
        results = await store.get_by_ids([t1.id, t2.id])
        assert {r.id for r in results} == {t1.id, t2.id}

    async def test_get_by_ids_skips_missing(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant())
        results = await store.get_by_ids([t.id, "missing-1", "missing-2"])
        assert len(results) == 1
        assert results[0].id == t.id

    async def test_get_by_ids_preserves_input_order(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        store = InMemoryTenantStore()
        t1 = await store.create(make_tenant())
        t2 = await store.create(make_tenant())
        t3 = await store.create(make_tenant())
        results = await store.get_by_ids([t3.id, t1.id, t2.id])
        assert [r.id for r in results] == [t3.id, t1.id, t2.id]

    async def test_get_by_ids_empty_input(self) -> None:
        store = InMemoryTenantStore()
        assert await store.get_by_ids([]) == []

    async def test_get_by_ids_all_missing(self) -> None:
        store = InMemoryTenantStore()
        assert await store.get_by_ids(["x", "y", "z"]) == []


@pytest.mark.unit
class TestUpdate:
    async def test_update_name(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant(name="Original"))
        updated = await store.update(t.model_copy(update={"name": "Renamed"}))
        assert updated.name == "Renamed"
        assert (await store.get_by_id(t.id)).name == "Renamed"

    async def test_update_refreshes_updated_at(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant(created_at=datetime.now(UTC) - timedelta(hours=1)))
        result = await store.update(t.model_copy(update={"name": "New"}))
        assert result.updated_at > t.updated_at

    async def test_update_identifier_moves_index(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant(identifier="old-slug"))
        await store.update(t.model_copy(update={"identifier": "new-slug"}))

        # Old slug must be gone from the index
        with pytest.raises(TenantNotFoundError):
            await store.get_by_identifier("old-slug")

        # New slug must resolve correctly
        fetched = await store.get_by_identifier("new-slug")
        assert fetched.id == t.id

    async def test_update_missing_raises_not_found(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        store = InMemoryTenantStore()
        ghost = make_tenant()  # never persisted
        with pytest.raises(TenantNotFoundError):
            await store.update(ghost)

    async def test_update_preserves_unchanged_identifier_index_size(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant(identifier="same-slug"))
        # Updating without changing the identifier must NOT add a duplicate key
        await store.update(t.model_copy(update={"name": "Modified"}))
        assert len(store._identifier_map) == 1
        assert "same-slug" in store._identifier_map


@pytest.mark.unit
class TestDelete:
    async def test_delete_removes_from_both_indices(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant(identifier="to-delete"))
        await store.delete(t.id)

        assert t.id not in store._tenants
        assert "to-delete" not in store._identifier_map

    async def test_delete_makes_not_findable(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant())
        await store.delete(t.id)

        with pytest.raises(TenantNotFoundError):
            await store.get_by_id(t.id)

    async def test_delete_missing_raises_not_found(self) -> None:
        store = InMemoryTenantStore()
        with pytest.raises(TenantNotFoundError):
            await store.delete("ghost-id")

    async def test_delete_does_not_affect_other_tenants(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        store = InMemoryTenantStore()
        t1 = await store.create(make_tenant())
        t2 = await store.create(make_tenant())
        await store.delete(t1.id)
        result = await store.get_by_id(t2.id)
        assert result.id == t2.id


@pytest.mark.unit
class TestExistsCount:
    async def test_exists_false_before_create(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = make_tenant()
        assert not await store.exists(t.id)

    async def test_exists_true_after_create(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant())
        assert await store.exists(t.id)

    async def test_exists_false_after_delete(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant())
        await store.delete(t.id)
        assert not await store.exists(t.id)

    async def test_count_all(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        for _ in range(5):
            await store.create(make_tenant())
        assert await store.count() == 5

    async def test_count_by_status(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        await store.create(make_tenant(status=TenantStatus.ACTIVE))
        await store.create(make_tenant(status=TenantStatus.ACTIVE))
        await store.create(make_tenant(status=TenantStatus.SUSPENDED))
        assert await store.count(status=TenantStatus.ACTIVE) == 2
        assert await store.count(status=TenantStatus.SUSPENDED) == 1
        assert await store.count(status=TenantStatus.DELETED) == 0


@pytest.mark.unit
class TestList:
    async def test_empty_store_returns_empty_list(self) -> None:
        store = InMemoryTenantStore()
        assert await store.list() == []

    async def test_sorted_newest_first(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t1 = await store.create(make_tenant(created_at=_ts(-2)))
        t2 = await store.create(make_tenant(created_at=_ts(-1)))
        t3 = await store.create(make_tenant(created_at=_ts(0)))
        results = await store.list()
        assert [r.id for r in results] == [t3.id, t2.id, t1.id]

    async def test_status_filter(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        await store.create(make_tenant(status=TenantStatus.ACTIVE))
        await store.create(make_tenant(status=TenantStatus.SUSPENDED))
        active = await store.list(status=TenantStatus.ACTIVE)
        assert all(t.status == TenantStatus.ACTIVE for t in active)
        assert len(active) == 1

    async def test_pagination_skip(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        for i in range(5):
            await store.create(make_tenant(created_at=_ts(i)))
        page = await store.list(skip=2, limit=2)
        assert len(page) == 2

    async def test_pagination_limit(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        for _ in range(10):
            await store.create(make_tenant())
        page = await store.list(limit=3)
        assert len(page) == 3

    async def test_skip_beyond_total_returns_empty(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        store = InMemoryTenantStore()
        await store.create(make_tenant())
        assert await store.list(skip=100) == []


@pytest.mark.unit
class TestSetStatus:
    async def test_status_updated(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant(status=TenantStatus.ACTIVE))
        result = await store.set_status(t.id, TenantStatus.SUSPENDED)
        assert result.status == TenantStatus.SUSPENDED

    async def test_set_status_refreshes_updated_at(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant(created_at=_ts(-60)))
        result = await store.set_status(t.id, TenantStatus.SUSPENDED)
        assert result.updated_at > t.updated_at

    async def test_set_status_persisted(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant())
        await store.set_status(t.id, TenantStatus.DELETED)
        fetched = await store.get_by_id(t.id)
        assert fetched.status == TenantStatus.DELETED

    async def test_set_status_missing_raises_not_found(self) -> None:
        store = InMemoryTenantStore()
        with pytest.raises(TenantNotFoundError):
            await store.set_status("ghost", TenantStatus.ACTIVE)

    async def test_all_status_transitions(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant())
        for status in TenantStatus:
            result = await store.set_status(t.id, status)
            assert result.status == status


@pytest.mark.unit
class TestUpdateMetadata:
    async def test_shallow_merge_adds_keys(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant(metadata={"a": 1}))
        result = await store.update_metadata(t.id, {"b": 2})
        assert result.metadata == {"a": 1, "b": 2}

    async def test_existing_key_overwritten(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant(metadata={"key": "old"}))
        result = await store.update_metadata(t.id, {"key": "new"})
        assert result.metadata["key"] == "new"

    async def test_successive_updates_accumulate(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant())
        await store.update_metadata(t.id, {"a": 1})
        await store.update_metadata(t.id, {"b": 2})
        result = await store.update_metadata(t.id, {"c": 3})
        assert result.metadata == {"a": 1, "b": 2, "c": 3}

    async def test_refreshes_updated_at(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant(created_at=_ts(-60)))
        result = await store.update_metadata(t.id, {"x": 1})
        assert result.updated_at > t.updated_at

    async def test_empty_metadata_patch_is_noop(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant(metadata={"k": "v"}))
        result = await store.update_metadata(t.id, {})
        assert result.metadata == {"k": "v"}

    async def test_missing_tenant_raises_not_found(self) -> None:
        store = InMemoryTenantStore()
        with pytest.raises(TenantNotFoundError):
            await store.update_metadata("ghost", {"x": 1})


@pytest.mark.unit
class TestBulkUpdateStatus:
    async def test_updates_all_matched_tenants(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t1 = await store.create(make_tenant())
        t2 = await store.create(make_tenant())
        result = await store.bulk_update_status([t1.id, t2.id], TenantStatus.SUSPENDED)
        assert len(result) == 2
        assert all(t.status == TenantStatus.SUSPENDED for t in result)

    async def test_skips_missing_ids(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant())
        result = await store.bulk_update_status(
            [t.id, "missing-1", "missing-2"], TenantStatus.DELETED
        )
        assert len(result) == 1
        assert result[0].id == t.id

    async def test_empty_input_returns_empty(self) -> None:
        store = InMemoryTenantStore()
        assert await store.bulk_update_status([], TenantStatus.ACTIVE) == []

    async def test_all_missing_returns_empty(self) -> None:
        store = InMemoryTenantStore()
        result = await store.bulk_update_status(["a", "b", "c"], TenantStatus.DELETED)
        assert result == []

    async def test_shared_timestamp_across_batch(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t1 = await store.create(make_tenant())
        t2 = await store.create(make_tenant())
        result = await store.bulk_update_status([t1.id, t2.id], TenantStatus.SUSPENDED)
        # All updated_at values should be identical (single timestamp assigned)
        assert result[0].updated_at == result[1].updated_at

    async def test_changes_persisted_in_store(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant())
        await store.bulk_update_status([t.id], TenantStatus.DELETED)
        fetched = await store.get_by_id(t.id)
        assert fetched.status == TenantStatus.DELETED


@pytest.mark.unit
class TestSearch:
    async def test_match_by_identifier(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        await store.create(make_tenant(identifier="acme-corp", name="Acme Corp"))
        await store.create(make_tenant(identifier="globex-inc", name="Globex Inc"))
        results = await store.search("acme")
        assert len(results) == 1
        assert results[0].identifier == "acme-corp"

    async def test_match_by_name(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        await store.create(make_tenant(identifier="org-1", name="Umbrella Corporation"))
        results = await store.search("umbrella")
        assert len(results) == 1

    async def test_case_insensitive(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        await store.create(make_tenant(identifier="acme-corp", name="ACME Corp"))
        assert len(await store.search("ACME")) == 1
        assert len(await store.search("acme")) == 1

    async def test_limit_respected(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        for i in range(10):
            await store.create(make_tenant(identifier=f"tenant-{i:02d}", name=f"Tenant {i}"))
        results = await store.search("tenant", limit=3)
        assert len(results) == 3

    async def test_no_match_returns_empty(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        await store.create(make_tenant(identifier="acme-corp"))
        assert await store.search("nonexistent") == []

    async def test_relevance_exact_match_first(self, make_tenant: Callable[..., Tenant]) -> None:
        """Exact identifier match must rank above prefix and substring matches."""
        store = InMemoryTenantStore()
        await store.create(make_tenant(identifier="acme-corp", name="Acme Full"))
        await store.create(make_tenant(identifier="acme", name="Exact Acme"))  # exact
        await store.create(make_tenant(identifier="big-acme", name="Big Acme"))
        results = await store.search("acme", limit=10)
        # Exact identifier match should be first
        assert results[0].identifier == "acme"

    async def test_empty_store_returns_empty(self) -> None:
        store = InMemoryTenantStore()
        assert await store.search("anything") == []


@pytest.mark.unit
class TestDebugHelpers:
    async def test_clear_empties_both_dicts(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        await store.create(make_tenant())
        store.clear()
        assert store._tenants == {}
        assert store._identifier_map == {}

    async def test_get_all_returns_snapshot(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        t = await store.create(make_tenant())
        snapshot = store.get_all()
        assert t.id in snapshot
        # Mutating snapshot does not affect store
        del snapshot[t.id]
        assert t.id in store._tenants

    async def test_statistics_totals(self, make_tenant: Callable[..., Tenant]) -> None:
        store = InMemoryTenantStore()
        await store.create(make_tenant(status=TenantStatus.ACTIVE))
        await store.create(make_tenant(status=TenantStatus.ACTIVE))
        await store.create(make_tenant(status=TenantStatus.SUSPENDED))
        stats = store.statistics()
        assert stats["total"] == 3
        assert stats["identifier_index_size"] == 3
        assert stats["by_status"][TenantStatus.ACTIVE.value] == 2
        assert stats["by_status"][TenantStatus.SUSPENDED.value] == 1

    async def test_statistics_empty_store(self) -> None:
        store = InMemoryTenantStore()
        stats = store.statistics()
        assert stats["total"] == 0
        assert stats["by_status"] == {}
        assert stats["identifier_index_size"] == 0

    def test_close_is_noop(self) -> None:
        """close() on InMemoryTenantStore inherits the base no-op."""
        # Base TenantStore.close() is synchronous no-op — does not raise
        store = InMemoryTenantStore()
        # The method exists; calling it should not error when awaited
        import inspect  # noqa: PLC0415

        assert inspect.iscoroutinefunction(store.close)


@pytest.mark.unit
class TestConcurrency:
    async def test_concurrent_creates_all_succeed(self, make_tenant: Callable[..., Tenant]) -> None:
        """50 concurrent creates must not corrupt the identifier index."""
        store = InMemoryTenantStore()
        tenants = [make_tenant() for _ in range(50)]
        await asyncio.gather(*(store.create(t) for t in tenants))
        assert await store.count() == 50
        assert len(store._identifier_map) == 50

    async def test_concurrent_status_updates_all_apply(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        store = InMemoryTenantStore()
        tenants = [await store.create(make_tenant()) for _ in range(20)]
        await asyncio.gather(*(store.set_status(t.id, TenantStatus.SUSPENDED) for t in tenants))
        results = await store.list(status=TenantStatus.SUSPENDED)
        assert len(results) == 20

    async def test_concurrent_metadata_updates_no_lost_writes(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        """Concurrent metadata patches to the *same* tenant must not lose data."""
        store = InMemoryTenantStore()
        t = await store.create(make_tenant())
        patches: list[dict[str, Any]] = [{f"key_{i}": i} for i in range(20)]
        await asyncio.gather(*(store.update_metadata(t.id, p) for p in patches))
        final = await store.get_by_id(t.id)
        # Every key must be present — no patch must have been silently dropped
        assert len(final.metadata) == 20
