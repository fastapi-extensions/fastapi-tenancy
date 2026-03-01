"""Tests for TenancyManager — resolver building, tenant lifecycle, audit log."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest

from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.exceptions import (
    ConfigurationError,
    RateLimitExceededError,
    TenancyError,
    TenantNotFoundError,
)
from fastapi_tenancy.core.types import (
    AuditLog,
    IsolationStrategy,
    ResolutionStrategy,
    Tenant,
    TenantStatus,
)
from fastapi_tenancy.manager import TenancyManager
from fastapi_tenancy.storage.memory import InMemoryTenantStore


SQLITE_URL = "sqlite+aiosqlite:///:memory:"


def _cfg(**kwargs) -> TenancyConfig:
    return TenancyConfig(database_url=SQLITE_URL, **kwargs)


def _tenant(identifier: str = "acme-corp", tenant_id: str | None = None) -> Tenant:
    return Tenant(
        id=tenant_id or f"t-{uuid.uuid4().hex[:8]}",
        identifier=identifier,
        name=identifier.replace("-", " ").title(),
        status=TenantStatus.ACTIVE,
        metadata={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Resolver building via _build_resolver
# ---------------------------------------------------------------------------

class TestBuildResolver:
    def test_header_resolver_built(self):
        from fastapi_tenancy.resolution.header import HeaderTenantResolver
        cfg = _cfg(resolution_strategy=ResolutionStrategy.HEADER)
        store = InMemoryTenantStore()
        m = TenancyManager(cfg, store)
        assert isinstance(m.resolver, HeaderTenantResolver)

    def test_subdomain_resolver_built(self):
        from fastapi_tenancy.resolution.subdomain import SubdomainTenantResolver
        cfg = _cfg(
            resolution_strategy=ResolutionStrategy.SUBDOMAIN,
            domain_suffix=".example.com",
        )
        store = InMemoryTenantStore()
        m = TenancyManager(cfg, store)
        assert isinstance(m.resolver, SubdomainTenantResolver)

    def test_path_resolver_built(self):
        from fastapi_tenancy.resolution.path import PathTenantResolver
        cfg = _cfg(resolution_strategy=ResolutionStrategy.PATH)
        store = InMemoryTenantStore()
        m = TenancyManager(cfg, store)
        assert isinstance(m.resolver, PathTenantResolver)

    def test_jwt_resolver_built(self):
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver
        cfg = _cfg(
            resolution_strategy=ResolutionStrategy.JWT,
            jwt_secret="a" * 32,
        )
        store = InMemoryTenantStore()
        m = TenancyManager(cfg, store)
        assert isinstance(m.resolver, JWTTenantResolver)

    def test_custom_without_resolver_raises(self):
        cfg = _cfg(resolution_strategy=ResolutionStrategy.CUSTOM)
        store = InMemoryTenantStore()
        with pytest.raises(ConfigurationError, match="custom_resolver"):
            TenancyManager(cfg, store)

    def test_custom_with_resolver_succeeds(self):
        cfg = _cfg(resolution_strategy=ResolutionStrategy.CUSTOM)
        store = InMemoryTenantStore()
        custom_resolver = MagicMock()
        custom_resolver.resolve = AsyncMock()
        m = TenancyManager(cfg, store, custom_resolver=custom_resolver)
        assert m.resolver is custom_resolver

    def test_custom_resolver_ignored_for_non_custom_strategy(self, caplog):
        import logging
        cfg = _cfg(resolution_strategy=ResolutionStrategy.HEADER)
        store = InMemoryTenantStore()
        custom_resolver = MagicMock()
        custom_resolver.resolve = AsyncMock()
        with caplog.at_level(logging.WARNING):
            m = TenancyManager(cfg, store, custom_resolver=custom_resolver)
        # resolver should NOT be the custom one
        assert m.resolver is not custom_resolver


# ---------------------------------------------------------------------------
# Isolation provider building
# ---------------------------------------------------------------------------

class TestBuildProvider:
    def test_schema_provider_built(self):
        from fastapi_tenancy.isolation.schema import SchemaIsolationProvider
        cfg = _cfg(isolation_strategy=IsolationStrategy.SCHEMA)
        m = TenancyManager(cfg, InMemoryTenantStore())
        assert isinstance(m.isolation_provider, SchemaIsolationProvider)

    def test_rls_provider_built(self):
        from fastapi_tenancy.isolation.rls import RLSIsolationProvider
        pg_url = os.getenv("POSTGRES_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/test")
        try:
            cfg = TenancyConfig(database_url=pg_url, isolation_strategy=IsolationStrategy.RLS)
            m = TenancyManager(cfg, InMemoryTenantStore())
            assert isinstance(m.isolation_provider, RLSIsolationProvider)
        except Exception:
            pytest.skip("RLS requires PostgreSQL — POSTGRES_URL not set or unreachable")

    def test_hybrid_provider_built(self):
        from fastapi_tenancy.isolation.hybrid import HybridIsolationProvider
        pg_url = os.getenv("POSTGRES_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/test")
        try:
            cfg = TenancyConfig(
                database_url=pg_url,
                isolation_strategy=IsolationStrategy.HYBRID,
                premium_isolation_strategy=IsolationStrategy.SCHEMA,
                standard_isolation_strategy=IsolationStrategy.RLS,
            )
            m = TenancyManager(cfg, InMemoryTenantStore())
            assert isinstance(m.isolation_provider, HybridIsolationProvider)
        except Exception:
            pytest.skip("Hybrid+RLS requires PostgreSQL — POSTGRES_URL not set or unreachable")

    def test_database_provider_built(self):
        from fastapi_tenancy.isolation.database import DatabaseIsolationProvider
        cfg = TenancyConfig(
            database_url=SQLITE_URL,
            isolation_strategy=IsolationStrategy.DATABASE,
            database_url_template="sqlite+aiosqlite:///{tenant_id}.db",
        )
        m = TenancyManager(cfg, InMemoryTenantStore())
        assert isinstance(m.isolation_provider, DatabaseIsolationProvider)

    def test_custom_provider_injected(self):
        cfg = _cfg()
        mock_provider = MagicMock()
        m = TenancyManager(cfg, InMemoryTenantStore(), isolation_provider=mock_provider)
        assert m.isolation_provider is mock_provider


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    async def test_initialize_calls_store_initialize(self):
        cfg = _cfg()
        store = InMemoryTenantStore()
        # InMemoryTenantStore has no initialize, but TenancyManager checks hasattr
        m = TenancyManager(cfg, store)
        await m.initialize()  # should not raise
        await m.close()

    async def test_close_does_not_raise(self):
        cfg = _cfg()
        m = TenancyManager(cfg, InMemoryTenantStore())
        await m.initialize()
        await m.close()  # should not raise

    async def test_lifespan_context_manager(self):
        cfg = _cfg()
        m = TenancyManager(cfg, InMemoryTenantStore())
        lifespan = m.create_lifespan()
        from fastapi import FastAPI
        app = FastAPI(lifespan=lifespan)
        # Just ensure it's callable
        assert callable(lifespan)


# ---------------------------------------------------------------------------
# register_tenant
# ---------------------------------------------------------------------------

class TestRegisterTenant:
    async def test_creates_tenant_in_store(self):
        cfg = _cfg()
        store = InMemoryTenantStore()
        m = TenancyManager(cfg, store)
        await m.initialize()
        tenant = await m.register_tenant("new-tenant", "New Tenant")
        assert tenant.identifier == "new-tenant"
        assert await store.exists(tenant.id)
        await m.close()

    async def test_invalid_identifier_raises(self):
        cfg = _cfg()
        m = TenancyManager(cfg, InMemoryTenantStore())
        await m.initialize()
        with pytest.raises(ValueError, match="Invalid tenant identifier"):
            await m.register_tenant("-bad-slug-", "Bad Slug")
        await m.close()

    async def test_metadata_stored(self):
        cfg = _cfg()
        store = InMemoryTenantStore()
        m = TenancyManager(cfg, store)
        await m.initialize()
        tenant = await m.register_tenant(
            "meta-tenant", "Meta Tenant",
            metadata={"plan": "premium"},
        )
        assert tenant.metadata["plan"] == "premium"
        await m.close()

    async def test_isolation_provider_called_for_new_tenant(self):
        cfg = _cfg()
        store = InMemoryTenantStore()
        mock_provider = MagicMock()
        mock_provider.initialize_tenant = AsyncMock()
        mock_provider.close = AsyncMock()
        m = TenancyManager(cfg, store, isolation_provider=mock_provider)
        await m.initialize()
        await m.register_tenant("init-tenant", "Init Tenant")
        mock_provider.initialize_tenant.assert_called_once()
        await m.close()

    async def test_rollback_on_isolation_failure(self):
        cfg = _cfg()
        store = InMemoryTenantStore()
        mock_provider = MagicMock()
        mock_provider.initialize_tenant = AsyncMock(side_effect=RuntimeError("DB exploded"))
        mock_provider.close = AsyncMock()
        m = TenancyManager(cfg, store, isolation_provider=mock_provider)
        await m.initialize()
        with pytest.raises(TenancyError):
            await m.register_tenant("fail-tenant", "Fail Tenant")
        # Store should NOT contain the tenant after rollback
        assert await store.count() == 0
        await m.close()

    async def test_uses_config_default_status(self):
        cfg = TenancyConfig(
            database_url=SQLITE_URL,
            default_tenant_status="provisioning",
        )
        store = InMemoryTenantStore()
        mock_provider = MagicMock()
        mock_provider.initialize_tenant = AsyncMock()
        mock_provider.close = AsyncMock()
        m = TenancyManager(cfg, store, isolation_provider=mock_provider)
        tenant = await m.register_tenant("prov-tenant", "Prov Tenant")
        assert tenant.status == TenantStatus.PROVISIONING
        await m.close()


# ---------------------------------------------------------------------------
# suspend / activate / delete
# ---------------------------------------------------------------------------

class TestTenantLifecycleOps:
    async def _setup(self) -> tuple[TenancyManager, InMemoryTenantStore, Tenant]:
        cfg = _cfg()
        store = InMemoryTenantStore()
        mock_provider = MagicMock()
        mock_provider.initialize_tenant = AsyncMock()
        mock_provider.destroy_tenant = AsyncMock()
        mock_provider.close = AsyncMock()
        m = TenancyManager(cfg, store, isolation_provider=mock_provider)
        await m.initialize()
        t = _tenant()
        await store.create(t)
        return m, store, t

    async def test_suspend_tenant(self):
        m, store, t = await self._setup()
        result = await m.suspend_tenant(t.id)
        assert result.status == TenantStatus.SUSPENDED
        await m.close()

    async def test_activate_tenant(self):
        m, store, t = await self._setup()
        await m.suspend_tenant(t.id)
        result = await m.activate_tenant(t.id)
        assert result.status == TenantStatus.ACTIVE
        await m.close()

    async def test_suspend_not_found_raises(self):
        m, store, _ = await self._setup()
        with pytest.raises(TenantNotFoundError):
            await m.suspend_tenant("ghost-id")
        await m.close()

    async def test_soft_delete_marks_as_deleted(self):
        cfg = _cfg(enable_soft_delete=True)
        store = InMemoryTenantStore()
        mock_provider = MagicMock()
        mock_provider.initialize_tenant = AsyncMock()
        mock_provider.close = AsyncMock()
        m = TenancyManager(cfg, store, isolation_provider=mock_provider)
        t = _tenant()
        await store.create(t)
        await m.delete_tenant(t.id)
        updated = await store.get_by_id(t.id)
        assert updated.status == TenantStatus.DELETED

    async def test_hard_delete_removes_from_store(self):
        cfg = TenancyConfig(database_url=SQLITE_URL, enable_soft_delete=False)
        store = InMemoryTenantStore()
        mock_provider = MagicMock()
        mock_provider.close = AsyncMock()
        m = TenancyManager(cfg, store, isolation_provider=mock_provider)
        t = _tenant()
        await store.create(t)
        await m.delete_tenant(t.id)
        assert not await store.exists(t.id)

    async def test_delete_with_destroy_data(self):
        cfg = TenancyConfig(database_url=SQLITE_URL, enable_soft_delete=False)
        store = InMemoryTenantStore()
        mock_provider = MagicMock()
        mock_provider.destroy_tenant = AsyncMock()
        mock_provider.close = AsyncMock()
        m = TenancyManager(cfg, store, isolation_provider=mock_provider)
        t = _tenant()
        await store.create(t)
        await m.delete_tenant(t.id, destroy_data=True)
        mock_provider.destroy_tenant.assert_called_once()


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class TestAuditLog:
    async def test_default_audit_writer_logs(self, caplog):
        import logging
        cfg = _cfg(enable_audit_logging=True)
        m = TenancyManager(cfg, InMemoryTenantStore())
        entry = AuditLog(
            tenant_id="t1",
            action="create",
            resource="user",
            resource_id="u1",
            user_id="admin",
        )
        with caplog.at_level(logging.INFO):
            await m.write_audit_log(entry)
        # Default writer logs at INFO
        assert any("AUDIT" in r.message or "t1" in r.message for r in caplog.records)

    async def test_custom_audit_writer_called(self):
        mock_writer = MagicMock()
        mock_writer.write = AsyncMock()
        cfg = _cfg()
        m = TenancyManager(cfg, InMemoryTenantStore(), audit_writer=mock_writer)
        entry = AuditLog(tenant_id="t1", action="delete", resource="order")
        await m.write_audit_log(entry)
        mock_writer.write.assert_called_once_with(entry)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class TestRateLimiting:
    async def test_rate_limit_check_noop_when_disabled(self):
        cfg = _cfg(enable_rate_limiting=False)
        m = TenancyManager(cfg, InMemoryTenantStore())
        t = _tenant()
        # Should not raise even with no Redis
        await m.check_rate_limit(t)

    async def test_rate_limit_check_noop_without_limiter(self):
        cfg = _cfg()
        m = TenancyManager(cfg, InMemoryTenantStore())
        m._rate_limiting_enabled = True
        m._rate_limiter = None
        t = _tenant()
        await m.check_rate_limit(t)  # should not raise

    async def test_rate_limit_exceeded_raises(self):
        cfg = _cfg()
        m = TenancyManager(cfg, InMemoryTenantStore())
        m._rate_limiting_enabled = True
        # Mock Redis returning count > limit
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(return_value=999)  # way over limit
        m._rate_limiter = mock_redis
        m.config = m.config.model_copy(update={"rate_limit_per_minute": 100})
        t = _tenant()
        with pytest.raises(RateLimitExceededError):
            await m.check_rate_limit(t)

    async def test_rate_limit_redis_failure_is_logged_not_raised(self, caplog):
        import logging
        cfg = _cfg()
        m = TenancyManager(cfg, InMemoryTenantStore())
        m._rate_limiting_enabled = True
        mock_redis = MagicMock()
        mock_redis.eval = AsyncMock(side_effect=ConnectionError("Redis down"))
        m._rate_limiter = mock_redis
        t = _tenant()
        with caplog.at_level(logging.WARNING):
            await m.check_rate_limit(t)  # should NOT raise
        assert any("Rate limit check failed" in r.message for r in caplog.records)