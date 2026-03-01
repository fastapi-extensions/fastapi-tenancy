"""Tests for fastapi_tenancy.storage.memory — InMemoryTenantStore."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from fastapi_tenancy.core.exceptions import TenantNotFoundError
from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.storage.memory import InMemoryTenantStore


def make_tenant(
    identifier: str = "acme-corp",
    name: str = "Acme Corp",
    tenant_id: str = "t-001",
    status: TenantStatus = TenantStatus.ACTIVE,
    metadata: dict[str, Any] | None = None,
) -> Tenant:
    return Tenant(
        id=tenant_id,
        identifier=identifier,
        name=name,
        status=status,
        metadata=metadata or {},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def store() -> InMemoryTenantStore:
    return InMemoryTenantStore()


@pytest.fixture
async def seeded_store() -> InMemoryTenantStore:
    s = InMemoryTenantStore()
    await s.create(make_tenant("acme-corp", tenant_id="t-001"))
    await s.create(make_tenant("globex-inc", name="Globex Inc", tenant_id="t-002"))
    await s.create(make_tenant(
        "initech", name="Initech", tenant_id="t-003",
        status=TenantStatus.SUSPENDED,
    ))
    return s


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

class TestGetById:
    async def test_found(self, store):
        t = make_tenant()
        await store.create(t)
        result = await store.get_by_id(t.id)
        assert result.id == t.id

    async def test_not_found_raises(self, store):
        with pytest.raises(TenantNotFoundError):
            await store.get_by_id("does-not-exist")

    async def test_returns_correct_tenant(self, seeded_store):
        t = await seeded_store.get_by_id("t-002")
        assert t.identifier == "globex-inc"


class TestGetByIdentifier:
    async def test_found(self, store):
        t = make_tenant()
        await store.create(t)
        result = await store.get_by_identifier(t.identifier)
        assert result.id == t.id

    async def test_not_found_raises(self, store):
        with pytest.raises(TenantNotFoundError):
            await store.get_by_identifier("nonexistent")

    async def test_lookup_is_o1_via_index(self, seeded_store):
        result = await seeded_store.get_by_identifier("globex-inc")
        assert result.id == "t-002"


class TestList:
    async def test_empty_store(self, store):
        result = await store.list()
        assert result == []

    async def test_returns_all(self, seeded_store):
        result = await seeded_store.list()
        assert len(result) == 3

    async def test_sorted_by_created_at_desc(self, store):
        # Create in order, expect newest first
        for i in range(3):
            await store.create(make_tenant(
                identifier=f"tenant-{i:02d}",
                tenant_id=f"t-{i:02d}",
            ))
        result = await store.list()
        # Most recently created last in loop == first in result
        assert result[0].id == "t-02"

    async def test_skip_and_limit(self, seeded_store):
        all_tenants = await seeded_store.list()
        page1 = await seeded_store.list(skip=0, limit=2)
        page2 = await seeded_store.list(skip=2, limit=2)
        assert len(page1) == 2
        assert len(page2) == 1
        assert set(t.id for t in page1 + page2) == set(t.id for t in all_tenants)

    async def test_filter_by_status_active(self, seeded_store):
        active = await seeded_store.list(status=TenantStatus.ACTIVE)
        assert all(t.status == TenantStatus.ACTIVE for t in active)
        assert len(active) == 2

    async def test_filter_by_status_suspended(self, seeded_store):
        suspended = await seeded_store.list(status=TenantStatus.SUSPENDED)
        assert len(suspended) == 1
        assert suspended[0].identifier == "initech"

    async def test_filter_by_status_no_results(self, seeded_store):
        deleted = await seeded_store.list(status=TenantStatus.DELETED)
        assert deleted == []


class TestCount:
    async def test_empty(self, store):
        assert await store.count() == 0

    async def test_total(self, seeded_store):
        assert await seeded_store.count() == 3

    async def test_count_by_status(self, seeded_store):
        assert await seeded_store.count(status=TenantStatus.ACTIVE) == 2
        assert await seeded_store.count(status=TenantStatus.SUSPENDED) == 1
        assert await seeded_store.count(status=TenantStatus.DELETED) == 0


class TestExists:
    async def test_existing(self, store):
        t = make_tenant()
        await store.create(t)
        assert await store.exists(t.id) is True

    async def test_non_existing(self, store):
        assert await store.exists("nope") is False


class TestGetByIds:
    async def test_all_found(self, seeded_store):
        result = await seeded_store.get_by_ids(["t-001", "t-002"])
        assert len(result) == 2
        ids = [t.id for t in result]
        assert "t-001" in ids
        assert "t-002" in ids

    async def test_partial_found(self, seeded_store):
        result = await seeded_store.get_by_ids(["t-001", "doesnt-exist"])
        assert len(result) == 1
        assert result[0].id == "t-001"

    async def test_empty_input(self, seeded_store):
        result = await seeded_store.get_by_ids([])
        assert list(result) == []

    async def test_all_missing(self, seeded_store):
        result = await seeded_store.get_by_ids(["x", "y"])
        assert list(result) == []


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

class TestCreate:
    async def test_basic_create(self, store):
        t = make_tenant()
        created = await store.create(t)
        assert created.id == t.id
        assert created.identifier == t.identifier

    async def test_duplicate_id_raises(self, store):
        t = make_tenant()
        await store.create(t)
        with pytest.raises(ValueError, match="already exists"):
            await store.create(t)  # same object

    async def test_duplicate_identifier_raises(self, store):
        t1 = make_tenant(tenant_id="t-1")
        t2 = make_tenant(tenant_id="t-2")  # same identifier "acme-corp"
        await store.create(t1)
        with pytest.raises(ValueError, match="identifier"):
            await store.create(t2)

    async def test_identifier_index_updated(self, store):
        t = make_tenant()
        await store.create(t)
        found = await store.get_by_identifier(t.identifier)
        assert found.id == t.id


class TestUpdate:
    async def test_updates_mutable_fields(self, store):
        t = make_tenant()
        await store.create(t)
        updated = t.model_copy(update={"name": "New Name"})
        result = await store.update(updated)
        assert result.name == "New Name"

    async def test_updates_updated_at(self, store):
        t = make_tenant()
        await store.create(t)
        original_updated = t.updated_at
        import asyncio; await asyncio.sleep(0.001)
        result = await store.update(t.model_copy(update={"name": "Changed"}))
        assert result.updated_at > original_updated

    async def test_identifier_change_updates_index(self, store):
        t = make_tenant(identifier="old-ident")
        await store.create(t)
        new_t = t.model_copy(update={"identifier": "new-ident"})
        await store.update(new_t)
        # Old identifier should no longer work
        with pytest.raises(TenantNotFoundError):
            await store.get_by_identifier("old-ident")
        # New identifier should work
        found = await store.get_by_identifier("new-ident")
        assert found.id == t.id

    async def test_update_nonexistent_raises(self, store):
        t = make_tenant(tenant_id="ghost")
        with pytest.raises(TenantNotFoundError):
            await store.update(t)


class TestDelete:
    async def test_deletes_tenant(self, store):
        t = make_tenant()
        await store.create(t)
        await store.delete(t.id)
        assert not await store.exists(t.id)

    async def test_cleans_identifier_index(self, store):
        t = make_tenant()
        await store.create(t)
        await store.delete(t.id)
        with pytest.raises(TenantNotFoundError):
            await store.get_by_identifier(t.identifier)

    async def test_delete_nonexistent_raises(self, store):
        with pytest.raises(TenantNotFoundError):
            await store.delete("ghost-id")


class TestSetStatus:
    async def test_sets_status(self, store):
        t = make_tenant()
        await store.create(t)
        result = await store.set_status(t.id, TenantStatus.SUSPENDED)
        assert result.status == TenantStatus.SUSPENDED

    async def test_updates_updated_at(self, store):
        t = make_tenant()
        await store.create(t)
        import asyncio; await asyncio.sleep(0.001)
        result = await store.set_status(t.id, TenantStatus.SUSPENDED)
        assert result.updated_at > t.updated_at

    async def test_not_found_raises(self, store):
        with pytest.raises(TenantNotFoundError):
            await store.set_status("ghost", TenantStatus.SUSPENDED)


class TestUpdateMetadata:
    async def test_merges_metadata(self, store):
        t = make_tenant(metadata={"a": 1})
        await store.create(t)
        result = await store.update_metadata(t.id, {"b": 2})
        assert result.metadata == {"a": 1, "b": 2}

    async def test_overwrites_existing_key(self, store):
        t = make_tenant(metadata={"x": "old"})
        await store.create(t)
        result = await store.update_metadata(t.id, {"x": "new"})
        assert result.metadata["x"] == "new"

    async def test_not_found_raises(self, store):
        with pytest.raises(TenantNotFoundError):
            await store.update_metadata("ghost", {"k": "v"})

    async def test_updates_updated_at(self, store):
        t = make_tenant()
        await store.create(t)
        import asyncio; await asyncio.sleep(0.001)
        result = await store.update_metadata(t.id, {"k": "v"})
        assert result.updated_at > t.updated_at


class TestBulkUpdateStatus:
    async def test_updates_multiple(self, seeded_store):
        result = await seeded_store.bulk_update_status(
            ["t-001", "t-002"], TenantStatus.SUSPENDED
        )
        assert len(result) == 2
        assert all(t.status == TenantStatus.SUSPENDED for t in result)

    async def test_skips_missing_ids(self, seeded_store):
        result = await seeded_store.bulk_update_status(
            ["t-001", "ghost-id"], TenantStatus.DELETED
        )
        assert len(result) == 1
        assert result[0].id == "t-001"

    async def test_empty_input(self, seeded_store):
        result = await seeded_store.bulk_update_status([], TenantStatus.DELETED)
        assert list(result) == []

    async def test_shared_timestamp(self, seeded_store):
        result = await seeded_store.bulk_update_status(
            ["t-001", "t-002"], TenantStatus.SUSPENDED
        )
        assert len(result) == 2
        assert result[0].updated_at == result[1].updated_at


class TestSearch:
    async def test_find_by_name_substring(self, seeded_store):
        result = await seeded_store.search("Acme")
        assert len(result) >= 1
        assert any(t.identifier == "acme-corp" for t in result)

    async def test_find_by_identifier_substring(self, seeded_store):
        result = await seeded_store.search("glob")
        assert any(t.identifier == "globex-inc" for t in result)

    async def test_case_insensitive(self, seeded_store):
        result = await seeded_store.search("ACME")
        assert any(t.identifier == "acme-corp" for t in result)

    async def test_exact_identifier_match_ranked_first(self, seeded_store):
        result = await seeded_store.search("acme-corp")
        assert result[0].identifier == "acme-corp"

    async def test_no_results(self, seeded_store):
        result = await seeded_store.search("zzz-no-match")
        assert result == []

    async def test_empty_query_matches_all(self, seeded_store):
        # Empty string — every name contains ""
        result = await seeded_store.search("")
        assert len(result) == 3

    async def test_limit_respected(self, seeded_store):
        result = await seeded_store.search("", limit=2)
        assert len(result) == 2

    async def test_prefix_ranked_higher_than_contains(self, store):
        await store.create(make_tenant("acme-tech", name="Acme Tech", tenant_id="t-a"))
        await store.create(make_tenant("new-acme", name="New Acme", tenant_id="t-b"))
        result = await store.search("acme")
        # "acme-tech" starts with "acme", "new-acme" only contains it
        identifiers = [t.identifier for t in result]
        assert identifiers.index("acme-tech") < identifiers.index("new-acme")


# ---------------------------------------------------------------------------
# Debug / test helpers
# ---------------------------------------------------------------------------

class TestClear:
    async def test_clears_all(self, seeded_store):
        seeded_store.clear()
        assert await seeded_store.count() == 0

    async def test_clears_identifier_index(self, seeded_store):
        seeded_store.clear()
        with pytest.raises(TenantNotFoundError):
            await seeded_store.get_by_identifier("acme-corp")


class TestGetAll:
    async def test_returns_snapshot(self, seeded_store):
        snapshot = seeded_store.get_all()
        assert len(snapshot) == 3
        assert "t-001" in snapshot

    async def test_mutation_does_not_affect_store(self, seeded_store):
        snapshot = seeded_store.get_all()
        snapshot["injected"] = make_tenant(tenant_id="injected")
        assert not await seeded_store.exists("injected")


class TestStatistics:
    async def test_stats(self, seeded_store):
        stats = seeded_store.statistics()
        assert stats["total"] == 3
        assert stats["by_status"]["active"] == 2
        assert stats["by_status"]["suspended"] == 1
        assert stats["identifier_index_size"] == 3

    async def test_empty_store_stats(self, store):
        stats = store.statistics()
        assert stats["total"] == 0
        assert stats["by_status"] == {}
        assert stats["identifier_index_size"] == 0


# ---------------------------------------------------------------------------
# Concurrency / lock correctness
# ---------------------------------------------------------------------------

class TestConcurrency:
    async def test_concurrent_creates_unique_ids(self, store):
        """Concurrent creates with distinct IDs must all succeed."""
        async def create_one(n):
            t = make_tenant(
                identifier=f"tenant-{n:04d}",
                tenant_id=f"t-{n:04d}",
            )
            await store.create(t)

        await asyncio.gather(*[create_one(i) for i in range(20)])
        assert await store.count() == 20

    async def test_concurrent_creates_same_id_raises(self, store):
        """Two concurrent creates of the same ID — exactly one must succeed."""
        t = make_tenant()
        results = []
        async def try_create():
            try:
                await store.create(t)
                results.append("ok")
            except ValueError:
                results.append("dup")
        await asyncio.gather(try_create(), try_create())
        assert results.count("ok") == 1
        assert results.count("dup") == 1
