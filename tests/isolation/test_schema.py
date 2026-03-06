"""Tests for :class:`~fastapi_tenancy.isolation.schema.SchemaIsolationProvider`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

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
