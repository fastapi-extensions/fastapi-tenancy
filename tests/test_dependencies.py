"""Tests for :mod:`fastapi_tenancy.dependencies`."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import text

from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.context import (
    TenantContext,
    get_current_tenant,
    get_current_tenant_optional,
)
from fastapi_tenancy.core.types import IsolationStrategy, ResolutionStrategy, Tenant, TenantStatus
from fastapi_tenancy.dependencies import (
    TenantDep,
    TenantOptionalDep,
    make_audit_log_dependency,
    make_tenant_config_dependency,
    make_tenant_db_dependency,
)
from fastapi_tenancy.manager import TenancyManager
from fastapi_tenancy.storage.memory import InMemoryTenantStore

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession


def _cfg(**kw: Any) -> TenancyConfig:
    defaults: dict[str, Any] = {
        "database_url": "sqlite+aiosqlite:///:memory:",
        "resolution_strategy": ResolutionStrategy.HEADER,
        "isolation_strategy": IsolationStrategy.SCHEMA,
        "tenant_header_name": "X-Tenant-ID",
    }
    defaults.update(kw)
    return TenancyConfig(**defaults)


def _tenant(
    identifier: str = "test-tenant",
    metadata: dict[str, Any] | None = None,
    status: TenantStatus = TenantStatus.ACTIVE,
) -> Tenant:
    now = datetime.now(UTC)
    return Tenant(
        id=f"t-{identifier}",
        identifier=identifier,
        name=identifier.title(),
        status=status,
        metadata=metadata or {},
        created_at=now,
        updated_at=now,
    )


async def _build_manager(tenant: Tenant, **cfg_kwargs: Any) -> TenancyManager:
    cfg = _cfg(**cfg_kwargs)
    store = InMemoryTenantStore()
    await store.create(tenant)
    m = TenancyManager(cfg, store)
    await m.initialize()
    return m


def _make_app(manager: TenancyManager) -> FastAPI:
    """Return a FastAPI app with TenancyMiddleware pre-configured."""
    from fastapi_tenancy.middleware.tenancy import TenancyMiddleware  # noqa: PLC0415

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[Any, None]:
        yield

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(TenancyMiddleware, manager=manager, excluded_paths=["/health"])
    return app


class TestMakeTenantDbDependency:
    def test_returns_callable(self) -> None:
        """Factory returns a callable without raising."""
        m = TenancyManager(_cfg(), InMemoryTenantStore())
        dep = make_tenant_db_dependency(m)
        assert callable(dep)

    @pytest.mark.asyncio
    async def test_dependency_yields_async_session_in_route(self) -> None:
        """Inside a route the dependency yields an AsyncSession."""
        tenant = _tenant()
        manager = await _build_manager(tenant)
        app = _make_app(manager)

        get_db = make_tenant_db_dependency(manager)
        session_types: list[type] = []

        @app.get("/db-type")
        async def db_type(session: AsyncSession = Depends(get_db)) -> dict[str, str]:
            session_types.append(type(session))
            return {"type": type(session).__name__}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/db-type", headers={"X-Tenant-ID": "test-tenant"})

        assert resp.status_code == 200
        assert resp.json()["type"] == "AsyncSession"

    @pytest.mark.asyncio
    async def test_session_can_execute_select(self) -> None:
        """The yielded session is fully operational."""
        tenant = _tenant()
        manager = await _build_manager(tenant)
        app = _make_app(manager)

        get_db = make_tenant_db_dependency(manager)

        @app.get("/db-exec")
        async def db_exec(session: AsyncSession = Depends(get_db)) -> dict[str, Any]:
            result = await session.execute(text("SELECT 1 AS val"))
            row = result.first()
            return {"val": row[0] if row else None}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/db-exec", headers={"X-Tenant-ID": "test-tenant"})

        assert resp.status_code == 200
        assert resp.json()["val"] == 1

    @pytest.mark.asyncio
    async def test_multiple_requests_each_get_fresh_session(self) -> None:
        """Each request gets an independent session."""
        tenant = _tenant()
        manager = await _build_manager(tenant)
        app = _make_app(manager)

        get_db = make_tenant_db_dependency(manager)
        sessions_seen: list[AsyncSession] = []

        @app.get("/session-id")
        async def session_id(session: AsyncSession = Depends(get_db)) -> dict[str, int]:
            sessions_seen.append(session)
            return {"id": id(session)}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r1 = await client.get("/session-id", headers={"X-Tenant-ID": "test-tenant"})
            r2 = await client.get("/session-id", headers={"X-Tenant-ID": "test-tenant"})

        assert r1.status_code == 200
        assert r2.status_code == 200
        # Both sessions must have been created (two distinct requests)
        assert len(sessions_seen) == 2
        # Keeping references prevents CPython from reusing the same memory
        # address for both objects — id() comparison is only reliable when
        # both objects are alive simultaneously.
        assert sessions_seen[0] is not sessions_seen[1]

    @pytest.mark.asyncio
    async def test_context_cleared_after_each_request(self) -> None:
        """Tenant context is None between requests."""
        tenant = _tenant()
        manager = await _build_manager(tenant)
        app = _make_app(manager)

        @app.get("/ping")
        async def ping() -> dict[str, str]:
            return {"ok": "yes"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.get("/ping", headers={"X-Tenant-ID": "test-tenant"})

        # After the request lifecycle, context must be reset
        assert TenantContext.get_optional() is None


class TestMakeTenantConfigDependency:
    def test_returns_callable(self) -> None:
        m = TenancyManager(_cfg(), InMemoryTenantStore())
        dep = make_tenant_config_dependency(m)
        assert callable(dep)

    @pytest.mark.asyncio
    async def test_returns_tenant_config_with_metadata(self) -> None:
        """Metadata fields are parsed into TenantConfig fields."""
        tenant = _tenant(metadata={"max_users": 50, "rate_limit_per_minute": 200})
        manager = await _build_manager(tenant)
        app = _make_app(manager)

        get_cfg = make_tenant_config_dependency(manager)

        @app.get("/cfg")
        async def get_config(cfg: Any = Depends(get_cfg)) -> dict[str, Any]:
            return {"max_users": cfg.max_users, "rate_limit_per_minute": cfg.rate_limit_per_minute}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/cfg", headers={"X-Tenant-ID": "test-tenant"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["max_users"] == 50
        assert data["rate_limit_per_minute"] == 200

    @pytest.mark.asyncio
    async def test_empty_metadata_returns_defaults(self) -> None:
        """Empty metadata → all TenantConfig defaults apply."""
        tenant = _tenant(metadata={})
        manager = await _build_manager(tenant)
        app = _make_app(manager)

        get_cfg = make_tenant_config_dependency(manager)

        @app.get("/cfg-defaults")
        async def get_defaults(cfg: Any = Depends(get_cfg)) -> dict[str, Any]:
            return {
                "max_users": cfg.max_users,
                "max_storage_gb": cfg.max_storage_gb,
                "rate_limit_per_minute": cfg.rate_limit_per_minute,
            }

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/cfg-defaults", headers={"X-Tenant-ID": "test-tenant"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["max_users"] is None
        assert data["max_storage_gb"] is None
        assert data["rate_limit_per_minute"] == 100  # default


class TestMakeAuditLogDependency:
    def test_returns_callable(self) -> None:
        m = TenancyManager(_cfg(), InMemoryTenantStore())
        dep = make_audit_log_dependency(m)
        assert callable(dep)

    @pytest.mark.asyncio
    async def test_audit_log_callable_invokes_write(self) -> None:
        """log() inside a route calls manager.write_audit_log."""

        tenant = _tenant()
        manager = await _build_manager(tenant)
        app = _make_app(manager)

        written_entries: list[Any] = []

        async def capture_write(entry: Any) -> None:
            written_entries.append(entry)

        manager._audit_writer.write = capture_write

        get_audit = make_audit_log_dependency(manager)

        @app.post("/resource")
        async def create_resource(audit: Any = Depends(get_audit)) -> dict[str, str]:
            await audit(action="create", resource="order", resource_id="o-123")
            return {"ok": "yes"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/resource", headers={"X-Tenant-ID": "test-tenant"})

        assert resp.status_code == 200
        assert len(written_entries) == 1
        entry = written_entries[0]
        assert entry.action == "create"
        assert entry.resource == "order"
        assert entry.resource_id == "o-123"
        assert entry.tenant_id == "t-test-tenant"

    @pytest.mark.asyncio
    async def test_audit_log_with_user_id_and_metadata(self) -> None:
        """log() passes user_id and metadata through."""
        tenant = _tenant()
        manager = await _build_manager(tenant)
        app = _make_app(manager)

        written_entries: list[Any] = []

        async def capture_write(entry: Any) -> None:
            written_entries.append(entry)

        manager._audit_writer.write = capture_write

        get_audit = make_audit_log_dependency(manager)

        @app.delete("/resource/{rid}")
        async def delete_resource(rid: str, audit: Any = Depends(get_audit)) -> dict[str, str]:
            await audit(
                action="delete",
                resource="order",
                resource_id=rid,
                user_id="admin-user",
                metadata={"reason": "user request"},
            )
            return {"ok": "yes"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete("/resource/o-999", headers={"X-Tenant-ID": "test-tenant"})

        assert resp.status_code == 200
        entry = written_entries[0]
        assert entry.user_id == "admin-user"
        assert entry.metadata == {"reason": "user request"}


class TestTypeAliases:
    def test_tenant_dep_is_annotated(self) -> None:
        """TenantDep is an Annotated type alias."""
        assert hasattr(TenantDep, "__metadata__") or str(TenantDep).startswith("typing.")

    def test_tenant_optional_dep_is_annotated(self) -> None:
        assert hasattr(TenantOptionalDep, "__metadata__") or str(TenantOptionalDep).startswith(
            "typing."
        )


class TestContextDependencies:
    @pytest.mark.asyncio
    async def test_get_current_tenant_raises_when_no_context(self) -> None:
        """get_current_tenant() must raise when no tenant is set."""
        from fastapi_tenancy.core.exceptions import TenantNotFoundError  # noqa: PLC0415

        TenantContext.clear()
        with pytest.raises(TenantNotFoundError):
            get_current_tenant()

    def test_get_current_tenant_returns_tenant_when_set(self) -> None:
        tenant = _tenant()
        token = TenantContext.set(tenant)
        try:
            result = get_current_tenant()
            assert result.id == tenant.id
        finally:
            TenantContext.reset(token)

    def test_get_current_tenant_optional_returns_none_when_unset(self) -> None:
        TenantContext.clear()
        result = get_current_tenant_optional()
        assert result is None

    def test_get_current_tenant_optional_returns_tenant_when_set(self) -> None:
        tenant = _tenant()
        token = TenantContext.set(tenant)
        try:
            result = get_current_tenant_optional()
            assert result is not None
            assert result.id == tenant.id
        finally:
            TenantContext.reset(token)

    @pytest.mark.asyncio
    async def test_route_returns_400_when_no_tenant_header(self) -> None:
        """Route using TenantDep returns error when middleware rejects request."""
        tenant = _tenant()
        manager = await _build_manager(tenant)
        app = _make_app(manager)

        @app.get("/needs-tenant")
        async def needs_tenant(t: TenantDep) -> dict[str, str]:
            return {"id": t.id}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/needs-tenant")  # no header

        # Middleware returns 400 before route is even called
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_route_with_optional_tenant_works_without_header(self) -> None:
        """Route using TenantOptionalDep is accessible even without a tenant header
        when it is on an excluded path."""
        manager = await _build_manager(_tenant())
        app = _make_app(manager)

        @app.get("/health")  # health is in excluded_paths → no middleware
        async def optional_tenant_route() -> dict[str, Any]:
            t = TenantContext.get_optional()
            return {"has_tenant": t is not None}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")

        # No middleware interference → route runs normally
        assert resp.status_code == 200
        assert resp.json()["has_tenant"] is False
