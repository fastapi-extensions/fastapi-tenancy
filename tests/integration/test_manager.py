"""Integration tests — fastapi_tenancy.manager.TenancyManager

Tests manager lifecycle, initialization, resolver building, and
health/metric reporting using InMemoryTenantStore (no external services).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.types import (
    IsolationStrategy,
    ResolutionStrategy,
    Tenant,
    TenantStatus,
)
from fastapi_tenancy.manager import TenancyManager
from fastapi_tenancy.storage.memory import InMemoryTenantStore

pytestmark = pytest.mark.integration

_NOW = datetime(2024, 1, 1, tzinfo=UTC)


def _cfg(**kw) -> TenancyConfig:
    defaults = {
        "database_url": "sqlite+aiosqlite:///:memory:",
        "resolution_strategy": ResolutionStrategy.HEADER,
        "isolation_strategy": IsolationStrategy.SCHEMA,
    }
    defaults.update(kw)
    return TenancyConfig(**defaults)


def _tenant(identifier: str = "acme-corp") -> Tenant:
    return Tenant(
        id="t-001",
        identifier=identifier,
        name="Test",
        status=TenantStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )


# ─────────────────────────── Construction ────────────────────────────────────


class TestConstruction:
    def test_construction_no_io(self):
        cfg = _cfg()
        manager = TenancyManager(config=cfg)
        assert manager.config is cfg

    def test_not_initialized_after_construction(self):
        manager = TenancyManager(config=_cfg())
        assert manager._initialized is False

    def test_custom_store_accepted(self):
        store = InMemoryTenantStore()
        manager = TenancyManager(config=_cfg(), tenant_store=store)
        assert manager._custom_store is store

    def test_custom_resolver_accepted(self):
        from fastapi_tenancy.resolution.header import HeaderTenantResolver

        resolver = HeaderTenantResolver(tenant_store=InMemoryTenantStore())
        manager = TenancyManager(config=_cfg(), resolver=resolver)
        assert manager._custom_resolver is resolver


# ──────────────────────────── initialize ─────────────────────────────────────


class TestInitialize:
    async def test_initialize_completes(self):
        pytest.importorskip("aiosqlite")
        store = InMemoryTenantStore()
        manager = TenancyManager(config=_cfg(), tenant_store=store)
        await manager.initialize()
        assert manager._initialized is True
        await manager.shutdown()

    async def test_initialize_idempotent(self):
        pytest.importorskip("aiosqlite")
        store = InMemoryTenantStore()
        manager = TenancyManager(config=_cfg(), tenant_store=store)
        await manager.initialize()
        await manager.initialize()  # second call must not raise or double-init
        assert manager._initialized is True
        await manager.shutdown()

    async def test_resolver_available_after_init(self):
        pytest.importorskip("aiosqlite")
        store = InMemoryTenantStore()
        manager = TenancyManager(config=_cfg(), tenant_store=store)
        await manager.initialize()
        assert manager.resolver is not None
        await manager.shutdown()

    async def test_store_available_after_init(self):
        pytest.importorskip("aiosqlite")
        store = InMemoryTenantStore()
        manager = TenancyManager(config=_cfg(), tenant_store=store)
        await manager.initialize()
        assert manager.tenant_store is not None
        await manager.shutdown()


# ──────────────────────────── shutdown ───────────────────────────────────────


class TestShutdown:
    async def test_shutdown_without_init_ok(self):
        manager = TenancyManager(config=_cfg(), tenant_store=InMemoryTenantStore())
        await manager.shutdown()  # must not raise

    async def test_shutdown_after_init_ok(self):
        pytest.importorskip("aiosqlite")
        store = InMemoryTenantStore()
        manager = TenancyManager(config=_cfg(), tenant_store=store)
        await manager.initialize()
        await manager.shutdown()


# ────────────────────────── health_check ─────────────────────────────────────


class TestHealthCheck:
    async def test_health_check_after_init(self):
        pytest.importorskip("aiosqlite")
        store = InMemoryTenantStore()
        manager = TenancyManager(config=_cfg(), tenant_store=store)
        await manager.initialize()
        health = await manager.health_check()
        assert isinstance(health, dict)
        assert "status" in health
        await manager.shutdown()

    async def test_health_check_before_init(self):
        manager = TenancyManager(config=_cfg(), tenant_store=InMemoryTenantStore())
        health = await manager.health_check()
        assert isinstance(health, dict)


# ──────────────────────────── get_metrics ────────────────────────────────────


class TestGetMetrics:
    async def test_metrics_returns_dict(self):
        pytest.importorskip("aiosqlite")
        store = InMemoryTenantStore()
        manager = TenancyManager(config=_cfg(), tenant_store=store)
        await manager.initialize()
        metrics = await manager.get_metrics()
        assert isinstance(metrics, dict)
        await manager.shutdown()


# ──────────── create_lifespan (validates structure, not I/O) ─────────────────


class TestCreateLifespan:
    def test_returns_callable(self):
        cfg = _cfg()
        lifespan = TenancyManager.create_lifespan(cfg)
        assert callable(lifespan)

    def test_accepts_tenant_store_override(self):
        cfg = _cfg()
        store = InMemoryTenantStore()
        lifespan = TenancyManager.create_lifespan(cfg, tenant_store=store)
        assert callable(lifespan)
