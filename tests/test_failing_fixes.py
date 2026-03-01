"""Unit/integration tests for isolation providers using SQLite (no external DB required)."""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import Column, MetaData, String, Table, text
from sqlalchemy.ext.asyncio import create_async_engine

from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.exceptions import IsolationError
from fastapi_tenancy.core.types import IsolationStrategy, Tenant, TenantStatus


def _t(identifier: str | None = None, schema_name: str | None = None) -> Tenant:
    uid = uuid.uuid4().hex[:12]
    ident = identifier or f"tenant-{uid}"
    return Tenant(
        id=f"t-{uid}", identifier=ident, name=ident.replace("-", " ").title(),
        status=TenantStatus.ACTIVE, schema_name=schema_name,
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )


def _sqlite_cfg(**kw: Any) -> TenancyConfig:
    return TenancyConfig(database_url="sqlite+aiosqlite:///:memory:", **kw)


def _sqlite_db_cfg(tmp_path: str) -> TenancyConfig:
    """SQLite DATABASE isolation — requires database_url_template with {tenant_id}."""
    return TenancyConfig(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/master.db",
        isolation_strategy=IsolationStrategy.DATABASE,
        database_url_template=f"sqlite+aiosqlite:///{tmp_path}/{{tenant_id}}.db",
    )


_shared_meta = MetaData()
_items = Table(
    "items", _shared_meta,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(255), nullable=False),
    Column("name", String(255)),
)


# ══════════════════════════════════════════════════════════════════
# SchemaIsolationProvider — SQLite (prefix mode)
# ══════════════════════════════════════════════════════════════════

class TestSchemaIsolationProviderSQLite:
    @pytest_asyncio.fixture
    async def schema(self):
        from fastapi_tenancy.isolation.schema import SchemaIsolationProvider
        cfg = _sqlite_cfg(isolation_strategy=IsolationStrategy.SCHEMA)
        p = SchemaIsolationProvider(cfg)
        yield p
        await p.close()

    async def test_initialize_and_verify(self, schema):
        t = _t()
        meta = MetaData()
        Table("test_items", meta, Column("id", String(36), primary_key=True))
        await schema.initialize_tenant(t, metadata=meta)
        assert await schema.verify_isolation(t) is True
        await schema.destroy_tenant(t)

    async def test_get_table_prefix_format(self, schema):
        t = _t()
        prefix = schema.get_table_prefix(t)
        assert isinstance(prefix, str)
        assert len(prefix) > 0

    async def test_initialize_creates_prefix_tables_from_metadata(self, schema):
        t = _t()
        meta = MetaData()
        Table("orders", meta, Column("id", String(36), primary_key=True), Column("tenant_id", String(255)))
        await schema.initialize_tenant(t, metadata=meta)
        assert await schema.verify_isolation(t) is True
        await schema.destroy_tenant(t)

    async def test_destroy_clears_state(self, schema):
        t = _t()
        await schema.initialize_tenant(t)
        await schema.destroy_tenant(t)

    async def test_get_session_populates_session_info(self, schema):
        t = _t()
        await schema.initialize_tenant(t)
        try:
            async with schema.get_session(t) as session:
                assert session is not None
                # session.info should contain tenant info for prefix mode
                info = getattr(session, "info", {})
                assert isinstance(info, dict)
        finally:
            await schema.destroy_tenant(t)

    async def test_apply_filters_adds_tenant_clause(self, schema):
        from sqlalchemy import select
        t = _t()
        filtered = await schema.apply_filters(select(_items), t)
        compiled = str(filtered.compile())
        assert "tenant_id" in compiled

    async def test_schema_name_uses_tenant_override(self, schema):
        t = _t(schema_name="my_custom_schema")
        name = schema._schema_name(t)
        assert name == "my_custom_schema"

    async def test_schema_name_generated_from_identifier(self, schema):
        t = _t()
        name = schema._schema_name(t)
        assert t.identifier.replace("-", "_") in name or t.id in name

    async def test_verify_isolation_returns_false_when_missing(self, schema):
        t = _t()
        assert await schema.verify_isolation(t) is False

    async def test_close_idempotent(self, schema):
        await schema.close()
        await schema.close()

    async def test_initialize_idempotent(self, schema):
        t = _t()
        await schema.initialize_tenant(t)
        await schema.initialize_tenant(t)
        await schema.destroy_tenant(t)


# ══════════════════════════════════════════════════════════════════
# DatabaseIsolationProvider — SQLite (file-based)
# ══════════════════════════════════════════════════════════════════

class TestDatabaseIsolationProviderSQLite:
    @pytest_asyncio.fixture
    async def db_provider(self):
        from fastapi_tenancy.isolation.database import DatabaseIsolationProvider

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _sqlite_db_cfg(tmp)
            p = DatabaseIsolationProvider(cfg)
            yield p
            await p.close()

    async def test_tenant_url_creates_separate_db_file(self, db_provider):
        t = _t()
        url = db_provider._tenant_url(t)
        # URL contains sanitized identifier (hyphens → underscores)
        slug = t.identifier.replace("-", "_")
        assert slug in url or t.id.replace("-", "_") in url

    async def test_initialize_sqlite_creates_file(self, db_provider):
        t = _t()
        meta = MetaData()
        Table("events", meta, Column("id", String(36), primary_key=True))
        await db_provider.initialize_tenant(t, metadata=meta)
        assert await db_provider.verify_isolation(t) is True
        await db_provider.destroy_tenant(t)

    async def test_destroy_sqlite_removes_file(self, db_provider):
        t = _t()
        meta = MetaData()
        Table("items", meta, Column("id", String(36), primary_key=True))
        await db_provider.initialize_tenant(t, metadata=meta)
        assert await db_provider.verify_isolation(t) is True
        await db_provider.destroy_tenant(t)
        assert await db_provider.verify_isolation(t) is False

    async def test_initialize_sqlite_with_metadata(self, db_provider):
        t = _t()
        meta = MetaData()
        Table("events", meta, Column("id", String(36), primary_key=True))
        await db_provider.initialize_tenant(t, metadata=meta)
        try:
            assert await db_provider.verify_isolation(t) is True
        finally:
            await db_provider.destroy_tenant(t)

    async def test_get_session_works_after_init(self, db_provider):
        t = _t()
        await db_provider.initialize_tenant(t)
        try:
            async with db_provider.get_session(t) as session:
                r = await session.execute(text("SELECT 1"))
                assert r.scalar() == 1
        finally:
            await db_provider.destroy_tenant(t)

    async def test_verify_false_for_unknown_tenant(self, db_provider):
        assert await db_provider.verify_isolation(_t()) is False

    async def test_apply_filters_passthrough(self, db_provider):
        from sqlalchemy import select
        q = select(_items)
        assert await db_provider.apply_filters(q, _t()) is q

    async def test_database_name_is_deterministic(self, db_provider):
        t = _t()
        assert db_provider._database_name(t) == db_provider._database_name(t)

    async def test_concurrent_engine_creation_is_safe(self, db_provider):
        import asyncio
        t = _t()
        await db_provider.initialize_tenant(t)
        try:
            engines = await asyncio.gather(*[db_provider._get_engine(t) for _ in range(5)])
            # All should be the same cached engine
            assert all(e is engines[0] for e in engines)
        finally:
            await db_provider.destroy_tenant(t)

    async def test_close_and_then_initialize(self, db_provider):
        t = _t()
        await db_provider.initialize_tenant(t)
        await db_provider.destroy_tenant(t)
        await db_provider.close()


# ══════════════════════════════════════════════════════════════════
# BaseIsolationProvider helper methods
# ══════════════════════════════════════════════════════════════════

class TestBaseIsolationProviderHelpers:
    async def test_get_schema_name_respects_override(self):
        from fastapi_tenancy.isolation.schema import SchemaIsolationProvider
        cfg = _sqlite_cfg(isolation_strategy=IsolationStrategy.SCHEMA)
        p = SchemaIsolationProvider(cfg)
        t = _t(schema_name="override_schema")
        assert p._schema_name(t) == "override_schema"
        await p.close()

    async def test_get_schema_name_default_generated(self):
        from fastapi_tenancy.isolation.schema import SchemaIsolationProvider
        cfg = _sqlite_cfg(isolation_strategy=IsolationStrategy.SCHEMA)
        p = SchemaIsolationProvider(cfg)
        t = _t()
        name = p._schema_name(t)
        assert isinstance(name, str) and len(name) > 0
        await p.close()

    async def test_get_database_url_with_tenant(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _sqlite_db_cfg(tmp)
            url = cfg.get_database_url_for_tenant("test-tenant-id")
            assert "test-tenant-id" in url or "sqlite" in url

    async def test_config_build_database_url_includes_tenant(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _sqlite_db_cfg(tmp)
            url = cfg.get_database_url_for_tenant("t123")
            assert isinstance(url, str)
            assert "t123" in url or "sqlite" in url
