"""Integration tests — fastapi_tenancy.dependencies

Tests the three FastAPI dependency functions:
* get_tenant_db — requires isolation_provider on app.state
* require_active_tenant — raises 403 for inactive tenants
* get_tenant_config — builds TenantConfig from metadata

All tested by calling the dependency functions directly (not via TestClient)
or with minimal FastAPI setup to avoid needing a full database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fastapi_tenancy.core.context import TenantContext
from fastapi_tenancy.core.types import Tenant, TenantConfig, TenantStatus

pytestmark = pytest.mark.integration

_NOW = datetime(2024, 1, 1, tzinfo=UTC)


def _tenant(status: TenantStatus = TenantStatus.ACTIVE, metadata: dict | None = None) -> Tenant:
    return Tenant(
        id="t-001",
        identifier="acme-corp",
        name="Acme Corp",
        status=status,
        metadata=metadata or {},
        created_at=_NOW,
        updated_at=_NOW,
    )


@pytest.fixture(autouse=True)
def clear_ctx():
    TenantContext.clear()
    yield
    TenantContext.clear()


# ────────────────────── require_active_tenant ─────────────────────────────────


class TestRequireActiveTenant:
    async def test_active_tenant_returns_tenant(self):
        from fastapi import HTTPException

        from fastapi_tenancy.dependencies import require_active_tenant

        t = _tenant(status=TenantStatus.ACTIVE)
        result = await require_active_tenant(t)
        assert result is t

    async def test_suspended_tenant_raises_403(self):
        from fastapi import HTTPException

        from fastapi_tenancy.dependencies import require_active_tenant

        t = _tenant(status=TenantStatus.SUSPENDED)
        with pytest.raises(HTTPException) as exc:
            await require_active_tenant(t)
        assert exc.value.status_code == 403

    async def test_deleted_tenant_raises_403(self):
        from fastapi import HTTPException

        from fastapi_tenancy.dependencies import require_active_tenant

        t = _tenant(status=TenantStatus.DELETED)
        with pytest.raises(HTTPException) as exc:
            await require_active_tenant(t)
        assert exc.value.status_code == 403

    async def test_provisioning_tenant_raises_403(self):
        from fastapi import HTTPException

        from fastapi_tenancy.dependencies import require_active_tenant

        t = _tenant(status=TenantStatus.PROVISIONING)
        with pytest.raises(HTTPException) as exc:
            await require_active_tenant(t)
        assert exc.value.status_code == 403

    async def test_403_message_contains_identifier(self):
        from fastapi import HTTPException

        from fastapi_tenancy.dependencies import require_active_tenant

        t = _tenant(status=TenantStatus.SUSPENDED)
        with pytest.raises(HTTPException) as exc:
            await require_active_tenant(t)
        assert "acme-corp" in str(exc.value.detail)


# ──────────────────────── get_tenant_config ───────────────────────────────────


class TestGetTenantConfig:
    async def test_empty_metadata_returns_defaults(self):
        from fastapi_tenancy.dependencies import get_tenant_config

        t = _tenant(metadata={})
        cfg = await get_tenant_config(t)
        assert isinstance(cfg, TenantConfig)
        assert cfg.max_users is None
        assert cfg.rate_limit_per_minute == 100
        assert cfg.features_enabled == []

    async def test_metadata_max_users(self):
        from fastapi_tenancy.dependencies import get_tenant_config

        t = _tenant(metadata={"max_users": 50})
        cfg = await get_tenant_config(t)
        assert cfg.max_users == 50

    async def test_metadata_features_enabled(self):
        from fastapi_tenancy.dependencies import get_tenant_config

        t = _tenant(metadata={"features_enabled": ["sso", "audit"]})
        cfg = await get_tenant_config(t)
        assert "sso" in cfg.features_enabled

    async def test_metadata_rate_limit(self):
        from fastapi_tenancy.dependencies import get_tenant_config

        t = _tenant(metadata={"rate_limit_per_minute": 500})
        cfg = await get_tenant_config(t)
        assert cfg.rate_limit_per_minute == 500

    async def test_metadata_max_storage(self):
        from fastapi_tenancy.dependencies import get_tenant_config

        t = _tenant(metadata={"max_storage_gb": 100.0})
        cfg = await get_tenant_config(t)
        assert cfg.max_storage_gb == 100.0

    async def test_metadata_custom_settings(self):
        from fastapi_tenancy.dependencies import get_tenant_config

        t = _tenant(metadata={"custom_settings": {"theme": "dark"}})
        cfg = await get_tenant_config(t)
        assert cfg.custom_settings["theme"] == "dark"

    async def test_returns_tenant_config_instance(self):
        from fastapi_tenancy.dependencies import get_tenant_config

        t = _tenant()
        cfg = await get_tenant_config(t)
        assert isinstance(cfg, TenantConfig)


# ─────────────────────────── get_tenant_db ───────────────────────────────────


class TestGetTenantDb:
    async def test_raises_when_no_isolation_provider(self):
        from fastapi_tenancy.dependencies import get_tenant_db

        t = _tenant()
        request = MagicMock()
        request.app.state = MagicMock(spec=[])  # no isolation_provider attr

        with pytest.raises(RuntimeError, match="isolation_provider"):
            async for _ in get_tenant_db(t, request):
                pass

    async def test_yields_session_from_isolation_provider(self):
        from fastapi_tenancy.dependencies import get_tenant_db

        t = _tenant()
        mock_session = AsyncMock()
        mock_provider = AsyncMock()
        mock_provider.get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_provider.get_session.return_value.__aexit__ = AsyncMock(return_value=None)

        request = MagicMock()
        request.app.state.isolation_provider = mock_provider

        sessions = []
        async for session in get_tenant_db(t, request):
            sessions.append(session)

        assert sessions[0] is mock_session
