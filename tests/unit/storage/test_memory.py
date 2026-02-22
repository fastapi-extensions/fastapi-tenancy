"""Unit tests for InMemoryTenantStore"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest

from fastapi_tenancy.core.exceptions import TenantNotFoundError
from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.storage.memory import InMemoryTenantStore


def _make(
    identifier: str = "acme-corp",
    status: TenantStatus = TenantStatus.ACTIVE,
    tid: str | None = None,
) -> Tenant:
    return Tenant(
        id=tid or str(uuid.uuid4()),
        identifier=identifier,
        name=f"Tenant {identifier}",
        status=status,
    )


@pytest.fixture
def store() -> InMemoryTenantStore:
    return InMemoryTenantStore()


@pytest.fixture
async def filled_store() -> InMemoryTenantStore:
    s = InMemoryTenantStore()
    for i in range(5):
        await s.create(_make(identifier=f"tenant-{i:02d}", status=TenantStatus.ACTIVE))
    # One suspended
    await s.create(_make(identifier="sus-tenant", status=TenantStatus.SUSPENDED))
    return s


class TestCreate:
    @pytest.mark.asyncio
    async def test_create_returns_tenant(self, store):
        t = _make()
        result = await store.create(t)
        assert result is t

    @pytest.mark.asyncio
    async def test_duplicate_id_raises(self, store):
        t = _make(tid="same-id")
        await store.create(t)
        t2 = _make(identifier="other-corp", tid="same-id")
        with pytest.raises(ValueError, match="already exists"):
            await store.create(t2)

    @pytest.mark.asyncio
    async def test_duplicate_identifier_raises(self, store):
        t1 = _make(identifier="acme-corp")
        t2 = _make(identifier="acme-corp", tid="other-id")
        await store.create(t1)
        with pytest.raises(ValueError, match="already exists"):
            await store.create(t2)


class TestRead:
    @pytest.mark.asyncio
    async def test_get_by_id(self, store):
        t = _make()
        await store.create(t)
        result = await store.get_by_id(t.id)
        assert result.id == t.id

    @pytest.mark.asyncio
    async def test_get_by_id_not_found(self, store):
        with pytest.raises(TenantNotFoundError):
            await store.get_by_id("nope")

    @pytest.mark.asyncio
    async def test_get_by_identifier(self, store):
        t = _make(identifier="acme-corp")
        await store.create(t)
        result = await store.get_by_identifier("acme-corp")
        assert result.identifier == "acme-corp"

    @pytest.mark.asyncio
    async def test_get_by_identifier_not_found(self, store):
        with pytest.raises(TenantNotFoundError):
            await store.get_by_identifier("nope-corp")

    @pytest.mark.asyncio
    async def test_exists_true(self, store):
        t = _make()
        await store.create(t)
        assert await store.exists(t.id) is True

    @pytest.mark.asyncio
    async def test_exists_false(self, store):
        assert await store.exists("nope") is False


class TestList:
    @pytest.mark.asyncio
    async def test_list_all(self, filled_store):
        result = await filled_store.list()
        assert len(result) == 6

    @pytest.mark.asyncio
    async def test_list_sorted_newest_first(self, store):
        # Create with slight time spread
        for i in range(3):
            t = Tenant(
                id=f"t{i}",
                identifier=f"tenant-{i:02d}",
                name=f"T{i}",
                created_at=datetime(2024, 1, i + 1, tzinfo=UTC),
                updated_at=datetime(2024, 1, i + 1, tzinfo=UTC),
            )
            await store.create(t)
        results = await store.list()
        assert results[0].id == "t2"  # newest first

    @pytest.mark.asyncio
    async def test_list_pagination(self, filled_store):
        page1 = await filled_store.list(skip=0, limit=2)
        page2 = await filled_store.list(skip=2, limit=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert {t.id for t in page1}.isdisjoint({t.id for t in page2})

    @pytest.mark.asyncio
    async def test_list_filter_by_status(self, filled_store):
        suspended = await filled_store.list(status=TenantStatus.SUSPENDED)
        assert len(suspended) == 1
        assert all(t.status == TenantStatus.SUSPENDED for t in suspended)


class TestCount:
    @pytest.mark.asyncio
    async def test_count_all(self, filled_store):
        assert await filled_store.count() == 6

    @pytest.mark.asyncio
    async def test_count_by_status(self, filled_store):
        assert await filled_store.count(status=TenantStatus.ACTIVE) == 5
        assert await filled_store.count(status=TenantStatus.SUSPENDED) == 1

    @pytest.mark.asyncio
    async def test_count_empty(self, store):
        assert await store.count() == 0


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_name(self, store):
        t = _make()
        await store.create(t)
        updated = t.model_copy(update={"name": "New Name"})
        result = await store.update(updated)
        assert result.name == "New Name"
        # updated_at is refreshed
        assert result.updated_at >= t.updated_at

    @pytest.mark.asyncio
    async def test_update_identifier_updates_index(self, store):
        t = _make(identifier="old-slug")
        await store.create(t)
        updated = t.model_copy(update={"identifier": "new-slug"})
        await store.update(updated)
        with pytest.raises(TenantNotFoundError):
            await store.get_by_identifier("old-slug")
        result = await store.get_by_identifier("new-slug")
        assert result.id == t.id

    @pytest.mark.asyncio
    async def test_update_not_found_raises(self, store):
        t = _make(tid="missing")
        with pytest.raises(TenantNotFoundError):
            await store.update(t)


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_tenant(self, store):
        t = _make()
        await store.create(t)
        await store.delete(t.id)
        assert not await store.exists(t.id)

    @pytest.mark.asyncio
    async def test_delete_removes_identifier_index(self, store):
        t = _make(identifier="acme-corp")
        await store.create(t)
        await store.delete(t.id)
        with pytest.raises(TenantNotFoundError):
            await store.get_by_identifier("acme-corp")

    @pytest.mark.asyncio
    async def test_delete_not_found_raises(self, store):
        with pytest.raises(TenantNotFoundError):
            await store.delete("missing")


class TestSetStatus:
    @pytest.mark.asyncio
    async def test_set_status(self, store):
        t = _make()
        await store.create(t)
        result = await store.set_status(t.id, TenantStatus.SUSPENDED)
        assert result.status == TenantStatus.SUSPENDED

    @pytest.mark.asyncio
    async def test_set_status_updates_updated_at(self, store):
        t = _make()
        await store.create(t)
        result = await store.set_status(t.id, TenantStatus.DELETED)
        assert result.updated_at >= t.updated_at

    @pytest.mark.asyncio
    async def test_set_status_not_found(self, store):
        with pytest.raises(TenantNotFoundError):
            await store.set_status("missing", TenantStatus.ACTIVE)


class TestUpdateMetadata:
    @pytest.mark.asyncio
    async def test_merge_metadata(self, store):
        t = Tenant(id="t1", identifier="acme-corp", name="Acme", metadata={"a": 1})
        await store.create(t)
        result = await store.update_metadata(t.id, {"b": 2, "a": 99})
        assert result.metadata == {"a": 99, "b": 2}

    @pytest.mark.asyncio
    async def test_preserves_existing_keys(self, store):
        t = Tenant(id="t1", identifier="acme-corp", name="Acme", metadata={"keep": "this"})
        await store.create(t)
        result = await store.update_metadata(t.id, {"new": "key"})
        assert result.metadata["keep"] == "this"

    @pytest.mark.asyncio
    async def test_update_metadata_not_found(self, store):
        with pytest.raises(TenantNotFoundError):
            await store.update_metadata("missing", {"k": "v"})


class TestGetByIds:
    @pytest.mark.asyncio
    async def test_returns_found_tenants(self, store):
        t1 = _make(identifier="t-01", tid="t1")
        t2 = _make(identifier="t-02", tid="t2")
        await store.create(t1)
        await store.create(t2)
        result = await store.get_by_ids(["t1", "t2", "missing"])
        ids = {t.id for t in result}
        assert "t1" in ids
        assert "t2" in ids
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_returns_empty_for_empty_input(self, store):
        result = await store.get_by_ids([])
        assert result == []


class TestBulkUpdateStatus:
    @pytest.mark.asyncio
    async def test_bulk_update(self, store):
        t1 = _make(identifier="t-01", tid="t1")
        t2 = _make(identifier="t-02", tid="t2")
        await store.create(t1)
        await store.create(t2)
        results = await store.bulk_update_status(["t1", "t2"], TenantStatus.SUSPENDED)
        assert all(t.status == TenantStatus.SUSPENDED for t in results)

    @pytest.mark.asyncio
    async def test_bulk_skips_missing(self, store):
        t = _make(identifier="t-01", tid="t1")
        await store.create(t)
        results = await store.bulk_update_status(["t1", "missing"], TenantStatus.DELETED)
        assert len(results) == 1


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_by_identifier(self, store):
        await store.create(_make(identifier="acme-corp"))
        await store.create(_make(identifier="globex-corp"))
        results = await store.search("acme")
        assert len(results) == 1
        assert results[0].identifier == "acme-corp"

    @pytest.mark.asyncio
    async def test_search_by_name(self, store):
        t = Tenant(id="t1", identifier="my-co", name="Acme Corporation")
        await store.create(t)
        results = await store.search("Corporation")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_search_case_insensitive(self, store):
        await store.create(_make(identifier="acme-corp"))
        results = await store.search("ACME")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_search_limit(self, store):
        for i in range(10):
            await store.create(_make(identifier=f"acme-{i:02d}"))
        results = await store.search("acme", limit=3)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_search_empty(self, store):
        results = await store.search("nomatch")
        assert results == []


class TestDebugHelpers:
    @pytest.mark.asyncio
    async def test_clear(self, filled_store):
        filled_store.clear()
        assert await filled_store.count() == 0

    @pytest.mark.asyncio
    async def test_get_all(self, filled_store):
        all_tenants = filled_store.get_all()
        assert len(all_tenants) == 6

    @pytest.mark.asyncio
    async def test_statistics(self, filled_store):
        stats = filled_store.statistics()
        assert stats["total"] == 6
        assert stats["identifier_index_size"] == 6
        assert "by_status" in stats
        assert stats["by_status"]["active"] == 5
        assert stats["by_status"]["suspended"] == 1
