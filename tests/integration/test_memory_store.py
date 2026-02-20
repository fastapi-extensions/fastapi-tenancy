"""Integration tests — fastapi_tenancy.storage.memory.InMemoryTenantStore

Coverage target: 100 %

All operations tested with real async calls (no mocking of the store itself).
Verifies full CRUD lifecycle, index integrity, search ranking, statistics, bulk ops.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fastapi_tenancy.core.exceptions import TenantNotFoundError
from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.storage.memory import InMemoryTenantStore

pytestmark = pytest.mark.integration

_NOW = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
_LATER = datetime(2024, 1, 2, 0, 0, 0, tzinfo=UTC)


def _t(
    id: str = "t-001",
    identifier: str = "acme-corp",
    name: str = "Acme Corp",
    status: TenantStatus = TenantStatus.ACTIVE,
    ts: datetime = _NOW,
) -> Tenant:
    return Tenant(
        id=id,
        identifier=identifier,
        name=name,
        status=status,
        created_at=ts,
        updated_at=ts,
    )


@pytest.fixture
async def s() -> InMemoryTenantStore:
    store = InMemoryTenantStore()
    yield store
    store.clear()


# ───────────────────────────── create ────────────────────────────────────────


class TestCreate:
    async def test_create_returns_tenant(self, s):
        t = _t()
        result = await s.create(t)
        assert result.id == t.id
        assert result.identifier == t.identifier

    async def test_created_tenant_retrievable_by_id(self, s):
        t = _t()
        await s.create(t)
        found = await s.get_by_id(t.id)
        assert found.id == t.id

    async def test_created_tenant_retrievable_by_identifier(self, s):
        t = _t()
        await s.create(t)
        found = await s.get_by_identifier(t.identifier)
        assert found.identifier == t.identifier

    async def test_duplicate_id_raises(self, s):
        t = _t()
        await s.create(t)
        with pytest.raises(ValueError, match="already exists"):
            await s.create(t)

    async def test_duplicate_identifier_raises(self, s):
        t1 = _t(id="t-001", identifier="same-slug")
        t2 = _t(id="t-002", identifier="same-slug")
        await s.create(t1)
        with pytest.raises(ValueError, match="already exists"):
            await s.create(t2)

    async def test_multiple_creates_succeed(self, s):
        await s.create(_t(id="t-001", identifier="acme-corp"))
        await s.create(_t(id="t-002", identifier="globex-corp"))
        assert await s.count() == 2


# ──────────────────────────── get_by_id ──────────────────────────────────────


class TestGetById:
    async def test_existing_returns_tenant(self, s):
        t = _t()
        await s.create(t)
        result = await s.get_by_id(t.id)
        assert result.id == t.id

    async def test_missing_raises(self, s):
        with pytest.raises(TenantNotFoundError):
            await s.get_by_id("nonexistent")

    async def test_returns_exact_object(self, s):
        t = _t()
        await s.create(t)
        found = await s.get_by_id(t.id)
        assert found == t


# ──────────────────────── get_by_identifier ──────────────────────────────────


class TestGetByIdentifier:
    async def test_existing_returns_tenant(self, s):
        t = _t()
        await s.create(t)
        result = await s.get_by_identifier(t.identifier)
        assert result.identifier == t.identifier

    async def test_missing_raises(self, s):
        with pytest.raises(TenantNotFoundError):
            await s.get_by_identifier("no-such-slug")

    async def test_case_sensitive(self, s):
        await s.create(_t(identifier="acme-corp"))
        with pytest.raises(TenantNotFoundError):
            await s.get_by_identifier("ACME-CORP")


# ─────────────────────────────── list ────────────────────────────────────────


class TestList:
    async def test_empty_store(self, s):
        assert await s.list() == []

    async def test_returns_all(self, s):
        await s.create(_t(id="t-001", identifier="a-corp", ts=_NOW))
        await s.create(_t(id="t-002", identifier="b-corp", ts=_LATER))
        tenants = await s.list()
        assert len(tenants) == 2

    async def test_sorted_by_created_at_descending(self, s):
        await s.create(_t(id="t-001", identifier="older", ts=_NOW))
        await s.create(_t(id="t-002", identifier="newer", ts=_LATER))
        tenants = await s.list()
        assert tenants[0].identifier == "newer"
        assert tenants[1].identifier == "older"

    async def test_skip_pagination(self, s):
        for i in range(5):
            await s.create(_t(id=f"t-{i:03d}", identifier=f"corp-{i:02d}"))
        page = await s.list(skip=2, limit=2)
        assert len(page) == 2

    async def test_limit_pagination(self, s):
        for i in range(10):
            await s.create(_t(id=f"t-{i:03d}", identifier=f"corp-{i:02d}"))
        page = await s.list(limit=3)
        assert len(page) == 3

    async def test_filter_by_status(self, s):
        await s.create(_t(id="t-001", identifier="active-one", status=TenantStatus.ACTIVE))
        await s.create(_t(id="t-002", identifier="suspended-one", status=TenantStatus.SUSPENDED))
        active = await s.list(status=TenantStatus.ACTIVE)
        assert len(active) == 1
        assert active[0].identifier == "active-one"


# ─────────────────────────────── count ───────────────────────────────────────


class TestCount:
    async def test_empty(self, s):
        assert await s.count() == 0

    async def test_total_count(self, s):
        await s.create(_t(id="t-001", identifier="a"))
        await s.create(_t(id="t-002", identifier="b"))
        assert await s.count() == 2

    async def test_count_by_status(self, s):
        await s.create(_t(id="t-001", identifier="active", status=TenantStatus.ACTIVE))
        await s.create(_t(id="t-002", identifier="susp", status=TenantStatus.SUSPENDED))
        assert await s.count(status=TenantStatus.ACTIVE) == 1
        assert await s.count(status=TenantStatus.SUSPENDED) == 1
        assert await s.count(status=TenantStatus.DELETED) == 0


# ─────────────────────────────── exists ──────────────────────────────────────


class TestExists:
    async def test_existing(self, s):
        t = _t()
        await s.create(t)
        assert await s.exists(t.id) is True

    async def test_missing(self, s):
        assert await s.exists("no-such-id") is False


# ──────────────────────────── get_by_ids ─────────────────────────────────────


class TestGetByIds:
    async def test_all_found(self, s):
        t1 = _t(id="t-001", identifier="acme")
        t2 = _t(id="t-002", identifier="globex")
        await s.create(t1)
        await s.create(t2)
        result = await s.get_by_ids(["t-001", "t-002"])
        assert len(result) == 2

    async def test_missing_ids_skipped(self, s):
        t = _t()
        await s.create(t)
        result = await s.get_by_ids(["t-001", "nonexistent"])
        assert len(result) == 1

    async def test_empty_list_returns_empty(self, s):
        result = await s.get_by_ids([])
        assert result == []


# ────────────────────────────── update ───────────────────────────────────────


class TestUpdate:
    async def test_update_name(self, s):
        t = _t()
        await s.create(t)
        updated = t.model_copy(update={"name": "New Name"})
        result = await s.update(updated)
        assert result.name == "New Name"

    async def test_update_refreshes_updated_at(self, s):
        t = _t(ts=_NOW)
        await s.create(t)
        updated = t.model_copy(update={"name": "Changed"})
        result = await s.update(updated)
        assert result.updated_at > _NOW

    async def test_update_identifier_changes_index(self, s):
        t = _t(identifier="old-slug")
        await s.create(t)
        updated = t.model_copy(update={"identifier": "new-slug"})
        await s.update(updated)
        with pytest.raises(TenantNotFoundError):
            await s.get_by_identifier("old-slug")
        found = await s.get_by_identifier("new-slug")
        assert found.identifier == "new-slug"

    async def test_update_missing_raises(self, s):
        t = _t(id="no-such-id")
        with pytest.raises(TenantNotFoundError):
            await s.update(t)


# ─────────────────────────────── delete ──────────────────────────────────────


class TestDelete:
    async def test_delete_removes_from_id_index(self, s):
        t = _t()
        await s.create(t)
        await s.delete(t.id)
        with pytest.raises(TenantNotFoundError):
            await s.get_by_id(t.id)

    async def test_delete_removes_from_identifier_index(self, s):
        t = _t()
        await s.create(t)
        await s.delete(t.id)
        with pytest.raises(TenantNotFoundError):
            await s.get_by_identifier(t.identifier)

    async def test_delete_missing_raises(self, s):
        with pytest.raises(TenantNotFoundError):
            await s.delete("ghost-id")

    async def test_delete_reduces_count(self, s):
        t1 = _t(id="t-001", identifier="a")
        t2 = _t(id="t-002", identifier="b")
        await s.create(t1)
        await s.create(t2)
        await s.delete(t1.id)
        assert await s.count() == 1


# ────────────────────────────── set_status ───────────────────────────────────


class TestSetStatus:
    async def test_status_updated(self, s):
        t = _t(status=TenantStatus.ACTIVE)
        await s.create(t)
        updated = await s.set_status(t.id, TenantStatus.SUSPENDED)
        assert updated.status == TenantStatus.SUSPENDED

    async def test_updated_at_refreshed(self, s):
        t = _t(ts=_NOW)
        await s.create(t)
        updated = await s.set_status(t.id, TenantStatus.DELETED)
        assert updated.updated_at > _NOW

    async def test_missing_raises(self, s):
        with pytest.raises(TenantNotFoundError):
            await s.set_status("ghost", TenantStatus.ACTIVE)


# ──────────────────────────── update_metadata ────────────────────────────────


class TestUpdateMetadata:
    async def test_merge_new_keys(self, s):
        t = _t()
        await s.create(t)
        result = await s.update_metadata(t.id, {"plan": "pro"})
        assert result.metadata["plan"] == "pro"

    async def test_existing_keys_preserved(self, s):
        t = _t()
        await s.create(t)
        await s.update_metadata(t.id, {"a": 1})
        result = await s.update_metadata(t.id, {"b": 2})
        assert result.metadata["a"] == 1
        assert result.metadata["b"] == 2

    async def test_key_overwritten(self, s):
        t = _t()
        await s.create(t)
        await s.update_metadata(t.id, {"plan": "basic"})
        result = await s.update_metadata(t.id, {"plan": "pro"})
        assert result.metadata["plan"] == "pro"

    async def test_missing_raises(self, s):
        with pytest.raises(TenantNotFoundError):
            await s.update_metadata("ghost", {"key": "val"})


# ─────────────────────────── bulk_update_status ──────────────────────────────


class TestBulkUpdateStatus:
    async def test_updates_all_matched(self, s):
        t1 = _t(id="t-001", identifier="a")
        t2 = _t(id="t-002", identifier="b")
        await s.create(t1)
        await s.create(t2)
        result = await s.bulk_update_status(["t-001", "t-002"], TenantStatus.SUSPENDED)
        assert len(result) == 2
        assert all(t.status == TenantStatus.SUSPENDED for t in result)

    async def test_missing_ids_skipped(self, s):
        t = _t()
        await s.create(t)
        result = await s.bulk_update_status([t.id, "nonexistent"], TenantStatus.DELETED)
        assert len(result) == 1

    async def test_same_timestamp_on_all(self, s):
        t1 = _t(id="t-001", identifier="a")
        t2 = _t(id="t-002", identifier="b")
        await s.create(t1)
        await s.create(t2)
        result = await s.bulk_update_status(["t-001", "t-002"], TenantStatus.SUSPENDED)
        assert result[0].updated_at == result[1].updated_at

    async def test_empty_list_returns_empty(self, s):
        result = await s.bulk_update_status([], TenantStatus.ACTIVE)
        assert result == []


# ───────────────────────────── search ────────────────────────────────────────


class TestSearch:
    async def _seed(self, s):
        await s.create(_t(id="t-001", identifier="acme-corp", name="Acme Corporation"))
        await s.create(_t(id="t-002", identifier="acme-labs", name="Acme Labs"))
        await s.create(_t(id="t-003", identifier="globex-corp", name="Globex Corporation"))

    async def test_identifier_match(self, s):
        await self._seed(s)
        results = await s.search("acme-corp")
        assert any(r.identifier == "acme-corp" for r in results)

    async def test_name_substring_match(self, s):
        await self._seed(s)
        results = await s.search("Globex")
        assert any("globex" in r.identifier for r in results)

    async def test_case_insensitive_search(self, s):
        await self._seed(s)
        results = await s.search("ACME")
        assert len(results) >= 2

    async def test_no_match_returns_empty(self, s):
        await self._seed(s)
        results = await s.search("zzznomatch")
        assert results == []

    async def test_limit_respected(self, s):
        await self._seed(s)
        results = await s.search("acme", limit=1)
        assert len(results) == 1

    async def test_exact_match_ranked_first(self, s):
        await self._seed(s)
        results = await s.search("acme-corp")
        assert results[0].identifier == "acme-corp"

    async def test_scan_limit_accepted(self, s):
        await self._seed(s)
        # _scan_limit should be accepted without error
        results = await s.search("acme", _scan_limit=50)
        assert isinstance(results, list)


# ───────────────── Helper methods: clear / get_all / statistics ───────────────


class TestHelpers:
    async def test_clear_empties_store(self, s):
        await s.create(_t())
        s.clear()
        assert await s.count() == 0

    async def test_get_all_returns_snapshot(self, s):
        t = _t()
        await s.create(t)
        all_tenants = s.get_all()
        assert "t-001" in all_tenants

    async def test_get_all_mutation_does_not_affect_store(self, s):
        t = _t()
        await s.create(t)
        snapshot = s.get_all()
        snapshot["injected"] = t  # type: ignore[assignment]
        assert "injected" not in s.get_all()

    async def test_statistics_structure(self, s):
        await s.create(_t(id="t-001", identifier="a", status=TenantStatus.ACTIVE))
        await s.create(_t(id="t-002", identifier="b", status=TenantStatus.SUSPENDED))
        stats = s.statistics()
        assert stats["total"] == 2
        assert "by_status" in stats
        assert stats["identifier_index_size"] == 2

    async def test_statistics_by_status_counts(self, s):
        await s.create(_t(id="t-001", identifier="a", status=TenantStatus.ACTIVE))
        await s.create(_t(id="t-002", identifier="b", status=TenantStatus.ACTIVE))
        stats = s.statistics()
        assert stats["by_status"].get("active", 0) == 2

    async def test_empty_statistics(self, s):
        stats = s.statistics()
        assert stats["total"] == 0
        assert stats["by_status"] == {}
