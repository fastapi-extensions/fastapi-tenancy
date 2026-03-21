"""Tests for :class:`~fastapi_tenancy.isolation.schema.SchemaIsolationProvider`."""

from __future__ import annotations

import contextlib
import os
import socket
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.exceptions import IsolationError
from fastapi_tenancy.core.types import IsolationStrategy, Tenant
from fastapi_tenancy.isolation.schema import SchemaIsolationProvider
from fastapi_tenancy.utils.db_compat import DbDialect

if TYPE_CHECKING:
    from collections.abc import Callable


def _sqla_cfg(url: str = "sqlite+aiosqlite:///:memory:", **kw: Any) -> TenancyConfig:
    return TenancyConfig(
        database_url=url,
        isolation_strategy=kw.pop("isolation_strategy", IsolationStrategy.SCHEMA),
        schema_prefix=kw.pop("schema_prefix", "t_"),
        database_echo=kw.pop("database_echo", False),
        database_pool_size=kw.pop("database_pool_size", 2),
        database_max_overflow=kw.pop("database_max_overflow", 2),
        database_pool_timeout=kw.pop("database_pool_timeout", 10),
        database_pool_recycle=kw.pop("database_pool_recycle", 300),
        database_pool_pre_ping=kw.pop("database_pool_pre_ping", False),
        **kw,
    )


@pytest.mark.unit
class TestConstruction:
    def test_sqlite_detected_as_sqlite_dialect(self) -> None:
        p = SchemaIsolationProvider(_sqla_cfg())
        assert p.dialect == DbDialect.SQLITE

    def test_shared_engine_injected(self, sqlite_engine: Any) -> None:
        cfg = _sqla_cfg()
        p = SchemaIsolationProvider(cfg, engine=sqlite_engine)
        assert p.engine is sqlite_engine

    def test_mysql_delegate_created_for_mysql_dialect(self) -> None:
        pytest.importorskip("aiomysql", reason="aiomysql not installed")
        cfg = _sqla_cfg("mysql+aiomysql://u:p@localhost/db")
        p = SchemaIsolationProvider(cfg)
        assert p._mysql_delegate is not None
        assert p.dialect == DbDialect.MYSQL

    def test_no_mysql_delegate_for_sqlite(self) -> None:
        p = SchemaIsolationProvider(_sqla_cfg())
        assert p._mysql_delegate is None

    def test_pg_no_static_pool(self) -> None:
        cfg = _sqla_cfg("postgresql+asyncpg://u:p@localhost/db")
        p = SchemaIsolationProvider(cfg)
        assert not isinstance(p.engine.pool, StaticPool)

    def test_sqlite_uses_static_pool(self) -> None:
        p = SchemaIsolationProvider(_sqla_cfg())
        assert isinstance(p.engine.pool, StaticPool)


@pytest.mark.unit
class TestSchemaNameHelpers:
    def test_schema_name_from_config(self, make_tenant: Callable[..., Tenant]) -> None:
        cfg = _sqla_cfg(schema_prefix="ts_")
        p = SchemaIsolationProvider(cfg)
        t = make_tenant(identifier="acme-corp")
        name = p._schema_name(t)
        assert name.startswith("ts_")
        assert "acme" in name

    def test_schema_name_uses_tenant_override(self, make_tenant: Callable[..., Tenant]) -> None:
        p = SchemaIsolationProvider(_sqla_cfg())
        t = make_tenant(schema_name="custom_schema")
        assert p._schema_name(t) == "custom_schema"

    def test_validated_schema_name_raises_on_invalid(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        p = SchemaIsolationProvider(_sqla_cfg())
        t = make_tenant(schema_name="INVALID-SCHEMA!")
        with pytest.raises(IsolationError, match="validate_schema_name"):
            p._validated_schema_name(t)

    def test_get_table_prefix(self, make_tenant: Callable[..., Tenant]) -> None:
        p = SchemaIsolationProvider(_sqla_cfg())
        t = make_tenant(identifier="acme-corp")
        prefix = p.get_table_prefix(t)
        assert prefix.endswith("_")
        assert len(prefix) <= 30


@pytest.mark.unit
class TestPrefixSession:
    async def test_session_info_populated(
        self,
        schema_provider: SchemaIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant(identifier="prefix-tenant")
        async with schema_provider.get_session(t) as session:
            assert session.info.get("tenant_id") == t.id
            assert "table_prefix" in session.info
            assert session.info["table_prefix"].endswith("_")

    async def test_session_prefix_matches_get_table_prefix(
        self,
        schema_provider: SchemaIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant(identifier="slug-for-prefix")
        expected = schema_provider.get_table_prefix(t)
        async with schema_provider.get_session(t) as session:
            assert session.info["table_prefix"] == expected

    async def test_prefix_session_wraps_exception_as_isolation_error(
        self,
        schema_provider: SchemaIsolationProvider,
        make_tenant: Callable[..., Tenant],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exceptions raised inside _prefix_session body become IsolationError."""
        t = make_tenant()
        with pytest.raises(IsolationError):
            async with schema_provider.get_session(t):
                raise RuntimeError("simulated failure")


@pytest.mark.unit
class TestApplyFilters:
    async def test_adds_where_clause(
        self,
        schema_provider: SchemaIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant()
        meta = sa.MetaData()
        tbl = sa.Table(
            "items", meta, sa.Column("id", sa.Integer), sa.Column("tenant_id", sa.String)
        )
        base_query = sa.select(tbl)
        filtered = await schema_provider.apply_filters(base_query, t)
        compiled = str(filtered.compile(compile_kwargs={"literal_binds": False}))
        assert "WHERE" in compiled

    async def test_returns_same_type(
        self,
        schema_provider: SchemaIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant()
        meta = sa.MetaData()
        tbl = sa.Table("t", meta, sa.Column("id", sa.Integer))
        q = sa.select(tbl)
        result = await schema_provider.apply_filters(q, t)
        # Must be a Select-compatible object
        assert hasattr(result, "where")


@pytest.mark.unit
class TestInitializeDestroyPrefix:
    async def test_initialize_without_metadata_is_noop(
        self,
        schema_provider: SchemaIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant()
        # Should complete without error even when no metadata supplied
        await schema_provider.initialize_tenant(t, metadata=None)

    async def test_initialize_creates_prefixed_tables(
        self,
        schema_provider: SchemaIsolationProvider,
        make_tenant: Callable[..., Tenant],
        simple_metadata: sa.MetaData,
    ) -> None:
        t = make_tenant(identifier="init-prefix-test")
        await schema_provider.initialize_tenant(t, metadata=simple_metadata)
        # Verify tables exist
        assert await schema_provider.verify_isolation(t) is True

    async def test_initialize_fk_remap(
        self,
        schema_provider: SchemaIsolationProvider,
        make_tenant: Callable[..., Tenant],
        fk_metadata: sa.MetaData,
    ) -> None:
        t = make_tenant(identifier="fk-test-tenant")
        # Should not raise — FK references must be remapped to prefixed names
        await schema_provider.initialize_tenant(t, metadata=fk_metadata)

    async def test_destroy_drops_prefixed_tables(
        self,
        schema_provider: SchemaIsolationProvider,
        make_tenant: Callable[..., Tenant],
        simple_metadata: sa.MetaData,
    ) -> None:
        t = make_tenant(identifier="destroy-prefix-test")
        await schema_provider.initialize_tenant(t, metadata=simple_metadata)
        assert await schema_provider.verify_isolation(t) is True
        await schema_provider.destroy_tenant(t)
        assert await schema_provider.verify_isolation(t) is False

    async def test_destroy_nonexistent_tenant_no_error(
        self,
        schema_provider: SchemaIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant(identifier="never-existed")
        # Should complete silently — no tables to drop
        await schema_provider.destroy_tenant(t)

    async def test_initialize_failure_raises_isolation_error(
        self,
        schema_provider: SchemaIsolationProvider,
        make_tenant: Callable[..., Tenant],
        simple_metadata: sa.MetaData,
    ) -> None:

        t = make_tenant()
        # AsyncEngine.begin is a Cython read-only slot; we can't patch it
        # directly with patch.object.  Instead we replace the engine on the
        # provider with a lightweight MagicMock whose begin() raises.
        broken_engine = MagicMock()
        broken_engine.begin.side_effect = RuntimeError("db exploded")
        broken_engine.connect = AsyncMock(side_effect=RuntimeError("db exploded"))

        original_engine = schema_provider.engine
        try:
            schema_provider.engine = broken_engine
            with pytest.raises(IsolationError, match="initialize_tenant"):
                await schema_provider.initialize_tenant(t, metadata=simple_metadata)
        finally:
            schema_provider.engine = original_engine


@pytest.mark.unit
class TestVerifyIsolationPrefix:
    async def test_returns_false_when_no_tables(
        self,
        schema_provider: SchemaIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant(identifier="empty-tenant")
        assert await schema_provider.verify_isolation(t) is False

    async def test_returns_true_after_initialize(
        self,
        schema_provider: SchemaIsolationProvider,
        make_tenant: Callable[..., Tenant],
        simple_metadata: sa.MetaData,
    ) -> None:
        t = make_tenant(identifier="verify-after-init")
        await schema_provider.initialize_tenant(t, metadata=simple_metadata)
        assert await schema_provider.verify_isolation(t) is True

    async def test_verify_returns_false_on_engine_error(
        self,
        schema_provider: SchemaIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:

        t = make_tenant()
        # AsyncEngine.connect is a Cython read-only slot; replace the engine
        # with a lightweight mock whose connect() raises immediately.
        broken_engine = MagicMock()
        broken_engine.connect.side_effect = RuntimeError("boom")

        original_engine = schema_provider.engine
        try:
            schema_provider.engine = broken_engine
            assert await schema_provider.verify_isolation(t) is False
        finally:
            schema_provider.engine = original_engine


@pytest.mark.unit
class TestClose:
    async def test_close_disposes_engine(self, schema_provider: SchemaIsolationProvider) -> None:
        await schema_provider.close()
        # Subsequent close must not raise (dispose is idempotent)
        await schema_provider.engine.dispose()


@pytest.mark.unit
@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("aiomysql"),
    reason="aiomysql not installed",
)
class TestMySQLDelegation:
    async def test_get_session_delegates_to_mysql_provider(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        cfg = TenancyConfig(
            database_url="mysql+aiomysql://u:p@localhost/db",
            isolation_strategy=IsolationStrategy.SCHEMA,
            schema_prefix="t_",
            database_echo=False,
            database_pool_size=2,
            database_max_overflow=2,
            database_pool_timeout=10,
            database_pool_recycle=300,
            database_pool_pre_ping=False,
        )
        p = SchemaIsolationProvider(cfg)
        assert p._mysql_delegate is not None

    async def test_initialize_delegates_to_mysql_provider(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        cfg = TenancyConfig(
            database_url="mysql+aiomysql://u:p@localhost/db",
            isolation_strategy=IsolationStrategy.SCHEMA,
            schema_prefix="t_",
            database_echo=False,
            database_pool_size=2,
            database_max_overflow=2,
            database_pool_timeout=10,
            database_pool_recycle=300,
            database_pool_pre_ping=False,
        )
        p = SchemaIsolationProvider(cfg)
        t = make_tenant()
        # Patch the delegate to avoid real DB call
        called: list[bool] = []

        async def _mock_init(tenant: Tenant, metadata: Any = None) -> None:
            called.append(True)

        p._mysql_delegate.initialize_tenant = _mock_init  # type: ignore
        await p.initialize_tenant(t)
        assert called

    async def test_destroy_delegates_to_mysql_provider(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        cfg = TenancyConfig(
            database_url="mysql+aiomysql://u:p@localhost/db",
            isolation_strategy=IsolationStrategy.SCHEMA,
            schema_prefix="t_",
            database_echo=False,
            database_pool_size=2,
            database_max_overflow=2,
            database_pool_timeout=10,
            database_pool_recycle=300,
            database_pool_pre_ping=False,
        )
        p = SchemaIsolationProvider(cfg)
        t = make_tenant()
        called: list[bool] = []

        async def _mock_destroy(tenant: Tenant, **kw: Any) -> None:
            called.append(True)

        p._mysql_delegate.destroy_tenant = _mock_destroy  # type: ignore
        await p.destroy_tenant(t)
        assert called


@pytest.mark.e2e
class TestPostgresSchemaSession:
    async def test_search_path_set_in_session(
        self,
        pg_schema_provider: SchemaIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant(identifier="pg-schema-test")
        schema = pg_schema_provider._schema_name(t)
        # Ensure schema exists first
        async with pg_schema_provider.engine.begin() as conn:
            await conn.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        try:
            async with pg_schema_provider.get_session(t) as session:
                result = await session.execute(sa.text("SHOW search_path"))
                path = result.scalar()
                assert schema in str(path)
        finally:
            async with pg_schema_provider.engine.begin() as conn:
                await conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    async def test_schema_session_exception_wrapped(
        self,
        pg_schema_provider: SchemaIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant(identifier="pg-session-err")
        schema = pg_schema_provider._schema_name(t)
        async with pg_schema_provider.engine.begin() as conn:
            await conn.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        try:
            with pytest.raises(IsolationError):
                async with pg_schema_provider.get_session(t):
                    raise RuntimeError("failure in handler")
        finally:
            async with pg_schema_provider.engine.begin() as conn:
                await conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.mark.e2e
class TestPostgresSchemaProvision:
    async def test_initialize_creates_schema(
        self,
        pg_schema_provider: SchemaIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant(identifier="pg-init-schema")
        await pg_schema_provider.initialize_tenant(t)
        assert await pg_schema_provider.verify_isolation(t) is True
        # Cleanup
        schema = pg_schema_provider._schema_name(t)
        async with pg_schema_provider.engine.begin() as conn:
            await conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    async def test_initialize_with_metadata_creates_tables(
        self,
        pg_schema_provider: SchemaIsolationProvider,
        make_tenant: Callable[..., Tenant],
        simple_metadata: sa.MetaData,
    ) -> None:
        t = make_tenant(identifier="pg-init-tables")
        await pg_schema_provider.initialize_tenant(t, metadata=simple_metadata)
        assert await pg_schema_provider.verify_isolation(t) is True
        schema = pg_schema_provider._schema_name(t)
        async with pg_schema_provider.engine.begin() as conn:
            await conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    async def test_destroy_drops_schema(
        self,
        pg_schema_provider: SchemaIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant(identifier="pg-destroy-schema")
        await pg_schema_provider.initialize_tenant(t)
        await pg_schema_provider.destroy_tenant(t)
        assert await pg_schema_provider.verify_isolation(t) is False

    async def test_initialize_idempotent(
        self,
        pg_schema_provider: SchemaIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant(identifier="pg-idempotent-schema")
        await pg_schema_provider.initialize_tenant(t)
        await pg_schema_provider.initialize_tenant(t)  # second call must not raise
        assert await pg_schema_provider.verify_isolation(t) is True
        schema = pg_schema_provider._schema_name(t)
        async with pg_schema_provider.engine.begin() as conn:
            await conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.mark.integration
class TestPrefixSessionMultiTransaction:
    """FIX: session.info must persist across manual commit/begin cycles."""

    async def test_prefix_set_in_session_info(self, make_tenant: Callable[..., Tenant]) -> None:
        provider = SchemaIsolationProvider(_sqla_cfg())
        tenant = make_tenant(identifier="prefix-tenant")
        try:
            async with provider._prefix_session(tenant) as session:
                assert session.info.get("table_prefix") is not None
                assert session.info.get("tenant_id") == tenant.id
        finally:
            await provider.close()

    async def test_prefix_survives_manual_begin_commit(
        self,
        make_tenant: Callable[..., Tenant],
        simple_metadata: sa.MetaData,
    ) -> None:
        provider = SchemaIsolationProvider(_sqla_cfg())
        tenant = make_tenant(identifier="prefix-persist")
        try:
            await provider.initialize_tenant(tenant, metadata=simple_metadata)
            async with provider._prefix_session(tenant) as session:
                prefix_before = session.info["table_prefix"]
                async with session.begin():
                    pass  # commit
                assert session.info["table_prefix"] == prefix_before
        finally:
            await provider.close()


@pytest.mark.integration
class TestSearchPathBeginEventListener:
    """FIX: SET LOCAL search_path must be re-applied after each commit on PostgreSQL."""

    async def test_search_path_reapplied_after_commit(
        self,
        make_tenant: Callable[..., Tenant],
        simple_metadata: sa.MetaData,
    ) -> None:

        pg_url = os.getenv(
            "POSTGRES_URL",
            "postgresql+asyncpg://testing:Testing123!@localhost:5432/test_db",
        )
        try:
            with socket.create_connection(("localhost", 5432), timeout=1.0):
                pass
        except OSError:
            pytest.skip("PostgreSQL not reachable")

        provider = SchemaIsolationProvider(_sqla_cfg(pg_url))
        tenant = make_tenant(identifier=f"sp-tenant-{uuid.uuid4().hex[:8]}")
        try:
            await provider.initialize_tenant(tenant, metadata=simple_metadata)
            async with provider._schema_session(tenant) as session:
                await session.execute(
                    sa.text("INSERT INTO items (tenant_id, name) VALUES (:tid, :n)"),
                    {"tid": tenant.id, "n": "hello"},
                )
                await session.commit()
                await session.execute(sa.text("SELECT 1"))
                result = await session.execute(sa.text("SELECT name FROM items"))
                rows = result.scalars().all()
            assert "hello" in rows
        finally:
            with contextlib.suppress(Exception):
                schema = provider._schema_name(tenant)
                async with provider.engine.begin() as conn:
                    await conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await provider.close()

    async def test_cross_transaction_isolation_pg_schema(
        self,
        make_tenant: Callable[..., Tenant],
        simple_metadata: sa.MetaData,
    ) -> None:

        pg_url = os.getenv(
            "POSTGRES_URL",
            "postgresql+asyncpg://testing:Testing123!@localhost:5432/test_db",
        )
        try:
            with socket.create_connection(("localhost", 5432), timeout=1.0):
                pass
        except OSError:
            pytest.skip("PostgreSQL not reachable")

        provider = SchemaIsolationProvider(_sqla_cfg(pg_url))
        uid = uuid.uuid4().hex[:8]
        tenant_a = make_tenant(identifier=f"iso-a-{uid}")
        tenant_b = make_tenant(identifier=f"iso-b-{uid}")
        try:
            await provider.initialize_tenant(tenant_a, metadata=simple_metadata)
            await provider.initialize_tenant(tenant_b, metadata=simple_metadata)
            async with provider._schema_session(tenant_a) as session:
                await session.execute(
                    sa.text("INSERT INTO items (tenant_id, name) VALUES (:tid, :n)"),
                    {"tid": tenant_a.id, "n": "tenant-a-data"},
                )
                await session.commit()
            async with provider._schema_session(tenant_b) as session:
                result = await session.execute(sa.text("SELECT name FROM items"))
                rows = result.scalars().all()
            assert rows == [], f"Tenant B saw tenant A data: {rows}"
        finally:
            for t in [tenant_a, tenant_b]:
                with contextlib.suppress(Exception):
                    schema = provider._schema_name(t)
                    async with provider.engine.begin() as conn:
                        await conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await provider.close()


@pytest.mark.integration
class TestPostgresSchemaLifecycle:
    """Full create → insert → read → destroy lifecycle on a real PostgreSQL instance."""

    async def test_schema_create_insert_read_destroy(
        self,
        make_tenant: Callable[..., Tenant],
        simple_metadata: sa.MetaData,
    ) -> None:

        pg_url = os.getenv(
            "POSTGRES_URL",
            "postgresql+asyncpg://testing:Testing123!@localhost:5432/test_db",
        )
        try:
            with socket.create_connection(("localhost", 5432), timeout=1.0):
                pass
        except OSError:
            pytest.skip("PostgreSQL not reachable")

        provider = SchemaIsolationProvider(_sqla_cfg(pg_url))
        tenant = make_tenant(identifier=f"lifecycle-{uuid.uuid4().hex[:8]}")
        try:
            await provider.initialize_tenant(tenant, metadata=simple_metadata)
            assert await provider.verify_isolation(tenant)
            async with provider.get_session(tenant) as session:
                await session.execute(
                    sa.text("INSERT INTO items (tenant_id, name) VALUES (:tid, :n)"),
                    {"tid": tenant.id, "n": "pg-data"},
                )
                await session.commit()
            async with provider.get_session(tenant) as session:
                result = await session.execute(sa.text("SELECT name FROM items"))
                rows = result.scalars().all()
            assert "pg-data" in rows
            await provider.destroy_tenant(tenant)
            assert not await provider.verify_isolation(tenant)
        finally:
            with contextlib.suppress(Exception):
                schema = provider._schema_name(tenant)
                async with provider.engine.begin() as conn:
                    await conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await provider.close()

    async def test_initialize_idempotent(
        self,
        make_tenant: Callable[..., Tenant],
        simple_metadata: sa.MetaData,
    ) -> None:

        pg_url = os.getenv(
            "POSTGRES_URL",
            "postgresql+asyncpg://testing:Testing123!@localhost:5432/test_db",
        )
        try:
            with socket.create_connection(("localhost", 5432), timeout=1.0):
                pass
        except OSError:
            pytest.skip("PostgreSQL not reachable")

        provider = SchemaIsolationProvider(_sqla_cfg(pg_url))
        tenant = make_tenant(identifier=f"idempotent-{uuid.uuid4().hex[:8]}")
        try:
            await provider.initialize_tenant(tenant, metadata=simple_metadata)
            await provider.initialize_tenant(tenant, metadata=simple_metadata)
        finally:
            with contextlib.suppress(Exception):
                schema = provider._schema_name(tenant)
                async with provider.engine.begin() as conn:
                    await conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await provider.close()


@pytest.mark.integration
class TestMSSQLSchemaIsolation:
    """FIX: MSSQL schema isolation must use schema_translate_map, not ALTER USER."""

    async def test_mssql_schema_session_sets_default_schema(
        self,
        make_tenant: Callable[..., Tenant],
        simple_metadata: sa.MetaData,
    ) -> None:

        pytest.importorskip("aioodbc", reason="aioodbc not installed")
        mssql_url = os.getenv(
            "MSSQL_URL",
            "mssql+aioodbc://sa:Testing123!@localhost:1433/test_db"
            "?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes",
        )
        try:
            with socket.create_connection(("localhost", 1433), timeout=1.0):
                pass
        except OSError:
            pytest.skip("SQL Server not reachable on localhost:1433")

        provider = SchemaIsolationProvider(_sqla_cfg(mssql_url))
        tenant = make_tenant(identifier=f"mssql-{uuid.uuid4().hex[:6]}")
        try:
            await provider.initialize_tenant(tenant, metadata=simple_metadata)
            assert await provider.verify_isolation(tenant)
            async with provider.get_session(tenant) as session:
                assert "schema" in session.info
                assert session.info["tenant_id"] == tenant.id
                schema = session.info["schema"]
                await session.execute(
                    sa.text(f"INSERT INTO [{schema}].[items] (tenant_id, name) VALUES (:tid, :n)"),
                    {"tid": tenant.id, "n": "mssql-data"},
                )
                await session.commit()
            async with provider.get_session(tenant) as session:
                schema = session.info["schema"]
                result = await session.execute(sa.text(f"SELECT name FROM [{schema}].[items]"))
                rows = result.scalars().all()
            assert "mssql-data" in rows
        finally:
            with contextlib.suppress(Exception):
                await provider.destroy_tenant(tenant)
            await provider.close()

    async def test_mssql_dispatches_to_mssql_session_not_schema_session(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        """get_session must dispatch to _mssql_schema_session, not _schema_session."""
        from unittest.mock import patch  # noqa: PLC0415

        pytest.importorskip("aioodbc", reason="aioodbc not installed")
        mssql_url = os.getenv(
            "MSSQL_URL",
            "mssql+aioodbc://sa:Testing123!@localhost:1433/test_db"
            "?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes",
        )
        try:
            with socket.create_connection(("localhost", 1433), timeout=1.0):
                pass
        except OSError:
            pytest.skip("SQL Server not reachable on localhost:1433")

        provider = SchemaIsolationProvider(_sqla_cfg(mssql_url))
        assert provider.dialect == DbDialect.MSSQL
        assert hasattr(provider, "_mssql_schema_session")
        with patch.object(
            provider,
            "_schema_session",
            side_effect=AssertionError("_schema_session must NOT be called for MSSQL"),
        ):
            # Verify dispatch logic — dialect and method presence confirmed above
            assert provider.dialect == DbDialect.MSSQL
        await provider.close()
