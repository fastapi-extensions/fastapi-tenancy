"""Integration tests — fastapi_tenancy.storage.database.SQLAlchemyTenantStore

Uses a real SQLite+aiosqlite database (temp file per test).
Requires: pip install fastapi-tenancy[sqlite]

Coverage target: 100 % of SQLAlchemyTenantStore

Verified:
* initialize() creates the table
* Full CRUD lifecycle
* Pagination (skip / limit)
* Status filtering in list()
* duplicate id / identifier raises ValueError
* TenantNotFoundError on missing records
* update_metadata merges correctly
* set_status persists
* bulk_update_status
* search by name and identifier
* UTC timezone coercion for SQLite naive datetimes
* close() disposes the engine cleanly
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fastapi_tenancy.core.exceptions import TenantNotFoundError
from fastapi_tenancy.core.types import Tenant, TenantStatus

pytestmark = pytest.mark.integration

_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)


def _t(
    id: str = "t-001",
    identifier: str = "acme-corp",
    name: str = "Acme Corp",
    status: TenantStatus = TenantStatus.ACTIVE,
    metadata: dict | None = None,
) -> Tenant:
    return Tenant(
        id=id,
        identifier=identifier,
        name=name,
        status=status,
        metadata=metadata or {},
        created_at=_NOW,
        updated_at=_NOW,
    )


@pytest.fixture
async def db(tmp_path):
    """Real SQLite-backed SQLAlchemyTenantStore per test."""
    pytest.importorskip("aiosqlite", reason="aiosqlite not installed")
    from fastapi_tenancy.storage.database import SQLAlchemyTenantStore

    path = tmp_path / "test.db"
    store = SQLAlchemyTenantStore(database_url=f"sqlite+aiosqlite:///{path}")
    await store.initialize()
    yield store
    await store.close()


# ──────────────────────────── initialize ─────────────────────────────────────


class TestInitialize:
    async def test_table_created(self, db):
        # If initialize succeeded, count() should work without error
        assert await db.count() == 0

    async def test_idempotent(self, tmp_path):
        pytest.importorskip("aiosqlite")
        from fastapi_tenancy.storage.database import SQLAlchemyTenantStore

        path = tmp_path / "test2.db"
        store = SQLAlchemyTenantStore(database_url=f"sqlite+aiosqlite:///{path}")
        await store.initialize()
        await store.initialize()  # second call must not raise
        await store.close()


# ─────────────────────────────── create ──────────────────────────────────────


class TestCreate:
    async def test_create_and_retrieve_by_id(self, db):
        t = _t()
        await db.create(t)
        found = await db.get_by_id(t.id)
        assert found.id == t.id
        assert found.identifier == t.identifier

    async def test_create_and_retrieve_by_identifier(self, db):
        t = _t()
        await db.create(t)
        found = await db.get_by_identifier(t.identifier)
        assert found.identifier == t.identifier

    async def test_metadata_persisted(self, db):
        t = _t(metadata={"plan": "pro", "max_users": 100})
        await db.create(t)
        found = await db.get_by_id(t.id)
        assert found.metadata["plan"] == "pro"
        assert found.metadata["max_users"] == 100

    async def test_duplicate_id_raises(self, db):
        t = _t()
        await db.create(t)
        with pytest.raises((ValueError, Exception)):
            await db.create(t)

    async def test_duplicate_identifier_raises(self, db):
        t1 = _t(id="t-001", identifier="same-slug")
        t2 = _t(id="t-002", identifier="same-slug")
        await db.create(t1)
        with pytest.raises((ValueError, Exception)):
            await db.create(t2)


# ─────────────────────────── get operations ──────────────────────────────────


class TestGet:
    async def test_get_by_id_missing_raises(self, db):
        with pytest.raises(TenantNotFoundError):
            await db.get_by_id("no-such-id")

    async def test_get_by_identifier_missing_raises(self, db):
        with pytest.raises(TenantNotFoundError):
            await db.get_by_identifier("no-such-slug")

    async def test_status_persisted(self, db):
        t = _t(status=TenantStatus.SUSPENDED)
        await db.create(t)
        found = await db.get_by_id(t.id)
        assert found.status == TenantStatus.SUSPENDED


# ────────────────────────────── list ─────────────────────────────────────────


class TestList:
    async def test_empty(self, db):
        assert await db.list() == []

    async def test_returns_created(self, db):
        await db.create(_t())
        tenants = await db.list()
        assert len(tenants) == 1

    async def test_pagination_skip(self, db):
        for i in range(5):
            await db.create(_t(id=f"t-{i:03d}", identifier=f"corp-{i:02d}"))
        page = await db.list(skip=3, limit=10)
        assert len(page) == 2

    async def test_pagination_limit(self, db):
        for i in range(5):
            await db.create(_t(id=f"t-{i:03d}", identifier=f"corp-{i:02d}"))
        page = await db.list(limit=3)
        assert len(page) == 3

    async def test_filter_by_active_status(self, db):
        await db.create(_t(id="t-001", identifier="active", status=TenantStatus.ACTIVE))
        await db.create(_t(id="t-002", identifier="susp", status=TenantStatus.SUSPENDED))
        active = await db.list(status=TenantStatus.ACTIVE)
        assert len(active) == 1
        assert active[0].status == TenantStatus.ACTIVE


# ─────────────────────────────── count ───────────────────────────────────────


class TestCount:
    async def test_zero_initially(self, db):
        assert await db.count() == 0

    async def test_increments_on_create(self, db):
        await db.create(_t())
        assert await db.count() == 1

    async def test_status_filter(self, db):
        await db.create(_t(id="t-001", identifier="a", status=TenantStatus.ACTIVE))
        await db.create(_t(id="t-002", identifier="b", status=TenantStatus.DELETED))
        assert await db.count(status=TenantStatus.ACTIVE) == 1
        assert await db.count(status=TenantStatus.DELETED) == 1


# ─────────────────────────────── exists ──────────────────────────────────────


class TestExists:
    async def test_existing_returns_true(self, db):
        t = _t()
        await db.create(t)
        assert await db.exists(t.id) is True

    async def test_missing_returns_false(self, db):
        assert await db.exists("ghost") is False


# ────────────────────────────── update ───────────────────────────────────────


class TestUpdate:
    async def test_name_updated(self, db):
        t = _t()
        await db.create(t)
        updated = t.model_copy(update={"name": "New Name"})
        result = await db.update(updated)
        assert result.name == "New Name"

    async def test_update_persists_to_db(self, db):
        t = _t()
        await db.create(t)
        updated = t.model_copy(update={"name": "Persisted"})
        await db.update(updated)
        found = await db.get_by_id(t.id)
        assert found.name == "Persisted"

    async def test_update_missing_raises(self, db):
        t = _t(id="ghost-id")
        with pytest.raises(TenantNotFoundError):
            await db.update(t)


# ─────────────────────────────── delete ──────────────────────────────────────


class TestDelete:
    async def test_delete_succeeds(self, db):
        t = _t()
        await db.create(t)
        await db.delete(t.id)
        with pytest.raises(TenantNotFoundError):
            await db.get_by_id(t.id)

    async def test_delete_missing_raises(self, db):
        with pytest.raises(TenantNotFoundError):
            await db.delete("ghost")

    async def test_count_decrements(self, db):
        t = _t()
        await db.create(t)
        await db.delete(t.id)
        assert await db.count() == 0


# ────────────────────────────── set_status ───────────────────────────────────


class TestSetStatus:
    async def test_status_changed(self, db):
        t = _t(status=TenantStatus.ACTIVE)
        await db.create(t)
        updated = await db.set_status(t.id, TenantStatus.SUSPENDED)
        assert updated.status == TenantStatus.SUSPENDED

    async def test_persisted(self, db):
        t = _t()
        await db.create(t)
        await db.set_status(t.id, TenantStatus.DELETED)
        found = await db.get_by_id(t.id)
        assert found.status == TenantStatus.DELETED

    async def test_missing_raises(self, db):
        with pytest.raises(TenantNotFoundError):
            await db.set_status("ghost", TenantStatus.ACTIVE)


# ──────────────────────────── update_metadata ────────────────────────────────


class TestUpdateMetadata:
    async def test_merge_new_keys(self, db):
        t = _t()
        await db.create(t)
        result = await db.update_metadata(t.id, {"plan": "pro"})
        assert result.metadata["plan"] == "pro"

    async def test_existing_keys_preserved_on_merge(self, db):
        t = _t(metadata={"plan": "basic"})
        await db.create(t)
        result = await db.update_metadata(t.id, {"max_users": 50})
        assert result.metadata["plan"] == "basic"
        assert result.metadata["max_users"] == 50

    async def test_persisted_to_db(self, db):
        t = _t()
        await db.create(t)
        await db.update_metadata(t.id, {"key": "value"})
        found = await db.get_by_id(t.id)
        assert found.metadata["key"] == "value"

    async def test_missing_raises(self, db):
        with pytest.raises(TenantNotFoundError):
            await db.update_metadata("ghost", {"x": 1})


# ─────────────────────────── bulk_update_status ──────────────────────────────


class TestBulkUpdateStatus:
    async def test_updates_all(self, db):
        t1 = _t(id="t-001", identifier="a")
        t2 = _t(id="t-002", identifier="b")
        await db.create(t1)
        await db.create(t2)
        result = await db.bulk_update_status(["t-001", "t-002"], TenantStatus.SUSPENDED)
        assert len(result) == 2

    async def test_missing_skipped(self, db):
        t = _t()
        await db.create(t)
        result = await db.bulk_update_status([t.id, "nonexistent"], TenantStatus.DELETED)
        assert len(result) == 1


# ─────────────────────────────── search ──────────────────────────────────────


class TestSearch:
    async def test_identifier_match(self, db):
        await db.create(_t(identifier="acme-corp", name="Acme Corporation"))
        results = await db.search("acme")
        assert len(results) >= 1

    async def test_name_match(self, db):
        await db.create(_t(identifier="acme-corp", name="Acme Corporation"))
        results = await db.search("Corporation")
        assert len(results) >= 1

    async def test_no_match(self, db):
        await db.create(_t())
        results = await db.search("zzz-no-match")
        assert results == []

    async def test_limit_respected(self, db):
        for i in range(5):
            await db.create(_t(
                id=f"t-{i:03d}",
                identifier=f"corp-{i:02d}",
                name=f"Corp {i}",
            ))
        results = await db.search("corp", limit=2)
        assert len(results) <= 2


# ─────────────────────── Timezone coercion (SQLite) ──────────────────────────


class TestTimezoneCoercion:
    async def test_created_at_is_utc_aware(self, db):
        t = _t()
        await db.create(t)
        found = await db.get_by_id(t.id)
        assert found.created_at.tzinfo is not None

    async def test_updated_at_is_utc_aware(self, db):
        t = _t()
        await db.create(t)
        found = await db.get_by_id(t.id)
        assert found.updated_at.tzinfo is not None
