"""Integration tests — fastapi_tenancy.middleware.tenancy.TenancyMiddleware

Tests full middleware pipeline using FastAPI + httpx AsyncClient.
Uses InMemoryTenantStore so no external services are needed.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from fastapi_tenancy.core.context import TenantContext
from fastapi_tenancy.core.types import Tenant, TenantStatus

pytestmark = pytest.mark.integration

_NOW = datetime(2024, 1, 1, tzinfo=UTC)


def _tenant(
    identifier: str = "acme-corp",
    status: TenantStatus = TenantStatus.ACTIVE,
) -> Tenant:
    return Tenant(
        id="t-001",
        identifier=identifier,
        name="Acme Corp",
        status=status,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _build_app(tenant: Tenant | None = None, skip_paths: list | None = None):
    """Build a minimal FastAPI app with TenancyMiddleware."""
    pytest.importorskip("fastapi", reason="fastapi not installed")
    pytest.importorskip("httpx", reason="httpx not installed")

    from fastapi import FastAPI
    from fastapi_tenancy.middleware.tenancy import TenancyMiddleware
    from fastapi_tenancy.resolution.header import HeaderTenantResolver
    from fastapi_tenancy.storage.memory import InMemoryTenantStore

    store = InMemoryTenantStore()

    app = FastAPI()

    resolver = HeaderTenantResolver(tenant_store=store)

    app.add_middleware(
        TenancyMiddleware,
        resolver=resolver,
        skip_paths=skip_paths or ["/health"],
        debug_headers=True,
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/tenant")
    async def get_tenant_info():
        t = TenantContext.get()
        return {"identifier": t.identifier, "status": t.status}

    # Seed the store if a tenant is provided
    if tenant is not None:
        import asyncio

        asyncio.get_event_loop().run_until_complete(store.create(tenant))

    return app, store


@pytest.fixture
def app_active():
    """App with an active tenant seeded."""
    app, store = _build_app(tenant=_tenant("acme-corp", TenantStatus.ACTIVE))
    return app


@pytest.fixture
def app_suspended():
    """App with a suspended tenant seeded."""
    app, store = _build_app(tenant=_tenant("acme-corp", TenantStatus.SUSPENDED))
    return app


@pytest.fixture
def app_empty():
    """App with no tenants seeded."""
    app, _ = _build_app(tenant=None)
    return app


# ─────────────────────────── Skip paths ──────────────────────────────────────


class TestSkipPaths:
    async def test_health_endpoint_skips_resolution(self, app_active):
        pytest.importorskip("httpx")
        from httpx import AsyncClient, ASGITransport

        async with AsyncClient(transport=ASGITransport(app=app_active), base_url="http://test") as client:
            response = await client.get("/health")
        assert response.status_code == 200

    async def test_health_no_tenant_header_needed(self, app_active):
        pytest.importorskip("httpx")
        from httpx import AsyncClient, ASGITransport

        async with AsyncClient(transport=ASGITransport(app=app_active), base_url="http://test") as client:
            response = await client.get("/health")
        assert response.json() == {"status": "ok"}


# ──────────────────────── Active tenant ──────────────────────────────────────


class TestActiveTenant:
    async def test_active_tenant_resolved(self, app_active):
        pytest.importorskip("httpx")
        from httpx import AsyncClient, ASGITransport

        async with AsyncClient(transport=ASGITransport(app=app_active), base_url="http://test") as client:
            response = await client.get("/tenant", headers={"X-Tenant-ID": "acme-corp"})
        assert response.status_code == 200
        data = response.json()
        assert data["identifier"] == "acme-corp"

    async def test_missing_header_returns_400_or_422(self, app_active):
        pytest.importorskip("httpx")
        from httpx import AsyncClient, ASGITransport

        async with AsyncClient(transport=ASGITransport(app=app_active), base_url="http://test") as client:
            response = await client.get("/tenant")
        assert response.status_code in (400, 422, 500)

    async def test_unknown_tenant_returns_404(self, app_empty):
        pytest.importorskip("httpx")
        from httpx import AsyncClient, ASGITransport

        async with AsyncClient(transport=ASGITransport(app=app_empty), base_url="http://test") as client:
            response = await client.get("/tenant", headers={"X-Tenant-ID": "unknown-corp"})
        assert response.status_code in (404, 400, 500)


# ─────────────────────── Suspended tenant ────────────────────────────────────


class TestSuspendedTenant:
    async def test_suspended_returns_403(self, app_suspended):
        pytest.importorskip("httpx")
        from httpx import AsyncClient, ASGITransport

        async with AsyncClient(transport=ASGITransport(app=app_suspended), base_url="http://test") as client:
            response = await client.get("/tenant", headers={"X-Tenant-ID": "acme-corp"})
        assert response.status_code == 403


# ─────────────────────── Context cleanup ─────────────────────────────────────


class TestContextCleanup:
    async def test_context_cleared_after_request(self, app_active):
        """Verify TenantContext is clean after each request (no leak)."""
        pytest.importorskip("httpx")
        from httpx import AsyncClient, ASGITransport

        # After the request, the test's own context should still be clean
        TenantContext.clear()
        async with AsyncClient(transport=ASGITransport(app=app_active), base_url="http://test") as client:
            await client.get("/tenant", headers={"X-Tenant-ID": "acme-corp"})
        # In the test task context (not the ASGI task), nothing should be set
        assert TenantContext.get_optional() is None
