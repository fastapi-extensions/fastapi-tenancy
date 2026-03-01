"""Tests for fastapi_tenancy.storage.database — SQLAlchemyTenantStore.

Runs against SQLite+aiosqlite (in-memory) for fast, dependency-free execution.
All tests are marked `integration` to distinguish from pure-unit tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest

from fastapi_tenancy.core.exceptions import TenantNotFoundError
from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.storage.database import SQLAlchemyTenantStore


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new(
    *,
    identifier: str | None = None,
    name: str | None = None,
    status: TenantStatus = TenantStatus.ACTIVE,
    metadata: dict | None = None,
) -> Tenant:
    uid = uuid.uuid4().hex[:12]
    ts = datetime.now(UTC)
    return Tenant(
        id=f"t-{uid}",
        identifier=identifier or f"tenant-{uid}",
        name=name or f"Tenant {uid}",
        status=status,
        metadata=metadata or {},
        created_at=ts,
        updated_at=ts,
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestInitializeAndClose:
    async def test_initialize_creates_tables(self):
        s = SQLAlchemyTenantStore("sqlite+aiosqlite:///:memory:")
        await s.initialize()
        # Should not raise
        count = await s.count()
        assert count == 0
        await s.close()

    async def test_initialize_idempotent(self):
        s = SQLAlchemyTenantStore("sqlite+aiosqlite:///:memory:")
        await s.initialize()
        await s.initialize()  # second call must be safe
        await s.close()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


class TestCreate:
    async def test_create_persists(self, sqlite_store: SQLAlchemyTenantStore):
        t = _new(identifier="ac-one")
        created = await sqlite_store.create(t)
        assert created.id == t.id
        fetched = await sqlite_store.get_by_id(t.id)
        assert fetched.identifier == "ac-one"

    async def test_create_duplicate_id_raises(self, sqlite_store: SQLAlchemyTenantStore):
        t = _new(identifier="dup-id-test")
        await sqlite_store.create(t)
        with pytest.raises(Exception):
            await sqlite_store.create(t)

    async def test_create_stores_metadata(self, sqlite_store: SQLAlchemyTenantStore):
        t = _new(identifier="meta-test", metadata={"plan": "pro", "seats": 10})
        await sqlite_store.create(t)
        fetched = await sqlite_store.get_by_id(t.id)
        assert fetched.metadata["plan"] == "pro"
        assert fetched.metadata["seats"] == 10


class TestGetByIdentifier:
    async def test_found(self, sqlite_store: SQLAlchemyTenantStore):
        t = _new(identifier="by-slug-test")
        await sqlite_store.create(t)
        result = await sqlite_store.get_by_identifier("by-slug-test")
        assert result.id == t.id

    async def test_not_found_raises(self, sqlite_store: SQLAlchemyTenantStore):
        with pytest.raises(TenantNotFoundError):
            await sqlite_store.get_by_identifier("nonexistent-slug")


class TestUpdate:
    async def test_update_name(self, sqlite_store: SQLAlchemyTenantStore):
        t = _new(identifier="upd-one")
        await sqlite_store.create(t)
        updated = t.model_copy(update={"name": "Updated Name"})
        result = await sqlite_store.update(updated)
        assert result.name == "Updated Name"

    async def test_update_refreshes_updated_at(self, sqlite_store: SQLAlchemyTenantStore):
        t = _new(identifier="upd-ts")
        created = await sqlite_store.create(t)
        updated = created.model_copy(update={"name": "New"})
        result = await sqlite_store.update(updated)
        assert result.updated_at >= created.updated_at

    async def test_update_not_found_raises(self, sqlite_store: SQLAlchemyTenantStore):
        ghost = _new(identifier="ghost-upd")
        with pytest.raises(TenantNotFoundError):
            await sqlite_store.update(ghost)


class TestDelete:
    async def test_hard_delete(self, sqlite_store: SQLAlchemyTenantStore):
        t = _new(identifier="del-one")
        await sqlite_store.create(t)
        await sqlite_store.delete(t.id)
        with pytest.raises(TenantNotFoundError):
            await sqlite_store.get_by_id(t.id)

    async def test_delete_not_found_raises(self, sqlite_store: SQLAlchemyTenantStore):
        with pytest.raises(TenantNotFoundError):
            await sqlite_store.delete("nonexistent-id")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


class TestList:
    async def test_returns_tenants(self, sqlite_store: SQLAlchemyTenantStore):
        t1 = _new(identifier="lst-one")
        t2 = _new(identifier="lst-two")
        await sqlite_store.create(t1)
        await sqlite_store.create(t2)
        result = await sqlite_store.list()
        ids = {t.id for t in result}
        assert t1.id in ids
        assert t2.id in ids

    async def test_filter_by_status(self, sqlite_store: SQLAlchemyTenantStore):
        active = _new(identifier="flt-act")
        suspended = _new(identifier="flt-susp", status=TenantStatus.SUSPENDED)
        await sqlite_store.create(active)
        await sqlite_store.create(suspended)
        result = await sqlite_store.list(status=TenantStatus.SUSPENDED)
        assert all(t.status == TenantStatus.SUSPENDED for t in result)

    async def test_pagination(self, sqlite_store: SQLAlchemyTenantStore):
        for i in range(5):
            await sqlite_store.create(_new(identifier=f"pag-{i:03d}"))
        page1 = await sqlite_store.list(skip=0, limit=3)
        page2 = await sqlite_store.list(skip=3, limit=3)
        assert len(page1) == 3
        assert len(page2) == 2
        ids1 = {t.id for t in page1}
        ids2 = {t.id for t in page2}
        assert ids1.isdisjoint(ids2)


class TestCount:
    async def test_total(self, sqlite_store: SQLAlchemyTenantStore):
        await sqlite_store.create(_new(identifier="cnt-one"))
        await sqlite_store.create(_new(identifier="cnt-two"))
        assert await sqlite_store.count() == 2

    async def test_filtered(self, sqlite_store: SQLAlchemyTenantStore):
        await sqlite_store.create(_new(identifier="cnt-a", status=TenantStatus.ACTIVE))
        await sqlite_store.create(_new(identifier="cnt-s", status=TenantStatus.SUSPENDED))
        assert await sqlite_store.count(status=TenantStatus.ACTIVE) == 1
        assert await sqlite_store.count(status=TenantStatus.SUSPENDED) == 1


class TestExists:
    async def test_existing(self, sqlite_store: SQLAlchemyTenantStore):
        t = _new(identifier="ex-one")
        await sqlite_store.create(t)
        assert await sqlite_store.exists(t.id) is True

    async def test_nonexistent(self, sqlite_store: SQLAlchemyTenantStore):
        assert await sqlite_store.exists("ghost") is False


# ---------------------------------------------------------------------------
# Status operations
# ---------------------------------------------------------------------------


class TestSetStatus:
    async def test_set_suspended(self, sqlite_store: SQLAlchemyTenantStore):
        t = _new(identifier="sts-one")
        await sqlite_store.create(t)
        result = await sqlite_store.set_status(t.id, TenantStatus.SUSPENDED)
        assert result.status == TenantStatus.SUSPENDED
        fetched = await sqlite_store.get_by_id(t.id)
        assert fetched.status == TenantStatus.SUSPENDED

    async def test_not_found_raises(self, sqlite_store: SQLAlchemyTenantStore):
        with pytest.raises(TenantNotFoundError):
            await sqlite_store.set_status("ghost", TenantStatus.ACTIVE)


class TestBulkUpdateStatus:
    async def test_updates_multiple(self, sqlite_store: SQLAlchemyTenantStore):
        t1 = _new(identifier="bulk-one")
        t2 = _new(identifier="bulk-two")
        await sqlite_store.create(t1)
        await sqlite_store.create(t2)
        results = await sqlite_store.bulk_update_status(
            [t1.id, t2.id], TenantStatus.SUSPENDED
        )
        assert len(results) == 2
        assert all(t.status == TenantStatus.SUSPENDED for t in results)

    async def test_skips_missing(self, sqlite_store: SQLAlchemyTenantStore):
        t = _new(identifier="bulk-skip")
        await sqlite_store.create(t)
        results = await sqlite_store.bulk_update_status([t.id, "ghost"], TenantStatus.DELETED)
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


class TestUpdateMetadata:
    async def test_merge(self, sqlite_store: SQLAlchemyTenantStore):
        t = _new(identifier="meta-merge", metadata={"existing": "val"})
        await sqlite_store.create(t)
        result = await sqlite_store.update_metadata(t.id, {"new_key": "new_val"})
        assert result.metadata["existing"] == "val"
        assert result.metadata["new_key"] == "new_val"

    async def test_overwrite(self, sqlite_store: SQLAlchemyTenantStore):
        t = _new(identifier="meta-overwrite", metadata={"plan": "free"})
        await sqlite_store.create(t)
        result = await sqlite_store.update_metadata(t.id, {"plan": "pro"})
        assert result.metadata["plan"] == "pro"

    async def test_not_found_raises(self, sqlite_store: SQLAlchemyTenantStore):
        with pytest.raises(TenantNotFoundError):
            await sqlite_store.update_metadata("ghost", {"k": "v"})


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearch:
    async def test_identifier_match(self, sqlite_store: SQLAlchemyTenantStore):
        t = _new(identifier="findme-corp")
        await sqlite_store.create(t)
        results = await sqlite_store.search("findme")
        assert any(r.id == t.id for r in results)

    async def test_name_match(self, sqlite_store: SQLAlchemyTenantStore):
        t = _new(identifier="srch-one", name="Unique Searchable Name")
        await sqlite_store.create(t)
        results = await sqlite_store.search("Unique Searchable")
        assert any(r.id == t.id for r in results)

    async def test_limit_respected(self, sqlite_store: SQLAlchemyTenantStore):
        for i in range(10):
            await sqlite_store.create(_new(identifier=f"limitsrch-{i:02d}"))
        results = await sqlite_store.search("limitsrch", limit=3)
        assert len(results) <= 3

    async def test_no_match_returns_empty(self, sqlite_store: SQLAlchemyTenantStore):
        results = await sqlite_store.search("zzznotenanthasthisname")
        assert results == []


# ---------------------------------------------------------------------------
# get_by_ids
# ---------------------------------------------------------------------------


class TestGetByIds:
    async def test_all_found(self, sqlite_store: SQLAlchemyTenantStore):
        t1 = _new(identifier="gbi-one")
        t2 = _new(identifier="gbi-two")
        await sqlite_store.create(t1)
        await sqlite_store.create(t2)
        result = await sqlite_store.get_by_ids([t1.id, t2.id])
        ids = {t.id for t in result}
        assert t1.id in ids
        assert t2.id in ids

    async def test_missing_silently_skipped(self, sqlite_store: SQLAlchemyTenantStore):
        t = _new(identifier="gbi-skip")
        await sqlite_store.create(t)
        result = await sqlite_store.get_by_ids([t.id, "ghost-id"])
        assert len(result) == 1
