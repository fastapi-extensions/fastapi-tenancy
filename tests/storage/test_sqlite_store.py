"""SQLite-specific integration tests for :class:`~fastapi_tenancy.storage.database.SQLAlchemyTenantStore`."""  # noqa: E501

from __future__ import annotations

from datetime import UTC, datetime
import os
import socket
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import text

from fastapi_tenancy.core.exceptions import TenancyError, TenantNotFoundError
from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.storage.database import SQLAlchemyTenantStore
from fastapi_tenancy.utils.db_compat import DbDialect

if TYPE_CHECKING:
    from collections.abc import Callable


def _pg_up() -> bool:
    try:
        with socket.create_connection(("localhost", 5432), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.mark.integration
class TestSQLiteDialect:
    async def test_dialect_detected_as_sqlite(self, sqlite_store: SQLAlchemyTenantStore) -> None:
        assert sqlite_store._dialect == DbDialect.SQLITE

    async def test_static_pool_configured(self, sqlite_store: SQLAlchemyTenantStore) -> None:
        from sqlalchemy.pool import StaticPool  # noqa: PLC0415

        pool = sqlite_store._engine.pool
        assert isinstance(pool, StaticPool)

    async def test_two_separate_stores_isolated(self) -> None:
        """Two in-memory SQLite stores must not share the same database."""
        s1 = SQLAlchemyTenantStore("sqlite+aiosqlite:///:memory:")
        s2 = SQLAlchemyTenantStore("sqlite+aiosqlite:///:memory:")
        await s1.initialize()
        await s2.initialize()
        try:
            t = Tenant(
                id="iso-t1",
                identifier="iso-slug",
                name="Isolated",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            await s1.create(t)
            # s2 must not see t
            with pytest.raises(TenantNotFoundError):
                await s2.get_by_id("iso-t1")
        finally:
            await s1.close()
            await s2.close()


@pytest.mark.integration
class TestSQLiteGenericMetadataMerge:
    """Tests for the non-PostgreSQL read-modify-write metadata path."""

    async def test_metadata_merge_uses_generic_path(
        self,
        sqlite_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        # SQLite always takes the generic path
        t = await sqlite_store.create(make_tenant(metadata={"a": 1}))
        result = await sqlite_store.update_metadata(t.id, {"b": 2})
        assert result.metadata == {"a": 1, "b": 2}

    async def test_metadata_merge_preserves_original_on_missing(
        self, sqlite_store: SQLAlchemyTenantStore
    ) -> None:
        with pytest.raises(TenantNotFoundError):
            await sqlite_store.update_metadata("ghost", {"x": 1})

    async def test_corrupted_metadata_recovered_on_merge(
        self,
        sqlite_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await sqlite_store.create(make_tenant())
        async with sqlite_store._engine.begin() as conn:
            await conn.execute(
                text("UPDATE tenants SET tenant_metadata = 'INVALID' WHERE id = :id"),
                {"id": t.id},
            )
        result = await sqlite_store.update_metadata(t.id, {"recovered": True})
        assert result.metadata == {"recovered": True}


@pytest.mark.integration
class TestSQLiteBulkUpdateFallback:
    """Covers the non-RETURNING bulk_update_status path on SQLite."""

    async def test_bulk_status_update_all(
        self,
        sqlite_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t1 = await sqlite_store.create(make_tenant())
        t2 = await sqlite_store.create(make_tenant())
        result = await sqlite_store.bulk_update_status([t1.id, t2.id], TenantStatus.SUSPENDED)
        assert len(result) == 2
        assert all(t.status == TenantStatus.SUSPENDED for t in result)

    async def test_bulk_status_empty(self, sqlite_store: SQLAlchemyTenantStore) -> None:
        assert await sqlite_store.bulk_update_status([], TenantStatus.ACTIVE) == []

    async def test_bulk_status_skips_missing(
        self,
        sqlite_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await sqlite_store.create(make_tenant())
        result = await sqlite_store.bulk_update_status([t.id, "ghost"], TenantStatus.DELETED)
        assert len(result) == 1


@pytest.mark.integration
class TestSQLiteDeleteExceptionWrapper:
    """Verifies exception wrapping inside delete()."""

    async def test_delete_exception_wrapped_as_tenancy_error(
        self,
        sqlite_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        t = await sqlite_store.create(make_tenant())

        class _Broken:
            async def __aenter__(self) -> _Broken:
                raise RuntimeError("unexpected DB error")

            async def __aexit__(self, *_: object) -> bool:
                return False

        monkeypatch.setattr(sqlite_store, "_session_factory", lambda *a, **kw: _Broken())
        with pytest.raises(TenancyError, match="Failed to delete"):
            await sqlite_store.delete(t.id)


@pytest.mark.integration
class TestSQLiteNaiveDatetimeCoercion:
    async def test_naive_datetime_coerced_to_utc(
        self,
        sqlite_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await sqlite_store.create(make_tenant())
        async with sqlite_store._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE tenants "
                    "SET created_at = '2024-06-01 12:00:00', "
                    "    updated_at = '2024-06-01 12:00:00' "
                    "WHERE id = :id"
                ),
                {"id": t.id},
            )
        fetched = await sqlite_store.get_by_id(t.id)
        assert fetched.created_at.tzinfo is not None
        assert fetched.updated_at.tzinfo is not None


@pytest.mark.integration
class TestSearchDialect:
    """FIX: search() must work on SQLite (LIKE) and PostgreSQL (ILIKE)."""

    async def test_search_sqlite_case_insensitive(self, make_tenant: Callable[..., Tenant]) -> None:
        store = SQLAlchemyTenantStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        try:
            await store.create(make_tenant(identifier="alpha-corp", name="Alpha Corporation"))
            await store.create(make_tenant(identifier="beta-inc", name="Beta Incorporated"))
            await store.create(make_tenant(identifier="gamma-llc", name="Gamma LLC"))
            results = await store.search("alpha")
            ids = [r.identifier for r in results]
            assert "alpha-corp" in ids
            assert "beta-inc" not in ids
        finally:
            await store.close()

    async def test_search_sqlite_partial_match(self, make_tenant: Callable[..., Tenant]) -> None:
        store = SQLAlchemyTenantStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        try:
            await store.create(make_tenant(identifier="acme-widgets", name="Acme Widget Co"))
            await store.create(make_tenant(identifier="acme-anvils", name="Acme Anvil Co"))
            await store.create(make_tenant(identifier="staple-inc", name="Staple Inc"))
            results = await store.search("acme")
            ids = {r.identifier for r in results}
            assert "acme-widgets" in ids
            assert "acme-anvils" in ids
            assert "staple-inc" not in ids
        finally:
            await store.close()

    async def test_search_sqlite_metachar_safe(self, make_tenant: Callable[..., Tenant]) -> None:
        """LIKE metacharacters in the query must not expand to match all rows."""
        store = SQLAlchemyTenantStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        try:
            for i in range(5):
                await store.create(make_tenant(identifier=f"safe-tenant-{i}"))
            results = await store.search("%")
            assert len(results) == 0
        finally:
            await store.close()

    @pytest.mark.e2e
    async def test_search_postgres_ilike(self, make_tenant: Callable[..., Tenant]) -> None:
        if not _pg_up():
            pytest.skip("PostgreSQL not reachable")
        pg_url = os.getenv(
            "POSTGRES_URL",
            "postgresql+asyncpg://testing:Testing123!@localhost:5432/test_db",
        )
        store = SQLAlchemyTenantStore(pg_url, pool_size=2, max_overflow=2)
        await store.initialize()
        try:
            uid = uuid.uuid4().hex[:6]
            t1 = make_tenant(identifier=f"pg-search-{uid}-alpha")
            t2 = make_tenant(identifier=f"pg-search-{uid}-beta")
            await store.create(t1)
            await store.create(t2)
            results = await store.search(f"pg-search-{uid}")
            ids = {r.identifier for r in results}
            assert t1.identifier in ids
            assert t2.identifier in ids
        finally:
            try:
                async with store._engine.begin() as conn:
                    await conn.execute(text("TRUNCATE TABLE tenants RESTART IDENTITY CASCADE"))
            except Exception:
                pass
            await store.close()
