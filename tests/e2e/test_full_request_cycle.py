"""End-to-end tests — full FastAPI request lifecycle with TenancyManager

Tests the complete pipeline:
  HTTP request → TenancyMiddleware → resolver → store → TenantContext
  → route handler → response

Uses InMemoryTenantStore and header-based resolution.
No external services needed.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from fastapi_tenancy.core.context import TenantContext
from fastapi_tenancy.core.types import IsolationStrategy, ResolutionStrategy, Tenant, TenantStatus

pytestmark = pytest.mark.e2e

_NOW = datetime(2024, 1, 1, tzinfo=UTC)


def _tenant(
    id: str = "t-001",
    identifier: str = "acme-corp",
    name: str = "Acme Corp",
    status: TenantStatus = TenantStatus.ACTIVE,
    metadata: dict | None = None,
) -> Tenant:
    return Tenant(
        id=id,
        identifier=identifier,
        name=name,
        status=status,
        metadata=metadata or {},
        created_at=_NOW,
        updated_at=_NOW,
    )


@pytest.fixture
def multi_tenant_app():
    """Full FastAPI application with two seeded tenants."""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")

    from fastapi import FastAPI
    from fastapi_tenancy.core.config import TenancyConfig
    from fastapi_tenancy.manager import TenancyManager
    from fastapi_tenancy.middleware.tenancy import TenancyMiddleware
    from fastapi_tenancy.resolution.header import HeaderTenantResolver
    from fastapi_tenancy.storage.memory import InMemoryTenantStore

    store = InMemoryTenantStore()
    t1 = _tenant(id="t-001", identifier="acme-corp")
    t2 = _tenant(id="t-002", identifier="globex-corp", name="Globex Corp")

    import asyncio
    loop = asyncio.new_event_loop()
    loop.run_until_complete(store.create(t1))
    loop.run_until_complete(store.create(t2))
    loop.close()

    resolver = HeaderTenantResolver(tenant_store=store)

    app = FastAPI()
    app.add_middleware(
        TenancyMiddleware,
        resolver=resolver,
        debug_headers=True,
        skip_paths=["/health", "/metrics"],
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/me")
    async def whoami():
        t = TenantContext.get()
        return {
            "id": t.id,
            "identifier": t.identifier,
            "name": t.name,
            "status": t.status,
        }

    @app.get("/config")
    async def config():
        t = TenantContext.get()
        return {"metadata": t.metadata}

    return app


@pytest.fixture
async def client(multi_tenant_app):
    pytest.importorskip("httpx")
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=multi_tenant_app), base_url="http://test"
    ) as c:
        yield c


# ─────────────────────── Happy path — tenant 1 ───────────────────────────────


class TestTenantOne:
    async def test_whoami_acme(self, client):
        response = await client.get("/me", headers={"X-Tenant-ID": "acme-corp"})
        assert response.status_code == 200
        data = response.json()
        assert data["identifier"] == "acme-corp"
        assert data["id"] == "t-001"

    async def test_whoami_globex(self, client):
        response = await client.get("/me", headers={"X-Tenant-ID": "globex-corp"})
        assert response.status_code == 200
        data = response.json()
        assert data["identifier"] == "globex-corp"
        assert data["name"] == "Globex Corp"


# ─────────────────────── Multi-tenant isolation ───────────────────────────────


class TestMultiTenantIsolation:
    async def test_different_tenants_different_data(self, client):
        r1 = await client.get("/me", headers={"X-Tenant-ID": "acme-corp"})
        r2 = await client.get("/me", headers={"X-Tenant-ID": "globex-corp"})
        assert r1.json()["identifier"] != r2.json()["identifier"]

    async def test_sequential_requests_no_cross_contamination(self, client):
        for _ in range(3):
            r = await client.get("/me", headers={"X-Tenant-ID": "acme-corp"})
            assert r.json()["identifier"] == "acme-corp"
            r = await client.get("/me", headers={"X-Tenant-ID": "globex-corp"})
            assert r.json()["identifier"] == "globex-corp"


# ─────────────────────── Error cases ─────────────────────────────────────────


class TestErrorCases:
    async def test_missing_tenant_header_error(self, client):
        response = await client.get("/me")
        assert response.status_code >= 400

    async def test_unknown_tenant_identifier_error(self, client):
        response = await client.get("/me", headers={"X-Tenant-ID": "no-such-corp"})
        assert response.status_code >= 400

    async def test_invalid_identifier_format_error(self, client):
        response = await client.get("/me", headers={"X-Tenant-ID": "INVALID_ID!"})
        assert response.status_code >= 400


# ─────────────────────────── Skip paths ──────────────────────────────────────


class TestSkipPaths:
    async def test_health_no_header_needed(self, client):
        response = await client.get("/health")
        assert response.status_code == 200

    async def test_metrics_no_header_needed(self, client):
        # /metrics is in skip_paths but has no route — should get 404 not 400/403
        response = await client.get("/metrics")
        assert response.status_code == 404


# ─────────────────────── Metadata passthrough ─────────────────────────────────


class TestMetadataPassthrough:
    @pytest.fixture
    async def meta_client(self):
        pytest.importorskip("fastapi")
        pytest.importorskip("httpx")

        from fastapi import FastAPI
        from fastapi_tenancy.middleware.tenancy import TenancyMiddleware
        from fastapi_tenancy.resolution.header import HeaderTenantResolver
        from fastapi_tenancy.storage.memory import InMemoryTenantStore
        from httpx import ASGITransport, AsyncClient

        store = InMemoryTenantStore()
        t = _tenant(
            metadata={"plan": "pro", "max_users": 100, "features_enabled": ["sso"]},
        )
        import asyncio
        loop = asyncio.new_event_loop()
        loop.run_until_complete(store.create(t))
        loop.close()

        app = FastAPI()
        app.add_middleware(TenancyMiddleware, resolver=HeaderTenantResolver(tenant_store=store))

        @app.get("/config")
        async def config():
            t_ctx = TenantContext.get()
            return t_ctx.metadata

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c

    async def test_metadata_accessible_in_route(self, meta_client):
        response = await meta_client.get("/config", headers={"X-Tenant-ID": "acme-corp"})
        assert response.status_code == 200
        data = response.json()
        assert data.get("plan") == "pro"
        assert data.get("max_users") == 100
