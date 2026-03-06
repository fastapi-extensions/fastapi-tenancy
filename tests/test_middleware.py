"""Tests for :mod:`fastapi_tenancy.middleware.tenancy`."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.context import TenantContext
from fastapi_tenancy.core.exceptions import (
    RateLimitExceededError,
    TenancyError,
    TenantInactiveError,
)
from fastapi_tenancy.core.types import IsolationStrategy, ResolutionStrategy, Tenant, TenantStatus
from fastapi_tenancy.manager import TenancyManager
from fastapi_tenancy.middleware.tenancy import TenancyMiddleware, _json_response
from fastapi_tenancy.storage.memory import InMemoryTenantStore


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
    identifier: str = "acme-corp",
    status: TenantStatus = TenantStatus.ACTIVE,
) -> Tenant:
    now = datetime.now(UTC)
    return Tenant(
        id=f"t-{identifier}",
        identifier=identifier,
        name=identifier.title(),
        status=status,
        created_at=now,
        updated_at=now,
    )


async def _build_manager(tenant: Tenant, config: TenancyConfig | None = None) -> TenancyManager:
    cfg = config or _cfg()
    store = InMemoryTenantStore()
    await store.create(tenant)
    m = TenancyManager(cfg, store)
    await m.initialize()
    return m


def _make_fastapi_app(manager: TenancyManager, excluded_paths: list[str] | None = None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        TenancyMiddleware,
        manager=manager,
        excluded_paths=excluded_paths or [],
    )
    return app


class TestJsonResponseHelper:
    """Unit tests for the _json_response module-level helper."""

    @pytest.mark.asyncio
    async def test_sends_correct_status_and_body(self) -> None:
        sent: list[dict[str, Any]] = []

        async def fake_send(msg: dict[str, Any]) -> None:
            sent.append(msg)

        await _json_response(fake_send, 404, "Not found here")  # type: ignore[arg-type]

        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == 404
        assert sent[1]["type"] == "http.response.body"
        body = json.loads(sent[1]["body"])
        assert body == {"detail": "Not found here"}
        assert sent[1]["more_body"] is False

    @pytest.mark.asyncio
    async def test_content_type_header(self) -> None:
        sent: list[dict[str, Any]] = []

        async def fake_send(msg: dict[str, Any]) -> None:
            sent.append(msg)

        await _json_response(fake_send, 200, "ok")  # type: ignore[arg-type]
        headers = dict(sent[0]["headers"])
        assert headers[b"content-type"] == b"application/json"

    @pytest.mark.asyncio
    async def test_extra_headers_appended(self) -> None:
        sent: list[dict[str, Any]] = []

        async def fake_send(msg: dict[str, Any]) -> None:
            sent.append(msg)

        extra = [(b"retry-after", b"60")]
        await _json_response(fake_send, 429, "Too many", extra_headers=extra)  # type: ignore[arg-type]
        headers = dict(sent[0]["headers"])
        assert headers[b"retry-after"] == b"60"

    @pytest.mark.asyncio
    async def test_no_extra_headers_by_default(self) -> None:
        sent: list[dict[str, Any]] = []

        async def fake_send(msg: dict[str, Any]) -> None:
            sent.append(msg)

        await _json_response(fake_send, 200, "ok", extra_headers=None)  # type: ignore[arg-type]
        headers = dict(sent[0]["headers"])
        # content-type and content-length only
        assert b"content-type" in headers
        assert b"content-length" in headers


class TestMiddlewareInit:
    def test_default_excluded_is_empty(self) -> None:
        mw = TenancyMiddleware(MagicMock(), MagicMock())
        assert mw._excluded == []

    def test_explicit_excluded_paths(self) -> None:
        mw = TenancyMiddleware(MagicMock(), MagicMock(), excluded_paths=["/health", "/docs"])
        assert mw._excluded == ["/health", "/docs"]

    def test_is_excluded_returns_true_for_prefix_match(self) -> None:
        mw = TenancyMiddleware(MagicMock(), MagicMock(), excluded_paths=["/health"])
        assert mw._is_excluded("/health") is True
        assert mw._is_excluded("/health/check") is True

    def test_is_excluded_returns_false_for_no_match(self) -> None:
        mw = TenancyMiddleware(MagicMock(), MagicMock(), excluded_paths=["/health"])
        assert mw._is_excluded("/api/users") is False

    def test_is_excluded_empty_list_always_false(self) -> None:
        mw = TenancyMiddleware(MagicMock(), MagicMock())
        assert mw._is_excluded("/anything") is False


class TestNonHttpPassthrough:
    @pytest.mark.asyncio
    async def test_lifespan_scope_passes_through(self) -> None:
        received_scope: list[dict[str, Any]] = []

        async def fake_app(scope: Any, receive: Any, send: Any) -> None:
            received_scope.append(scope)

        mw = TenancyMiddleware(fake_app, MagicMock())
        scope = {"type": "lifespan"}
        await mw(scope, AsyncMock(), AsyncMock())
        assert received_scope[0]["type"] == "lifespan"

    @pytest.mark.asyncio
    async def test_unknown_scope_passes_through(self) -> None:
        received: list[Any] = []

        async def fake_app(scope: Any, receive: Any, send: Any) -> None:
            received.append(scope)

        mw = TenancyMiddleware(fake_app, MagicMock())
        scope = {"type": "custom_event"}
        await mw(scope, AsyncMock(), AsyncMock())
        assert received


class TestExcludedPaths:
    @pytest.mark.asyncio
    async def test_excluded_path_returns_app_response(self) -> None:
        """Excluded paths get the app's own response, not middleware rejection."""
        tenant = _tenant()
        manager = await _build_manager(tenant)
        app = _make_fastapi_app(manager, excluded_paths=["/health"])

        @app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")

        # App responds 200 because the path is excluded (no middleware rejection)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_non_excluded_path_without_tenant_gets_400(self) -> None:
        """Non-excluded path without tenant header gets rejected by middleware."""
        tenant = _tenant()
        manager = await _build_manager(tenant)
        app = _make_fastapi_app(manager, excluded_paths=["/health"])

        @app.get("/api/users")
        async def users() -> dict[str, str]:
            return {"users": "list"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/users")  # no X-Tenant-ID

        assert resp.status_code == 400


class TestResolutionErrors:
    @pytest.mark.asyncio
    async def test_tenant_resolution_error_returns_400(self) -> None:
        tenant = _tenant()
        manager = await _build_manager(tenant)
        app = _make_fastapi_app(manager)

        @app.get("/test")
        async def endpoint() -> dict[str, str]:
            return {"ok": "yes"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/test")  # No X-Tenant-ID header

        assert resp.status_code == 400
        assert "detail" in resp.json()

    @pytest.mark.asyncio
    async def test_tenant_not_found_returns_404(self) -> None:
        manager = await _build_manager(_tenant("known-tenant"))
        app = _make_fastapi_app(manager)

        @app.get("/test")
        async def endpoint() -> dict[str, str]:
            return {"ok": "yes"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/test", headers={"X-Tenant-ID": "unknown-tenant"})

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Tenant not found"

    @pytest.mark.asyncio
    async def test_generic_tenancy_error_during_resolution_returns_500(self) -> None:
        mock_manager = MagicMock()
        mock_manager.config = _cfg()
        mock_manager.resolver = MagicMock()
        mock_manager.resolver.resolve = AsyncMock(side_effect=TenancyError("unexpected boom"))

        app = FastAPI()
        app.add_middleware(TenancyMiddleware, manager=mock_manager)

        @app.get("/test")
        async def endpoint() -> dict[str, str]:
            return {"ok": "yes"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/test")

        assert resp.status_code == 500
        assert resp.json()["detail"] == "Internal tenancy error"


class TestInactiveTenants:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [
            TenantStatus.SUSPENDED,
            TenantStatus.DELETED,
            TenantStatus.PROVISIONING,
        ],
    )
    async def test_inactive_tenant_returns_403(self, status: TenantStatus) -> None:
        tenant = _tenant(identifier="inactive-tenant", status=status)
        manager = await _build_manager(tenant)
        app = _make_fastapi_app(manager)

        @app.get("/test")
        async def endpoint() -> dict[str, str]:
            return {"ok": "yes"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/test", headers={"X-Tenant-ID": "inactive-tenant"})

        assert resp.status_code == 403
        # Actual message: "Tenant is not active (status: suspended)"
        assert "not active" in resp.json()["detail"].lower()


class TestRateLimiting:
    @pytest.mark.asyncio
    async def test_rate_limit_disabled_proceeds_normally(self) -> None:
        """enable_rate_limiting=False → request proceeds without rate check."""
        tenant = _tenant()
        # NOTE: enable_rate_limiting=True requires redis_url, so use False
        config = _cfg(enable_rate_limiting=False)
        manager = await _build_manager(tenant, config=config)
        app = _make_fastapi_app(manager)

        @app.get("/test")
        async def endpoint() -> dict[str, str]:
            return {"ok": "yes"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/test", headers={"X-Tenant-ID": "acme-corp"})

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_rate_limit_exceeded_returns_429_with_retry_after(self) -> None:
        """When check_rate_limit raises RateLimitExceededError → 429 with Retry-After."""
        tenant = _tenant()
        # Use a mock manager to inject rate limit check failure
        mock_manager = MagicMock()
        mock_manager.config = _cfg(enable_rate_limiting=False)
        mock_manager.resolver = MagicMock()
        mock_manager.resolver.resolve = AsyncMock(return_value=tenant)
        # Patch config to say rate limiting is enabled
        mock_manager.config.enable_rate_limiting = True
        mock_manager.config.rate_limit_window_seconds = 60
        mock_manager.check_rate_limit = AsyncMock(
            side_effect=RateLimitExceededError(tenant_id=tenant.id, limit=100, window_seconds=60)
        )

        app = FastAPI()
        app.add_middleware(TenancyMiddleware, manager=mock_manager)

        @app.get("/test")
        async def endpoint() -> dict[str, str]:
            return {"ok": "yes"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/test", headers={"X-Tenant-ID": "acme-corp"})

        assert resp.status_code == 429
        assert "retry-after" in resp.headers


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_tenant_set_in_context_during_request(self) -> None:
        tenant = _tenant()
        manager = await _build_manager(tenant)
        app = _make_fastapi_app(manager)

        @app.get("/whoami")
        async def whoami() -> dict[str, str]:
            t = TenantContext.get()
            return {"id": t.id, "identifier": t.identifier}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/whoami", headers={"X-Tenant-ID": "acme-corp"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["identifier"] == "acme-corp"

    @pytest.mark.asyncio
    async def test_tenant_context_cleared_after_request(self) -> None:
        tenant = _tenant()
        manager = await _build_manager(tenant)
        app = _make_fastapi_app(manager)

        @app.get("/test")
        async def endpoint() -> dict[str, str]:
            return {"ok": "yes"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.get("/test", headers={"X-Tenant-ID": "acme-corp"})

        assert TenantContext.get_optional() is None


class TestInAppErrors:
    @pytest.mark.asyncio
    async def test_rate_limit_error_inside_app_returns_429(self) -> None:
        tenant = _tenant()
        manager = await _build_manager(tenant)
        app = _make_fastapi_app(manager)

        @app.get("/boom")
        async def boom() -> None:
            raise RateLimitExceededError(tenant_id=tenant.id, limit=10, window_seconds=60)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/boom", headers={"X-Tenant-ID": "acme-corp"})

        assert resp.status_code == 429

    @pytest.mark.asyncio
    async def test_tenant_inactive_error_inside_app_returns_403(self) -> None:
        tenant = _tenant()
        manager = await _build_manager(tenant)
        app = _make_fastapi_app(manager)

        @app.get("/inactive")
        async def inactive() -> None:
            raise TenantInactiveError(tenant_id=tenant.id, status="suspended")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/inactive", headers={"X-Tenant-ID": "acme-corp"})

        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_generic_tenancy_error_inside_app_returns_500(self) -> None:
        tenant = _tenant()
        manager = await _build_manager(tenant)
        app = _make_fastapi_app(manager)

        @app.get("/tenancy-err")
        async def tenancy_err() -> None:
            raise TenancyError("isolation broke")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/tenancy-err", headers={"X-Tenant-ID": "acme-corp"})

        assert resp.status_code == 500
        assert resp.json()["detail"] == "Internal tenancy error"


class TestScopeStateDictBranch:
    @pytest.mark.asyncio
    async def test_state_as_dict_stores_tenant(self) -> None:
        """When scope['state'] is a dict, tenant is stored as dict key."""
        tenant = _tenant()
        manager = await _build_manager(tenant)

        state_snapshots: list[Any] = []

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            state_snapshots.append(scope.get("state"))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}", "more_body": False})

        mw = TenancyMiddleware(inner_app, manager)

        scope: dict[str, Any] = {
            "type": "http",
            "method": "GET",
            "path": "/test",
            "query_string": b"",
            "headers": [(b"x-tenant-id", b"acme-corp")],
            "state": {},  # plain dict
        }

        async def fake_receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        sent: list[Any] = []

        async def fake_send(msg: Any) -> None:
            sent.append(msg)

        await mw(scope, fake_receive, fake_send)

        assert isinstance(state_snapshots[0], dict)
        assert "tenant" in state_snapshots[0]
        assert state_snapshots[0]["tenant"].identifier == "acme-corp"


class TestResponseAlreadyStarted:
    """Cover the 'response already started' else-branches in _handle().

    Each test sets up an inner app that:
    1. Sends ``http.response.start`` (marks response_started = True).
    2. Raises an in-app error.

    The middleware must NOT send a second http.response.start — it can only
    log. The test verifies no second response-start is emitted.
    """

    def _raw_scope(self, tenant_header: str = "acme-corp") -> dict[str, Any]:
        return {
            "type": "http",
            "method": "GET",
            "path": "/test",
            "query_string": b"",
            "headers": [(b"x-tenant-id", tenant_header.encode())],
        }

    async def _fake_receive(self) -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    @pytest.mark.asyncio
    async def test_rate_limit_after_response_started_does_not_send_second_start(self) -> None:
        tenant = _tenant()
        manager = await _build_manager(tenant)

        response_starts: list[Any] = []

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            raise RateLimitExceededError(tenant_id=tenant.id, limit=10, window_seconds=60)

        mw = TenancyMiddleware(inner_app, manager)
        sent: list[Any] = []

        async def fake_send(msg: Any) -> None:
            if msg.get("type") == "http.response.start":
                response_starts.append(msg)
            sent.append(msg)

        await mw(self._raw_scope(), self._fake_receive, fake_send)
        # Only the one response.start from the inner app; middleware must not add another.
        assert len(response_starts) == 1

    @pytest.mark.asyncio
    async def test_tenant_inactive_after_response_started_does_not_send_second_start(self) -> None:
        tenant = _tenant()
        manager = await _build_manager(tenant)

        response_starts: list[Any] = []

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            raise TenantInactiveError(tenant_id=tenant.id, status="suspended")

        mw = TenancyMiddleware(inner_app, manager)

        async def fake_send(msg: Any) -> None:
            if msg.get("type") == "http.response.start":
                response_starts.append(msg)

        await mw(self._raw_scope(), self._fake_receive, fake_send)
        assert len(response_starts) == 1

    @pytest.mark.asyncio
    async def test_tenancy_error_after_response_started_does_not_send_second_start(self) -> None:
        tenant = _tenant()
        manager = await _build_manager(tenant)

        response_starts: list[Any] = []

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            raise TenancyError("something broke after streaming started")

        mw = TenancyMiddleware(inner_app, manager)

        async def fake_send(msg: Any) -> None:
            if msg.get("type") == "http.response.start":
                response_starts.append(msg)

        await mw(self._raw_scope(), self._fake_receive, fake_send)
        assert len(response_starts) == 1

    @pytest.mark.asyncio
    async def test_context_always_restored_after_error_mid_stream(self) -> None:
        """TenantContext must be cleared even when error occurs after response started."""
        tenant = _tenant()
        manager = await _build_manager(tenant)

        async def inner_app(scope: Any, receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            raise TenancyError("mid-stream error")

        mw = TenancyMiddleware(inner_app, manager)

        async def noop_send(msg: Any) -> None:
            pass

        await mw(self._raw_scope(), self._fake_receive, noop_send)
        # Context must be cleaned up after the request, regardless of errors
        assert TenantContext.get_optional() is None
