"""Tests for TenancyMiddleware via ASGI + httpx.AsyncClient."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_tenancy.core.context import TenantContext, get_current_tenant
from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.manager import TenancyManager
from fastapi_tenancy.middleware.tenancy import TenancyMiddleware
from fastapi_tenancy.storage.memory import InMemoryTenantStore
from starlette.requests import Request as _Request


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_app(
    manager: TenancyManager,
    excluded_paths: list[str] | None = None,
):
    """Return a minimal FastAPI app wrapped in TenancyMiddleware."""
    from fastapi import FastAPI

    app = FastAPI()
    app.add_middleware(
        TenancyMiddleware,
        manager=manager,
        excluded_paths=excluded_paths or ["/health"],
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/tenant")
    async def whoami():
        t = get_current_tenant()
        return {"id": t.id, "identifier": t.identifier, "status": t.status}

    @app.get("/meta")
    async def meta():
        return TenantContext.get_all_metadata()

    return app


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestMiddlewareHappyPath:
    async def test_tenant_resolved_and_injected(
        self, http_client: AsyncClient, acme: Tenant
    ):
        resp = await http_client.get("/tenant")
        assert resp.status_code == 200
        body = resp.json()
        assert body["identifier"] == acme.identifier
        assert body["status"] == "active"

    async def test_excluded_path_bypasses_resolution(self, http_client: AsyncClient):
        """Health endpoint should succeed with no tenant header."""
        async with AsyncClient(
            transport=ASGITransport(app=http_client._transport.app),  # type: ignore[attr-defined]
            base_url="http://testserver",
        ) as anon_client:
            resp = await anon_client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    async def test_context_cleared_after_request(
        self, manager: TenancyManager, acme: Tenant
    ):
        """After a request, the ContextVar must be reset (not leftover)."""
        app = await _make_app(manager)

        # After this request completes, context should be None for the next task
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers={"X-Tenant-ID": acme.identifier},
        ) as client:
            await client.get("/tenant")

        # In the same event loop, context should be clean
        assert TenantContext.get_optional() is None


# ---------------------------------------------------------------------------
# Error responses
# ---------------------------------------------------------------------------


class TestMiddlewareErrors:
    async def test_missing_header_returns_400(
        self, manager: TenancyManager, acme: Tenant
    ):
        app = await _make_app(manager)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            resp = await client.get("/tenant")
        assert resp.status_code == 400
        assert "detail" in resp.json()

    async def test_unknown_tenant_returns_404(
        self, manager: TenancyManager
    ):
        app = await _make_app(manager)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers={"X-Tenant-ID": "no-such-tenant"},
        ) as client:
            resp = await client.get("/tenant")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Tenant not found"

    async def test_suspended_tenant_returns_403(
        self, manager: TenancyManager, acme: Tenant
    ):
        await manager.store.set_status(acme.id, TenantStatus.SUSPENDED)
        app = await _make_app(manager)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers={"X-Tenant-ID": acme.identifier},
        ) as client:
            resp = await client.get("/tenant")
        assert resp.status_code == 403
        assert "not active" in resp.json()["detail"].lower()

    async def test_deleted_tenant_returns_403(
        self, manager: TenancyManager, acme: Tenant
    ):
        await manager.store.set_status(acme.id, TenantStatus.DELETED)
        app = await _make_app(manager)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers={"X-Tenant-ID": acme.identifier},
        ) as client:
            resp = await client.get("/tenant")
        assert resp.status_code == 403

    async def test_provisioning_tenant_returns_403(
        self, manager: TenancyManager, acme: Tenant
    ):
        await manager.store.set_status(acme.id, TenantStatus.PROVISIONING)
        app = await _make_app(manager)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers={"X-Tenant-ID": acme.identifier},
        ) as client:
            resp = await client.get("/tenant")
        assert resp.status_code == 403

    async def test_error_response_is_json(
        self, manager: TenancyManager
    ):
        app = await _make_app(manager)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            resp = await client.get("/tenant")
        assert resp.headers["content-type"].startswith("application/json")


# ---------------------------------------------------------------------------
# Non-HTTP scopes
# ---------------------------------------------------------------------------


class TestMiddlewareNonHttpScope:
    async def test_lifespan_scope_passed_through(self, manager: TenancyManager):
        """Non-http/websocket scopes must be forwarded unchanged."""
        passed_scopes = []

        async def fake_app(scope, receive, send):
            passed_scopes.append(scope["type"])

        middleware = TenancyMiddleware(fake_app, manager=manager, excluded_paths=[])
        await middleware({"type": "lifespan"}, None, None)
        assert "lifespan" in passed_scopes


# ---------------------------------------------------------------------------
# Excluded paths
# ---------------------------------------------------------------------------


class TestExcludedPaths:
    async def test_multiple_excluded_paths(self, manager: TenancyManager, acme: Tenant):
        from fastapi import FastAPI

        app = FastAPI()
        app.add_middleware(
            TenancyMiddleware,
            manager=manager,
            excluded_paths=["/health", "/metrics", "/docs"],
        )

        @app.get("/metrics")
        async def metrics():
            return {"metrics": True}

        @app.get("/protected")
        async def protected():
            t = get_current_tenant()
            return {"id": t.id}

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as anon:
            resp = await anon.get("/metrics")
            assert resp.status_code == 200
            # Protected needs tenant header
            resp2 = await anon.get("/protected")
            assert resp2.status_code == 400

    async def test_prefix_match_excludes_sub_paths(
        self, manager: TenancyManager
    ):
        from fastapi import FastAPI

        app = FastAPI()
        app.add_middleware(
            TenancyMiddleware,
            manager=manager,
            excluded_paths=["/public"],
        )

        @app.get("/public/assets/logo.png")
        async def logo():
            return {"file": "logo"}

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as anon:
            resp = await anon.get("/public/assets/logo.png")
            assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tenant attached to scope state
# ---------------------------------------------------------------------------


class TestTenantInScopeState:
    async def test_tenant_in_scope_state(self, manager: TenancyManager, acme: Tenant):
        from fastapi import FastAPI  # noqa: PLC0415

        app = FastAPI()
        app.add_middleware(
            TenancyMiddleware,
            manager=manager,
            excluded_paths=[],
        )

        @app.get("/scope-tenant")
        async def scope_tenant(request: _Request):
            t = request.state.tenant
            return {"id": t.id}

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers={"X-Tenant-ID": acme.identifier},
        ) as client:
            resp = await client.get("/scope-tenant")
        assert resp.status_code == 200
        assert resp.json()["id"] == acme.id


# ---------------------------------------------------------------------------
# Rate limiting (mocked)
# ---------------------------------------------------------------------------


class TestRateLimitMiddleware:
    async def test_rate_limit_exceeded_returns_429(
        self, manager: TenancyManager, acme: Tenant
    ):
        from fastapi_tenancy.core.exceptions import RateLimitExceededError

        # Monkey-patch check_rate_limit to raise
        async def _raise(tenant):
            raise RateLimitExceededError(
                tenant_id=tenant.id,
                limit=10,
                window_seconds=60,
            )

        manager.config = manager.config.model_copy(update={"enable_rate_limiting": True})
        manager.check_rate_limit = _raise

        app = await _make_app(manager)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers={"X-Tenant-ID": acme.identifier},
        ) as client:
            resp = await client.get("/tenant")
        assert resp.status_code == 429
        assert "retry-after" in resp.headers
