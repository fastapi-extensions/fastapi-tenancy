"""MySQL-specific integration tests for :class:`~fastapi_tenancy.storage.database.SQLAlchemyTenantStore`."""  # noqa: E501

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from fastapi_tenancy.core.exceptions import TenantNotFoundError
from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.utils.db_compat import DbDialect

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi_tenancy.storage.database import SQLAlchemyTenantStore


@pytest.mark.e2e
class TestMySQLDialect:
    async def test_dialect_detected(self, mysql_store: SQLAlchemyTenantStore) -> None:
        assert mysql_store._dialect == DbDialect.MYSQL

    async def test_not_static_pool(self, mysql_store: SQLAlchemyTenantStore) -> None:
        from sqlalchemy.pool import StaticPool  # noqa: PLC0415

        assert not isinstance(mysql_store._engine.pool, StaticPool)


@pytest.mark.e2e
class TestMySQLCRUD:
    async def test_create_and_read(
        self,
        mysql_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await mysql_store.create(make_tenant(name="MySQL Tenant"))
        fetched = await mysql_store.get_by_id(t.id)
        assert fetched.name == "MySQL Tenant"

    async def test_utf8_name_roundtrip(
        self,
        mysql_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        # utf8mb4_unicode_ci collation must handle non-ASCII correctly
        t = await mysql_store.create(make_tenant(name="テナント株式会社"))
        fetched = await mysql_store.get_by_id(t.id)
        assert fetched.name == "テナント株式会社"

    async def test_delete_and_not_found(
        self,
        mysql_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await mysql_store.create(make_tenant())
        await mysql_store.delete(t.id)
        with pytest.raises(TenantNotFoundError):
            await mysql_store.get_by_id(t.id)


@pytest.mark.e2e
class TestMySQLMetadataMerge:
    """MySQL takes the generic read-modify-write path."""

    async def test_merge_adds_keys(
        self,
        mysql_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await mysql_store.create(make_tenant(metadata={"a": 1}))
        result = await mysql_store.update_metadata(t.id, {"b": 2})
        assert result.metadata == {"a": 1, "b": 2}

    async def test_overwrite_existing_key(
        self,
        mysql_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await mysql_store.create(make_tenant(metadata={"plan": "free"}))
        result = await mysql_store.update_metadata(t.id, {"plan": "pro"})
        assert result.metadata["plan"] == "pro"

    async def test_missing_raises_not_found(self, mysql_store: SQLAlchemyTenantStore) -> None:
        with pytest.raises(TenantNotFoundError):
            await mysql_store.update_metadata("ghost", {"k": "v"})


@pytest.mark.e2e
class TestMySQLBulkUpdate:
    """MySQL takes the non-RETURNING fallback path."""

    async def test_bulk_updates_all(
        self,
        mysql_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t1 = await mysql_store.create(make_tenant())
        t2 = await mysql_store.create(make_tenant())
        result = await mysql_store.bulk_update_status([t1.id, t2.id], TenantStatus.SUSPENDED)
        assert len(result) == 2

    async def test_bulk_empty(self, mysql_store: SQLAlchemyTenantStore) -> None:
        assert await mysql_store.bulk_update_status([], TenantStatus.ACTIVE) == []


@pytest.mark.e2e
class TestMySQLSearch:
    async def test_case_insensitive_search(
        self,
        mysql_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        await mysql_store.create(make_tenant(identifier="my-corp", name="MyCorp"))
        results = await mysql_store.search("MYCORP")
        assert any(r.identifier == "my-corp" for r in results)
