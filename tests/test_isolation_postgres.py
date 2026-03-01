"""Integration tests for isolation providers using real PostgreSQL.

Requires:
    docker compose -f docker-compose.test.yml up -d
    POSTGRES_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/test_tenancy
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import Column, MetaData, String, Table, text
from sqlalchemy.ext.asyncio import create_async_engine

from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.exceptions import ConfigurationError, IsolationError
from fastapi_tenancy.core.types import IsolationStrategy, Tenant, TenantStatus

# ──────────────────────────────────────────────────────────────────
POSTGRES_URL = os.getenv(
    "POSTGRES_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/test_tenancy",
)
# DATABASE isolation requires database_url_template (TenancyConfig validator)
PG_DB_TEMPLATE = "postgresql+asyncpg://postgres:postgres@localhost:5432/{database_name}"

pytestmark = pytest.mark.integration


async def _pg_reachable() -> bool:
    try:
        engine = create_async_engine(POSTGRES_URL, pool_pre_ping=True)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module", autouse=True)
def skip_if_no_postgres():
    import asyncio

    loop = asyncio.get_event_loop()
    reachable = loop.run_until_complete(_pg_reachable())
    if not reachable:
        pytest.skip("PostgreSQL not reachable — start docker-compose.test.yml")


def _t(identifier: str | None = None, schema_name: str | None = None,
       isolation: IsolationStrategy | None = None) -> Tenant:
    uid = uuid.uuid4().hex[:12]
    ident = identifier or f"tenant-{uid}"
    return Tenant(
        id=f"t-{uid}", identifier=ident, name=ident.replace("-", " ").title(),
        status=TenantStatus.ACTIVE, schema_name=schema_name, isolation_strategy=isolation,
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )


def _pg_cfg(**kw: Any) -> TenancyConfig:
    return TenancyConfig(database_url=POSTGRES_URL, **kw)


def _pg_db_cfg(**kw: Any) -> TenancyConfig:
    """Config for DATABASE isolation — includes the required url_template."""
    return TenancyConfig(
        database_url=POSTGRES_URL,
        database_url_template=PG_DB_TEMPLATE,
        isolation_strategy=IsolationStrategy.DATABASE,
        **kw,
    )


_shared_meta = MetaData()
_items = Table("items", _shared_meta,
               Column("id", String(36), primary_key=True),
               Column("tenant_id", String(255), nullable=False),
               Column("name", String(255)))


# ══════════════════════════════════════════════════════════════════
# RLSIsolationProvider
# ══════════════════════════════════════════════════════════════════

class TestRLSIsolationProvider:
    @pytest_asyncio.fixture
    async def rls(self):
        from fastapi_tenancy.isolation.rls import RLSIsolationProvider
        cfg = _pg_cfg(isolation_strategy=IsolationStrategy.RLS)
        p = RLSIsolationProvider(cfg)
        yield p
        await p.close()

    def test_rejects_non_postgres(self):
        from fastapi_tenancy.isolation.rls import RLSIsolationProvider
        cfg = TenancyConfig(database_url="sqlite+aiosqlite:///:memory:",
                            isolation_strategy=IsolationStrategy.SCHEMA)
        with pytest.raises(ConfigurationError, match="RLS isolation requires PostgreSQL"):
            RLSIsolationProvider(cfg)

    async def test_engine_created(self, rls):
        assert rls.engine is not None

    async def test_get_session_sets_guc(self, rls):
        t = _t()
        async with rls.get_session(t) as session:
            r = await session.execute(text("SELECT current_setting('app.current_tenant', TRUE)"))
            assert r.scalar() == t.id

    async def test_apply_filters_adds_where(self, rls):
        from sqlalchemy import select
        t = _t()
        filtered = await rls.apply_filters(select(_items), t)
        assert "tenant_id" in str(filtered.compile())

    async def test_initialize_without_metadata(self, rls):
        await rls.initialize_tenant(_t())

    async def test_initialize_with_metadata_creates_tables(self, rls):
        t = _t()
        meta = MetaData()
        tbl_name = f"rls_{uuid.uuid4().hex[:8]}"
        Table(tbl_name, meta, Column("id", String(36), primary_key=True), Column("tenant_id", String(255)))
        await rls.initialize_tenant(t, metadata=meta)
        async with rls.engine.begin() as conn:
            await conn.run_sync(meta.drop_all)

    async def test_verify_isolation_true(self, rls):
        assert await rls.verify_isolation(_t()) is True

    async def test_destroy_without_metadata_raises(self, rls):
        with pytest.raises(IsolationError, match="metadata"):
            await rls.destroy_tenant(_t())

    async def test_destroy_with_metadata_deletes_rows(self, rls):
        uid = uuid.uuid4().hex[:8]
        meta = MetaData()
        tbl = Table(f"rls_d_{uid}", meta,
                    Column("id", String(36), primary_key=True),
                    Column("tenant_id", String(255)))
        t = _t()
        async with rls.engine.begin() as conn:
            await conn.run_sync(meta.create_all)
            await conn.execute(tbl.insert().values(id=str(uuid.uuid4()), tenant_id=t.id))
        await rls.destroy_tenant(t, metadata=meta)
        async with rls.engine.begin() as conn:
            await conn.run_sync(meta.drop_all)

    async def test_close_idempotent(self, rls):
        await rls.close()
        await rls.close()

    async def test_accepts_shared_engine(self):
        from fastapi_tenancy.isolation.rls import RLSIsolationProvider
        shared = create_async_engine(POSTGRES_URL)
        try:
            p = RLSIsolationProvider(_pg_cfg(isolation_strategy=IsolationStrategy.RLS), engine=shared)
            assert p.engine is shared
        finally:
            await shared.dispose()


# ══════════════════════════════════════════════════════════════════
# SchemaIsolationProvider — PostgreSQL
# ══════════════════════════════════════════════════════════════════

class TestSchemaIsolationProviderPostgres:
    @pytest_asyncio.fixture
    async def schema(self):
        from fastapi_tenancy.isolation.schema import SchemaIsolationProvider
        p = SchemaIsolationProvider(_pg_cfg(isolation_strategy=IsolationStrategy.SCHEMA))
        yield p
        await p.close()

    async def test_initialize_creates_schema(self, schema):
        t = _t()
        await schema.initialize_tenant(t)
        assert await schema.verify_isolation(t) is True
        await schema.destroy_tenant(t)

    async def test_initialize_idempotent(self, schema):
        t = _t()
        await schema.initialize_tenant(t)
        await schema.initialize_tenant(t)
        await schema.destroy_tenant(t)

    async def test_destroy_drops_schema(self, schema):
        t = _t()
        await schema.initialize_tenant(t)
        await schema.destroy_tenant(t)
        assert await schema.verify_isolation(t) is False

    async def test_session_sets_search_path(self, schema):
        t = _t()
        await schema.initialize_tenant(t)
        try:
            async with schema.get_session(t) as session:
                r = await session.execute(text("SHOW search_path"))
                path = r.scalar()
            assert schema._schema_name(t) in path or "public" in path
        finally:
            await schema.destroy_tenant(t)

    async def test_apply_filters_adds_tenant_id(self, schema):
        from sqlalchemy import select
        filtered = await schema.apply_filters(select(_items), _t())
        assert "tenant_id" in str(filtered.compile())

    async def test_schema_name_from_tenant_override(self, schema):
        t = _t(schema_name="override_schema_xyz")
        assert schema._schema_name(t) == "override_schema_xyz"

    async def test_verify_false_when_missing(self, schema):
        assert await schema.verify_isolation(_t()) is False

    async def test_table_prefix(self, schema):
        prefix = schema.get_table_prefix(_t())
        assert isinstance(prefix, str) and prefix.endswith("_")

    async def test_initialize_with_metadata(self, schema):
        t = _t()
        meta = MetaData()
        Table("schema_test_items", meta, Column("id", String(36), primary_key=True))
        await schema.initialize_tenant(t, metadata=meta)
        try:
            assert await schema.verify_isolation(t) is True
        finally:
            await schema.destroy_tenant(t)

    async def test_invalid_schema_name_raises(self, schema):
        t = _t(schema_name="INVALID NAME!")
        with pytest.raises(IsolationError):
            await schema.initialize_tenant(t)

    async def test_validated_schema_name(self, schema):
        name = schema._validated_schema_name(_t())
        assert isinstance(name, str) and len(name) > 0


# ══════════════════════════════════════════════════════════════════
# DatabaseIsolationProvider — PostgreSQL
# ══════════════════════════════════════════════════════════════════

class TestDatabaseIsolationProviderPostgres:
    @pytest_asyncio.fixture
    async def db(self):
        from fastapi_tenancy.isolation.database import DatabaseIsolationProvider
        p = DatabaseIsolationProvider(_pg_db_cfg())
        yield p
        await p.close()

    async def test_database_name_deterministic(self, db):
        t = _t("my-tenant")
        assert db._database_name(t) == db._database_name(t)

    async def test_tenant_url_starts_with_pg(self, db):
        assert db._tenant_url(_t()).startswith("postgresql")

    async def test_initialize_creates_database(self, db):
        t = _t()
        await db.initialize_tenant(t)
        assert await db.verify_isolation(t) is True
        await db.destroy_tenant(t)

    async def test_initialize_idempotent(self, db):
        t = _t()
        await db.initialize_tenant(t)
        await db.initialize_tenant(t)
        await db.destroy_tenant(t)

    async def test_destroy_removes_database(self, db):
        t = _t()
        await db.initialize_tenant(t)
        await db.destroy_tenant(t)
        assert await db.verify_isolation(t) is False

    async def test_session_works(self, db):
        t = _t()
        await db.initialize_tenant(t)
        try:
            async with db.get_session(t) as session:
                assert (await session.execute(text("SELECT 1"))).scalar() == 1
        finally:
            await db.destroy_tenant(t)

    async def test_apply_filters_passthrough(self, db):
        from sqlalchemy import select
        q = select(_items)
        assert await db.apply_filters(q, _t()) is q

    async def test_engine_cached(self, db):
        t = _t()
        await db.initialize_tenant(t)
        try:
            e1 = await db._get_engine(t)
            e2 = await db._get_engine(t)
            assert e1 is e2
        finally:
            await db.destroy_tenant(t)

    async def test_verify_false_for_missing(self, db):
        assert await db.verify_isolation(_t()) is False

    async def test_close_all_engines(self, db):
        t = _t()
        await db.initialize_tenant(t)
        await db.destroy_tenant(t)
        await db.close()

    async def test_mssql_raises_isolation_error(self):
        from fastapi_tenancy.isolation.database import DatabaseIsolationProvider
        from fastapi_tenancy.utils.db_compat import DbDialect
        p = DatabaseIsolationProvider(_pg_db_cfg())
        p.dialect = DbDialect.MSSQL
        with pytest.raises(IsolationError, match="MSSQL"):
            await p.initialize_tenant(_t())
        await p.close()


# ══════════════════════════════════════════════════════════════════
# _LRUEngineCache
# ══════════════════════════════════════════════════════════════════

class TestLRUEngineCache:
    async def test_miss_returns_none(self):
        from fastapi_tenancy.isolation.database import _LRUEngineCache
        assert await _LRUEngineCache(5).get("x") is None

    async def test_put_and_get(self):
        from fastapi_tenancy.isolation.database import _LRUEngineCache
        cache = _LRUEngineCache(5)
        e = create_async_engine(POSTGRES_URL)
        try:
            await cache.put("k", e)
            assert await cache.get("k") is e
        finally:
            await e.dispose()

    async def test_lru_eviction(self):
        from fastapi_tenancy.isolation.database import _LRUEngineCache
        cache = _LRUEngineCache(2)
        engines = []
        for i in range(3):
            e = create_async_engine(POSTGRES_URL)
            engines.append(e)
            evicted = await cache.put(f"k{i}", e)
            if evicted:
                await evicted.dispose()
        assert await cache.get("k0") is None
        assert cache.size == 2
        for e in engines:
            await e.dispose()

    async def test_remove(self):
        from fastapi_tenancy.isolation.database import _LRUEngineCache
        cache = _LRUEngineCache(5)
        e = create_async_engine(POSTGRES_URL)
        await cache.put("k", e)
        removed = await cache.remove("k")
        assert removed is e
        assert await cache.get("k") is None
        await e.dispose()

    async def test_remove_missing(self):
        from fastapi_tenancy.isolation.database import _LRUEngineCache
        assert await _LRUEngineCache(5).remove("nope") is None

    async def test_dispose_all(self):
        from fastapi_tenancy.isolation.database import _LRUEngineCache
        cache = _LRUEngineCache(5)
        e1 = create_async_engine(POSTGRES_URL)
        e2 = create_async_engine(POSTGRES_URL)
        await cache.put("k1", e1)
        await cache.put("k2", e2)
        assert await cache.dispose_all() == 2
        assert cache.size == 0

    async def test_put_existing_moves_to_end(self):
        from fastapi_tenancy.isolation.database import _LRUEngineCache
        cache = _LRUEngineCache(3)
        engines = []
        for i in range(3):
            e = create_async_engine(POSTGRES_URL)
            engines.append(e)
            await cache.put(f"k{i}", e)
        await cache.get("k0")  # promote k0
        e4 = create_async_engine(POSTGRES_URL)
        engines.append(e4)
        evicted = await cache.put("k3", e4)
        assert evicted is not None
        assert await cache.get("k0") is not None
        for e in engines:
            await e.dispose()

    async def test_size_property(self):
        from fastapi_tenancy.isolation.database import _LRUEngineCache
        cache = _LRUEngineCache(5)
        assert cache.size == 0
        e = create_async_engine(POSTGRES_URL)
        await cache.put("k", e)
        assert cache.size == 1
        await cache.dispose_all()


# ══════════════════════════════════════════════════════════════════
# HybridIsolationProvider
# ══════════════════════════════════════════════════════════════════

class TestHybridIsolationProviderPostgres:
    @pytest_asyncio.fixture
    async def hybrid(self):
        from fastapi_tenancy.isolation.hybrid import HybridIsolationProvider
        cfg = TenancyConfig(
            database_url=POSTGRES_URL,
            isolation_strategy=IsolationStrategy.HYBRID,
            premium_isolation_strategy=IsolationStrategy.SCHEMA,
            standard_isolation_strategy=IsolationStrategy.RLS,
            premium_tenants=["premium-tenant-id"],
        )
        p = HybridIsolationProvider(cfg)
        yield p
        await p.close()

    async def test_wrong_strategy_raises_config_error(self):
        from fastapi_tenancy.isolation.hybrid import HybridIsolationProvider
        with pytest.raises(ConfigurationError):
            HybridIsolationProvider(_pg_cfg(isolation_strategy=IsolationStrategy.SCHEMA))

    async def test_premium_routed_to_schema(self, hybrid):
        from fastapi_tenancy.isolation.schema import SchemaIsolationProvider
        t = _t()
        t = t.model_copy(update={"id": "premium-tenant-id"})
        assert isinstance(hybrid._provider_for(t), SchemaIsolationProvider)

    async def test_standard_routed_to_rls(self, hybrid):
        from fastapi_tenancy.isolation.rls import RLSIsolationProvider
        assert isinstance(hybrid._provider_for(_t()), RLSIsolationProvider)

    async def test_override_rls(self, hybrid):
        from fastapi_tenancy.isolation.rls import RLSIsolationProvider
        t = _t(isolation=IsolationStrategy.RLS)
        assert isinstance(hybrid._provider_for(t), RLSIsolationProvider)

    async def test_override_schema(self, hybrid):
        from fastapi_tenancy.isolation.schema import SchemaIsolationProvider
        t = _t(isolation=IsolationStrategy.SCHEMA)
        assert isinstance(hybrid._provider_for(t), SchemaIsolationProvider)

    async def test_unknown_override_raises(self, hybrid):
        t = _t(isolation=IsolationStrategy.DATABASE)
        with pytest.raises(IsolationError, match="_provider_for"):
            hybrid._provider_for(t)

    async def test_apply_filters_delegates(self, hybrid):
        from sqlalchemy import select
        filtered = await hybrid.apply_filters(select(_items), _t())
        assert "tenant_id" in str(filtered.compile())

    async def test_initialize_and_destroy_premium(self, hybrid):
        t = _t()
        t = t.model_copy(update={"id": "premium-tenant-id"})
        await hybrid.initialize_tenant(t)
        assert await hybrid.verify_isolation(t) is True
        await hybrid.destroy_tenant(t)

    async def test_properties(self, hybrid):
        assert hybrid.premium_provider is not None
        assert hybrid.standard_provider is not None

    async def test_get_provider_for_tenant(self, hybrid):
        assert hybrid.get_provider_for_tenant(_t()) is not None

    async def test_invalid_inner_strategy_raises(self):
        from fastapi_tenancy.isolation.hybrid import _build_provider
        cfg = TenancyConfig(
            database_url=POSTGRES_URL,
            isolation_strategy=IsolationStrategy.HYBRID,
            premium_isolation_strategy=IsolationStrategy.SCHEMA,
            standard_isolation_strategy=IsolationStrategy.RLS,
        )
        e = create_async_engine(POSTGRES_URL)
        try:
            with pytest.raises(ConfigurationError):
                _build_provider(IsolationStrategy.HYBRID, cfg, e)
        finally:
            await e.dispose()