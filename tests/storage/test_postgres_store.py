"""PostgreSQL-specific integration tests for :class:`~fastapi_tenancy.storage.database.SQLAlchemyTenantStore`."""  # noqa: E501

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from fastapi_tenancy.core.exceptions import TenantNotFoundError
from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.utils.db_compat import DbDialect

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi_tenancy.storage.database import SQLAlchemyTenantStore


@pytest.mark.e2e
class TestPostgreSQLDialect:
    async def test_dialect_detected(self, postgres_store: SQLAlchemyTenantStore) -> None:
        assert postgres_store._dialect == DbDialect.POSTGRESQL

    async def test_connection_pool_not_static(self, postgres_store: SQLAlchemyTenantStore) -> None:
        from sqlalchemy.pool import StaticPool  # noqa: PLC0415

        assert not isinstance(postgres_store._engine.pool, StaticPool)


@pytest.mark.e2e
class TestPostgreSQLAtomicMetadataMerge:
    """Covers ``_update_metadata_pg`` — the JSONB ``||`` operator path."""

    async def test_basic_merge(
        self,
        postgres_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await postgres_store.create(make_tenant(metadata={"a": 1}))
        result = await postgres_store.update_metadata(t.id, {"b": 2})
        assert result.metadata == {"a": 1, "b": 2}

    async def test_overwrite_existing_key(
        self,
        postgres_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await postgres_store.create(make_tenant(metadata={"k": "old"}))
        result = await postgres_store.update_metadata(t.id, {"k": "new"})
        assert result.metadata["k"] == "new"

    async def test_missing_tenant_raises_not_found(
        self, postgres_store: SQLAlchemyTenantStore
    ) -> None:
        with pytest.raises(TenantNotFoundError):
            await postgres_store.update_metadata("ghost", {"x": 1})

    async def test_concurrent_metadata_merges_no_lost_update(
        self,
        postgres_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        """20 concurrent metadata patches must all persist without a lost update."""
        t = await postgres_store.create(make_tenant())
        patches = [{f"key_{i}": i} for i in range(20)]
        await asyncio.gather(*(postgres_store.update_metadata(t.id, p) for p in patches))
        final = await postgres_store.get_by_id(t.id)
        # Every distinct key must be present
        assert len(final.metadata) == 20


@pytest.mark.e2e
class TestPostgreSQLBulkUpdateReturning:
    """Covers the ``UPDATE … RETURNING`` path used only on PostgreSQL."""

    async def test_bulk_update_uses_returning(
        self,
        postgres_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t1 = await postgres_store.create(make_tenant())
        t2 = await postgres_store.create(make_tenant())
        result = await postgres_store.bulk_update_status([t1.id, t2.id], TenantStatus.SUSPENDED)
        assert len(result) == 2
        assert all(t.status == TenantStatus.SUSPENDED for t in result)

    async def test_bulk_empty_returns_empty(self, postgres_store: SQLAlchemyTenantStore) -> None:
        assert await postgres_store.bulk_update_status([], TenantStatus.ACTIVE) == []

    async def test_bulk_partial_match(
        self,
        postgres_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await postgres_store.create(make_tenant())
        result = await postgres_store.bulk_update_status(
            [t.id, "ghost-a", "ghost-b"], TenantStatus.DELETED
        )
        assert len(result) == 1
        assert result[0].status == TenantStatus.DELETED


@pytest.mark.e2e
class TestPostgreSQLSearch:
    async def test_ilike_case_insensitive(
        self,
        postgres_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        await postgres_store.create(make_tenant(identifier="pg-acme", name="Acme Corp"))
        results = await postgres_store.search("ACME")
        assert any(r.identifier == "pg-acme" for r in results)

    async def test_search_returns_alphabetical_order(
        self,
        postgres_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        await postgres_store.create(make_tenant(identifier="pg-zz", name="ZZ Corp"))
        await postgres_store.create(make_tenant(identifier="pg-aa", name="AA Corp"))
        results = await postgres_store.search("pg-")
        ids = [r.identifier for r in results]
        assert ids == sorted(ids)
