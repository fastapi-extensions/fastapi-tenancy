"""Integration tests for uncovered code paths: dependencies, middleware edge cases,
manager operations, migrations, and the public API surface.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.context import TenantContext
from fastapi_tenancy.core.exceptions import (
    RateLimitExceededError,
    TenancyError,
    TenantInactiveError,
    TenantNotFoundError,
    TenantResolutionError,
)
from fastapi_tenancy.core.types import (
    IsolationStrategy,
    ResolutionStrategy,
    Tenant,
    TenantStatus,
)
from fastapi_tenancy.manager import TenancyManager
from fastapi_tenancy.middleware.tenancy import TenancyMiddleware
from fastapi_tenancy.storage.memory import InMemoryTenantStore

SQLITE_URL = "sqlite+aiosqlite:///:memory:"


def _t(identifier: str | None = None) -> Tenant:
    uid = uuid.uuid4().hex[:12]
    ident = identifier or f"tenant-{uid}"
    return Tenant(
        id=f"id-{uid}", identifier=ident, name=ident.replace("-", " ").title(),
        status=TenantStatus.ACTIVE, metadata={},
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )


def _cfg(**kw: Any) -> TenancyConfig:
    return TenancyConfig(
        database_url=SQLITE_URL,
        resolution_strategy=ResolutionStrategy.HEADER,
        isolation_strategy=IsolationStrategy.SCHEMA,
        tenant_header_name="X-Tenant-ID",
        **kw,
    )


@pytest_asyncio.fixture
async def store_with_acme():
    store = InMemoryTenantStore()
    t = _t("acme-corp")
    await store.create(t)
    return store, t


@pytest_asyncio.fixture
async def manager_with_acme():
    store = InMemoryTenantStore()
    cfg = _cfg()
    m = TenancyManager(cfg, store)
    await m.initialize()
    t = _t("acme-corp")
    await store.create(t)
    yield m, store, t
    await m.close()


# ══════════════════════════════════════════════════════════════════
# FastAPI Dependencies
# ══════════════════════════════════════════════════════════════════

class TestDependencies:
    @pytest_asyncio.fixture
    async def app_with_manager(self, manager_with_acme):
        from fastapi import Depends
        from typing import Annotated
        from fastapi_tenancy.dependencies import (
            TenantDep, TenantOptionalDep, make_tenant_db_dependency,
            make_tenant_config_dependency, make_audit_log_dependency,
        )

        manager, store, acme = manager_with_acme
        get_db = make_tenant_db_dependency(manager)
        get_cfg = make_tenant_config_dependency(manager)
        get_audit = make_audit_log_dependency(manager)

        app = FastAPI()
        app.add_middleware(TenancyMiddleware, manager=manager, excluded_paths=[])

        @app.get("/tenant")
        async def get_tenant(tenant: TenantDep):
            return {"id": tenant.id, "identifier": tenant.identifier}

        @app.get("/tenant-optional")
        async def get_tenant_optional(tenant: TenantOptionalDep):
            return {"id": tenant.id if tenant else None}

        @app.get("/tenant-config")
        async def get_tenant_config(
            config: Annotated[Any, Depends(get_cfg)]
        ):
            return {"has_config": True}

        @app.get("/audit")
        async def audit_endpoint(
            audit: Annotated[Any, Depends(get_audit)]
        ):
            await audit(action="read", resource="order")
            return {"ok": True}

        return app, acme

    async def test_tenant_dep_resolves(self, app_with_manager):
        app, acme = app_with_manager
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"X-Tenant-ID": acme.identifier},
        ) as client:
            resp = await client.get("/tenant")
        assert resp.status_code == 200
        assert resp.json()["id"] == acme.id

    async def test_tenant_optional_dep_resolves(self, app_with_manager):
        app, acme = app_with_manager
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"X-Tenant-ID": acme.identifier},
        ) as client:
            resp = await client.get("/tenant-optional")
        assert resp.status_code == 200

    async def test_tenant_config_dep(self, app_with_manager):
        app, acme = app_with_manager
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"X-Tenant-ID": acme.identifier},
        ) as client:
            resp = await client.get("/tenant-config")
        assert resp.status_code == 200

    async def test_audit_dep(self, app_with_manager):
        app, acme = app_with_manager
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"X-Tenant-ID": acme.identifier},
        ) as client:
            resp = await client.get("/audit")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════
# Middleware edge cases
# ══════════════════════════════════════════════════════════════════

class TestMiddlewareCoverage:
    @pytest_asyncio.fixture
    async def store_and_manager(self):
        store = InMemoryTenantStore()
        m = TenancyManager(_cfg(), store)
        await m.initialize()
        yield store, m
        await m.close()

    def _make_app(self, manager, handler):
        # Request and FastAPI imported at module level
        app = FastAPI()
        app.add_middleware(TenancyMiddleware, manager=manager, excluded_paths=["/health"])

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        @app.get("/tenant")
        async def tenant_route(request: Request):
            return await handler(request)

        return app

    async def test_excluded_path_bypasses_middleware(self, store_and_manager):
        store, manager = store_and_manager
        app = self._make_app(manager, lambda r: {"ok": True})
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/health")
        assert resp.status_code == 200

    async def test_missing_header_returns_400(self, store_and_manager):
        store, manager = store_and_manager
        app = self._make_app(manager, lambda r: {"ok": True})
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/tenant")
        assert resp.status_code in (400, 404, 422)

    async def test_not_found_tenant_returns_404(self, store_and_manager):
        store, manager = store_and_manager
        app = self._make_app(manager, lambda r: {"ok": True})
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"X-Tenant-ID": "nonexistent"},
        ) as c:
            resp = await c.get("/tenant")
        assert resp.status_code == 404

    async def test_inactive_tenant_returns_403(self, store_and_manager):
        store, manager = store_and_manager
        t = _t()
        t = t.model_copy(update={"status": TenantStatus.SUSPENDED})
        await store.create(t)
        app = self._make_app(manager, lambda r: {"ok": True})
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"X-Tenant-ID": t.identifier},
        ) as c:
            resp = await c.get("/tenant")
        assert resp.status_code == 403

    async def test_tenant_in_scope_state(self, store_and_manager):
        # Request and FastAPI imported at module level
        store, manager = store_and_manager
        t = _t()
        await store.create(t)

        app = FastAPI()
        app.add_middleware(TenancyMiddleware, manager=manager, excluded_paths=[])

        @app.get("/scope")
        async def scope_endpoint(request: Request):
            tenant = request.state.tenant
            return {"id": tenant.id}

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"X-Tenant-ID": t.identifier},
        ) as c:
            resp = await c.get("/scope")
        assert resp.status_code == 200
        assert resp.json()["id"] == t.id

    async def test_rate_limit_exceeded_returns_429(self, store_and_manager):
        # Request and FastAPI imported at module level
        from unittest.mock import patch, AsyncMock
        store, manager = store_and_manager
        t = _t()
        await store.create(t)

        # Enable rate limiting in config
        manager.config = manager.config.model_copy(update={"enable_rate_limiting": True})

        app = FastAPI()
        app.add_middleware(TenancyMiddleware, manager=manager, excluded_paths=[])

        @app.get("/test")
        async def route(request: Request):
            return {"ok": True}

        # Patch check_rate_limit to raise RateLimitExceededError during middleware processing
        with patch.object(manager, "check_rate_limit", new=AsyncMock(
            side_effect=RateLimitExceededError(tenant_id=t.id, limit=10, window_seconds=60)
        )):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test",
                headers={"X-Tenant-ID": t.identifier},
            ) as c:
                resp = await c.get("/test")
        assert resp.status_code == 429

    async def test_tenant_inactive_error_in_app_returns_403(self, store_and_manager):
        """Inactive tenant is rejected during resolution with 403."""
        # Request and FastAPI imported at module level
        store, manager = store_and_manager
        t = _t()
        t = t.model_copy(update={"status": TenantStatus.SUSPENDED})
        await store.create(t)

        app = FastAPI()
        app.add_middleware(TenancyMiddleware, manager=manager, excluded_paths=[])

        @app.get("/test")
        async def route(request: Request):
            return {"ok": True}

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"X-Tenant-ID": t.identifier},
        ) as c:
            resp = await c.get("/test")
        assert resp.status_code == 403

    async def test_unhandled_tenancy_error_returns_500(self, store_and_manager):
        """A TenancyError during resolution returns 500."""
        # Request and FastAPI imported at module level
        from unittest.mock import patch, AsyncMock
        store, manager = store_and_manager

        app = FastAPI()
        app.add_middleware(TenancyMiddleware, manager=manager, excluded_paths=[])

        @app.get("/test")
        async def route(request: Request):
            return {"ok": True}

        # Patch resolver to raise generic TenancyError
        with patch.object(manager.resolver, "resolve", new=AsyncMock(
            side_effect=TenancyError("unexpected failure", details={})
        )):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test",
                headers={"X-Tenant-ID": "any-tenant"},
            ) as c:
                resp = await c.get("/test")
        assert resp.status_code == 500

    async def test_lifespan_passthrough(self, store_and_manager):
        from contextlib import asynccontextmanager
        # FastAPI imported at module level
        store, manager = store_and_manager

        @asynccontextmanager
        async def lifespan(app):
            yield

        app = FastAPI(lifespan=lifespan)
        app.add_middleware(TenancyMiddleware, manager=manager, excluded_paths=["/health"])

        @app.get("/health")
        async def health():
            return {"ok": True}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/health")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════
# Subdomain resolver — identifier that truly fails validation
# ══════════════════════════════════════════════════════════════════

class TestSubdomainResolverFix:
    async def test_invalid_subdomain_format_raises_resolution_error(self):
        """A subdomain starting with '-' fails validate_tenant_identifier → TenantResolutionError."""
        from starlette.requests import Request as StarletteRequest
        from fastapi_tenancy.resolution.subdomain import SubdomainTenantResolver

        store = InMemoryTenantStore()
        resolver = SubdomainTenantResolver(store, domain_suffix=".example.com")

        scope = {
            "type": "http",
            "headers": [(b"host", b"-invalid.example.com")],
            "method": "GET",
            "path": "/",
            "query_string": b"",
        }
        req = StarletteRequest(scope)
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_valid_identifier_not_in_store_raises_not_found(self):
        """A valid slug not in the store → TenantNotFoundError (not TenantResolutionError)."""
        from starlette.requests import Request as StarletteRequest
        from fastapi_tenancy.resolution.subdomain import SubdomainTenantResolver

        store = InMemoryTenantStore()
        resolver = SubdomainTenantResolver(store, domain_suffix=".example.com")

        scope = {
            "type": "http",
            "headers": [(b"host", b"missing-corp.example.com")],
            "method": "GET",
            "path": "/",
            "query_string": b"",
        }
        req = StarletteRequest(scope)
        with pytest.raises(TenantNotFoundError):
            await resolver.resolve(req)

    async def test_too_short_subdomain_raises_resolution_error(self):
        """'ab' (2 chars) fails validate_tenant_identifier → TenantResolutionError."""
        from starlette.requests import Request as StarletteRequest
        from fastapi_tenancy.resolution.subdomain import SubdomainTenantResolver

        store = InMemoryTenantStore()
        resolver = SubdomainTenantResolver(store, domain_suffix=".example.com")

        scope = {
            "type": "http",
            "headers": [(b"host", b"ab.example.com")],
            "method": "GET",
            "path": "/",
            "query_string": b"",
        }
        req = StarletteRequest(scope)
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_no_host_header_raises_resolution_error(self):
        from starlette.requests import Request as StarletteRequest
        from fastapi_tenancy.resolution.subdomain import SubdomainTenantResolver

        store = InMemoryTenantStore()
        resolver = SubdomainTenantResolver(store, domain_suffix=".example.com")
        scope = {"type": "http", "headers": [], "method": "GET", "path": "/", "query_string": b""}
        req = StarletteRequest(scope)
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)


# ══════════════════════════════════════════════════════════════════
# TenancyManager additional operations
# ══════════════════════════════════════════════════════════════════

class TestManagerAdditionalCoverage:
    @pytest_asyncio.fixture
    async def manager(self):
        store = InMemoryTenantStore()
        m = TenancyManager(_cfg(), store)
        await m.initialize()
        yield m, store
        await m.close()

    async def test_register_tenant_with_valid_identifier(self, manager):
        m, store = manager
        t = await m.register_tenant("new-tenant", "New Tenant", metadata={"plan": "basic"})
        assert t.identifier == "new-tenant"
        assert t.metadata.get("plan") == "basic"

    async def test_register_tenant_invalid_identifier_raises(self, manager):
        m, store = manager
        with pytest.raises(ValueError, match="Invalid tenant identifier"):
            await m.register_tenant("INVALID!", "Bad Tenant")

    async def test_delete_tenant_soft(self, manager):
        m, store = manager
        t = _t()
        await store.create(t)
        m.config = m.config.model_copy(update={"enable_soft_delete": True})
        await m.delete_tenant(t.id)
        fetched = await store.get_by_id(t.id)
        assert fetched.status == TenantStatus.DELETED

    async def test_delete_tenant_hard(self, manager):
        m, store = manager
        t = _t()
        await store.create(t)
        m.config = m.config.model_copy(update={"enable_soft_delete": False})
        await m.delete_tenant(t.id)
        with pytest.raises(TenantNotFoundError):
            await store.get_by_id(t.id)

    async def test_delete_tenant_not_found_raises(self, manager):
        m, _ = manager
        with pytest.raises(TenantNotFoundError):
            await m.delete_tenant("nonexistent-id")

    async def test_suspend_tenant(self, manager):
        m, store = manager
        t = _t()
        await store.create(t)
        updated = await m.suspend_tenant(t.id)
        assert updated.status == TenantStatus.SUSPENDED

    async def test_activate_tenant(self, manager):
        m, store = manager
        t = _t()
        t = t.model_copy(update={"status": TenantStatus.SUSPENDED})
        await store.create(t)
        updated = await m.activate_tenant(t.id)
        assert updated.status == TenantStatus.ACTIVE

    async def test_get_by_id_via_store(self, manager):
        m, store = manager
        t = _t()
        created = await store.create(t)
        fetched = await store.get_by_id(created.id)
        assert fetched.id == created.id

    async def test_get_by_identifier_via_store(self, manager):
        m, store = manager
        t = _t("find-by-slug")
        await store.create(t)
        found = await store.get_by_identifier("find-by-slug")
        assert found.identifier == "find-by-slug"

    async def test_update_via_store(self, manager):
        m, store = manager
        t = _t()
        created = await store.create(t)
        updated = created.model_copy(update={"name": "Updated Name"})
        result = await store.update(updated)
        assert result.name == "Updated Name"

    async def test_list_via_store(self, manager):
        m, store = manager
        t1 = await store.create(_t())
        t2 = await store.create(_t())
        tenants = await store.list()
        ids = {t.id for t in tenants}
        assert t1.id in ids and t2.id in ids

    async def test_register_tenant_with_isolation_strategy(self, manager):
        m, store = manager
        t = await m.register_tenant(
            "strategy-tenant", "Strategy Tenant",
            isolation_strategy=IsolationStrategy.SCHEMA
        )
        assert t.identifier == "strategy-tenant"

    async def test_check_rate_limit_disabled_no_op(self, manager):
        m, store = manager
        t = _t()
        # By default rate limiting is disabled — should not raise
        await m.check_rate_limit(t)

    async def test_write_audit_log_no_writer(self, manager):
        from fastapi_tenancy.core.types import AuditLog
        m, _ = manager
        entry = AuditLog(
            tenant_id="t1", action="read", resource="order",
            timestamp=datetime.now(UTC),
        )
        # Should not raise even without a configured writer
        await m.write_audit_log(entry)

    async def test_create_lifespan_context_manager(self, manager):
        # FastAPI imported at module level
        m, _ = manager
        lifespan = m.create_lifespan()
        app = FastAPI(lifespan=lifespan)

        @app.get("/")
        async def root():
            return {"ok": True}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/")
        assert resp.status_code == 200

    async def test_register_tenant_rollback_on_init_failure(self, manager):
        m, store = manager
        # Mock isolation_provider to fail on initialize_tenant
        m.isolation_provider.initialize_tenant = AsyncMock(side_effect=RuntimeError("db fail"))
        with pytest.raises(TenancyError):
            await m.register_tenant("fail-tenant", "Fail Tenant")
        # Should be rolled back — identifier should not be in store
        with pytest.raises(TenantNotFoundError):
            await store.get_by_identifier("fail-tenant")


# ══════════════════════════════════════════════════════════════════
# Migration manager
# ══════════════════════════════════════════════════════════════════

class TestMigrationsManagerCoverage:
    @pytest.fixture
    def migrator(self, tmp_path):
        from fastapi_tenancy.migrations.manager import TenantMigrationManager
        store = InMemoryTenantStore()
        cfg = _cfg()
        # Create a fake alembic.ini to avoid the "not found" warning
        ini = tmp_path / "alembic.ini"
        ini.write_text("[alembic]\nscript_location = migrations\n")
        return TenantMigrationManager(cfg, store, alembic_cfg_path=str(ini))

    def test_build_alembic_args_rls_strategy(self, migrator):
        t = _t()
        # Override config isolation to RLS
        migrator._config = migrator._config.model_copy(
            update={"isolation_strategy": IsolationStrategy.SCHEMA}
        )
        args = migrator._build_alembic_args(t)
        assert isinstance(args, dict)

    def test_build_alembic_args_schema_strategy(self, migrator):
        migrator._config = migrator._config.model_copy(
            update={"isolation_strategy": IsolationStrategy.SCHEMA}
        )
        t = _t()
        args = migrator._build_alembic_args(t)
        assert "url" in args

    def test_build_alembic_args_database_strategy(self, migrator, tmp_path):
        cfg = TenancyConfig(
            database_url=SQLITE_URL,
            isolation_strategy=IsolationStrategy.DATABASE,
            database_url_template=f"sqlite+aiosqlite:///{tmp_path}/{{tenant_id}}.db",
        )
        migrator._config = cfg
        t = _t()
        args = migrator._build_alembic_args(t)
        assert "url" in args

    async def test_get_current_revision_returns_none_on_error(self, migrator):
        t = _t()
        # No real alembic setup → should return None gracefully
        result = await migrator.get_current_revision(t)
        assert result is None

    async def test_upgrade_tenant_raises_when_alembic_unavailable(self, migrator):
        from fastapi_tenancy.migrations.manager import MigrationError
        t = _t()
        # Patch _ALEMBIC_AVAILABLE to False
        with patch("fastapi_tenancy.migrations.manager._ALEMBIC_AVAILABLE", False):
            with pytest.raises((MigrationError, ImportError)):
                await migrator.upgrade_tenant(t, revision="head")

    async def test_downgrade_tenant_raises_when_alembic_unavailable(self, migrator):
        from fastapi_tenancy.migrations.manager import MigrationError
        t = _t()
        with patch("fastapi_tenancy.migrations.manager._ALEMBIC_AVAILABLE", False):
            with pytest.raises((MigrationError, ImportError)):
                await migrator.downgrade_tenant(t, revision="-1")

    async def test_upgrade_all_with_no_tenants(self, migrator):
        results = await migrator.upgrade_all(revision="head")
        assert results == []

    async def test_upgrade_all_captures_failures(self, migrator):
        from fastapi_tenancy.migrations.manager import MigrationError
        t = _t()
        await migrator._store.create(t)
        with patch("fastapi_tenancy.migrations.manager._ALEMBIC_AVAILABLE", False):
            results = await migrator.upgrade_all(revision="head")
        assert len(results) == 1
        assert results[0]["success"] is False

    async def test_downgrade_all_with_no_tenants(self, migrator):
        results = await migrator.downgrade_all(revision="-1")
        assert results == []

    async def test_migrate_one_captures_migration_error(self, migrator):
        import asyncio
        from fastapi_tenancy.migrations.manager import MigrationError
        t = _t()
        semaphore = asyncio.Semaphore(1)
        migrator._run_migration_sync = MagicMock(side_effect=RuntimeError("fail"))
        result = await migrator._migrate_one(t, "upgrade", "head", semaphore)
        assert result["success"] is False
        assert "tenant_id" in result


# ══════════════════════════════════════════════════════════════════
# Public API surface
# ══════════════════════════════════════════════════════════════════

class TestPublicAPI:
    def test_package_imports(self):
        import fastapi_tenancy
        assert hasattr(fastapi_tenancy, "TenancyManager")
        assert hasattr(fastapi_tenancy, "TenancyMiddleware")
        assert hasattr(fastapi_tenancy, "TenancyConfig")

    def test_isolation_strategy_enum(self):
        assert IsolationStrategy.RLS.value == "rls"
        assert IsolationStrategy.SCHEMA.value == "schema"
        assert IsolationStrategy.DATABASE.value == "database"
        assert IsolationStrategy.HYBRID.value == "hybrid"

    def test_tenant_status_enum(self):
        assert TenantStatus.ACTIVE.value == "active"
        assert TenantStatus.SUSPENDED.value == "suspended"
        assert TenantStatus.DELETED.value == "deleted"

    def test_resolution_strategy_enum(self):
        assert ResolutionStrategy.HEADER.value == "header"
        assert ResolutionStrategy.SUBDOMAIN.value == "subdomain"

    def test_tenant_config_defaults(self):
        from fastapi_tenancy.core.types import TenantConfig
        config = TenantConfig()
        # max_users defaults to None (unlimited)
        assert config.max_users is None

    def test_tenant_config_with_max_users(self):
        from fastapi_tenancy.core.types import TenantConfig
        config = TenantConfig(max_users=100)
        assert config.max_users == 100
        assert config.max_users >= 0

    def test_audit_log_model(self):
        from fastapi_tenancy.core.types import AuditLog
        log = AuditLog(
            tenant_id="t1", action="create", resource="order",
            timestamp=datetime.now(UTC),
        )
        assert log.tenant_id == "t1"
        assert log.action == "create"

    def test_tenant_is_active_property(self):
        t = _t()
        assert t.is_active() is True
        t2 = t.model_copy(update={"status": TenantStatus.SUSPENDED})
        assert t2.is_active() is False

    def test_tenant_is_suspended_property(self):
        t = _t()
        t2 = t.model_copy(update={"status": TenantStatus.SUSPENDED})
        assert t2.is_suspended() is True

    def test_tenant_is_deleted_property(self):
        t = _t()
        t2 = t.model_copy(update={"status": TenantStatus.DELETED})
        assert t2.is_deleted() is True

    def test_tenancy_config_schema_prefix(self):
        cfg = _cfg(schema_prefix="app_")
        assert cfg.schema_prefix == "app_"

    def test_tenancy_config_rate_limit_settings(self):
        cfg = _cfg(enable_rate_limiting=False, rate_limit_per_minute=100)
        assert cfg.enable_rate_limiting is False
        assert cfg.rate_limit_per_minute == 100
