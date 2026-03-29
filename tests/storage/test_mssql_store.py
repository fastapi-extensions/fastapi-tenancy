"""MSSQL-specific integration tests for :class:`~fastapi_tenancy.storage.database.SQLAlchemyTenantStore`."""  # noqa: E501

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy.pool import StaticPool

from fastapi_tenancy.core.exceptions import TenantNotFoundError
from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.utils.db_compat import DbDialect

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi_tenancy.storage.database import SQLAlchemyTenantStore


@pytest.mark.e2e
class TestMSSQLDialect:
    async def test_dialect_detected(self, mssql_store: SQLAlchemyTenantStore) -> None:
        assert mssql_store._dialect == DbDialect.MSSQL

    async def test_not_static_pool(self, mssql_store: SQLAlchemyTenantStore) -> None:
        assert not isinstance(mssql_store._engine.pool, StaticPool)


@pytest.mark.e2e
class TestMSSQLCRUD:
    async def test_create_and_get_by_identifier(
        self,
        mssql_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await mssql_store.create(make_tenant(identifier="mssql-slug"))
        fetched = await mssql_store.get_by_identifier("mssql-slug")
        assert fetched.id == t.id

    async def test_delete_makes_tenant_unreachable(
        self,
        mssql_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await mssql_store.create(make_tenant())
        await mssql_store.delete(t.id)
        with pytest.raises(TenantNotFoundError):
            await mssql_store.get_by_id(t.id)

    async def test_update_name(
        self,
        mssql_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await mssql_store.create(make_tenant(name="Before"))
        result = await mssql_store.update(t.model_copy(update={"name": "After"}))
        assert result.name == "After"


@pytest.mark.e2e
class TestMSSQLMetadataMerge:
    async def test_merge_uses_generic_path(
        self,
        mssql_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await mssql_store.create(make_tenant(metadata={"x": 10}))
        result = await mssql_store.update_metadata(t.id, {"y": 20})
        assert result.metadata == {"x": 10, "y": 20}

    async def test_missing_raises_not_found(self, mssql_store: SQLAlchemyTenantStore) -> None:
        with pytest.raises(TenantNotFoundError):
            await mssql_store.update_metadata("ghost", {"k": "v"})


@pytest.mark.e2e
class TestMSSQLBulkUpdate:
    async def test_bulk_update_all(
        self,
        mssql_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t1 = await mssql_store.create(make_tenant())
        t2 = await mssql_store.create(make_tenant())
        result = await mssql_store.bulk_update_status([t1.id, t2.id], TenantStatus.SUSPENDED)
        assert len(result) == 2
        assert all(t.status == TenantStatus.SUSPENDED for t in result)

    async def test_bulk_empty(self, mssql_store: SQLAlchemyTenantStore) -> None:
        assert await mssql_store.bulk_update_status([], TenantStatus.ACTIVE) == []


@pytest.mark.e2e
class TestMSSQLSearch:
    async def test_ilike_search(
        self,
        mssql_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        await mssql_store.create(make_tenant(identifier="ms-corp", name="MsCorp"))
        results = await mssql_store.search("ms-corp")
        assert any(r.identifier == "ms-corp" for r in results)


class TestMSSQLDestroyTenantQuotename:
    """FIX: QUOTENAME() wraps every identifier in the dynamic DROP TABLE SQL.

    Before the fix:
        ``'DROP TABLE [' + :schema + '].[' + TABLE_NAME + '];'`` — TABLE_NAME
        was concatenated raw (no QUOTENAME).

    After the fix:
        ``QUOTENAME(TABLE_SCHEMA) + N'.' + QUOTENAME(TABLE_NAME)`` — both
        identifiers are properly bracket-quoted by the database engine,
        providing defence-in-depth regardless of the validator.
    """

    def test_mssql_destroy_sql_contains_quotename(self) -> None:
        """The class-level Lua script / SQL text in schema.py must use QUOTENAME."""
        import inspect  # noqa: PLC0415

        from fastapi_tenancy.isolation import schema as schema_mod  # noqa: PLC0415

        source = inspect.getsource(schema_mod)

        # QUOTENAME must appear in the destroy_tenant path.
        assert "QUOTENAME" in source, (
            "MSSQL dynamic DROP TABLE SQL must use QUOTENAME() for defence-in-depth"
        )

    def test_mssql_destroy_sql_no_raw_concatenation(self) -> None:
        """The old raw concatenation pattern must no longer exist in the source."""
        import inspect  # noqa: PLC0415
        import re  # noqa: PLC0415

        from fastapi_tenancy.isolation import schema as schema_mod  # noqa: PLC0415

        source = inspect.getsource(schema_mod)

        # The old vulnerable pattern concatenated ':schema' directly into the
        # DROP TABLE string: "DROP TABLE [' + :schema + '].["
        bad_pattern = re.compile(r"DROP TABLE \[' \+ :schema \+ '\]\.\[")
        assert not bad_pattern.search(source), (
            "Old raw-concatenation pattern found in destroy_tenant MSSQL path"
        )
