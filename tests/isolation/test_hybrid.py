"""Tests for :class:`~fastapi_tenancy.isolation.hybrid.HybridIsolationProvider`
and the :func:`~fastapi_tenancy.isolation.hybrid._build_provider` factory.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.exceptions import ConfigurationError, IsolationError
from fastapi_tenancy.core.types import IsolationStrategy, Tenant
from fastapi_tenancy.isolation.base import BaseIsolationProvider
from fastapi_tenancy.isolation.database import DatabaseIsolationProvider
from fastapi_tenancy.isolation.hybrid import HybridIsolationProvider, _build_provider
from fastapi_tenancy.isolation.rls import RLSIsolationProvider
from fastapi_tenancy.isolation.schema import SchemaIsolationProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable


def _hybrid_cfg(
    premium: IsolationStrategy = IsolationStrategy.SCHEMA,
    standard: IsolationStrategy = IsolationStrategy.DATABASE,
    premium_tenants: list[str] | None = None,
    db_url: str = "sqlite+aiosqlite:///:memory:",
    **extra: Any,
) -> TenancyConfig:
    base_extra: dict[str, Any] = {
        "database_url_template": "sqlite+aiosqlite:///./std_{database_name}.db",
    }
    base_extra.update(extra)
    return TenancyConfig(
        database_url=db_url,
        isolation_strategy=IsolationStrategy.HYBRID,
        premium_isolation_strategy=premium,
        standard_isolation_strategy=standard,
        premium_tenants=premium_tenants or ["premium-001"],
        schema_prefix="t_",
        database_echo=False,
        database_pool_size=2,
        database_max_overflow=2,
        database_pool_timeout=10,
        database_pool_recycle=300,
        database_pool_pre_ping=False,
        **base_extra,
    )


class _FakeProvider(BaseIsolationProvider):
    """Fake inner provider that records calls without touching any database."""

    def __init__(self, cfg: TenancyConfig) -> None:
        super().__init__(cfg)
        self.sessions_opened: list[Tenant] = []
        self.filters_applied: list[Tenant] = []
        self.initialized: list[Tenant] = []
        self.destroyed: list[Tenant] = []
        self.verified: list[Tenant] = []
        self.closed = False

    @asynccontextmanager
    async def get_session(self, tenant: Tenant) -> AsyncGenerator[Any, Any]:
        self.sessions_opened.append(tenant)
        session = MagicMock(spec=AsyncSession)
        yield session

    async def apply_filters(self, query: Any, tenant: Tenant) -> Any:
        self.filters_applied.append(tenant)
        return query

    async def initialize_tenant(self, tenant: Tenant, metadata: Any = None) -> None:
        self.initialized.append(tenant)

    async def destroy_tenant(self, tenant: Tenant, **kwargs: Any) -> None:
        self.destroyed.append(tenant)

    async def verify_isolation(self, tenant: Tenant) -> bool:
        self.verified.append(tenant)
        return True

    async def close(self) -> None:
        self.closed = True


@pytest.mark.unit
class TestBuildProvider:
    def test_schema_strategy_returns_schema_provider(self) -> None:
        cfg = TenancyConfig(
            database_url="sqlite+aiosqlite:///:memory:",
            isolation_strategy=IsolationStrategy.SCHEMA,
            schema_prefix="t_",
            database_echo=False,
            database_pool_size=2,
            database_max_overflow=2,
            database_pool_timeout=10,
            database_pool_recycle=300,
            database_pool_pre_ping=False,
        )
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        p = _build_provider(IsolationStrategy.SCHEMA, cfg, engine)
        assert isinstance(p, SchemaIsolationProvider)

    def test_database_strategy_returns_database_provider(self) -> None:
        cfg = TenancyConfig(
            database_url="sqlite+aiosqlite:///:memory:",
            isolation_strategy=IsolationStrategy.DATABASE,
            database_url_template="sqlite+aiosqlite:///./t_{database_name}.db",
            schema_prefix="t_",
            database_echo=False,
            database_pool_size=2,
            database_max_overflow=2,
            database_pool_timeout=10,
            database_pool_recycle=300,
            database_pool_pre_ping=False,
        )
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        p = _build_provider(IsolationStrategy.DATABASE, cfg, engine)
        assert isinstance(p, DatabaseIsolationProvider)

    def test_hybrid_strategy_raises_configuration_error(self) -> None:
        cfg = _hybrid_cfg()
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        with pytest.raises(ConfigurationError, match="cannot be used inside"):
            _build_provider(IsolationStrategy.HYBRID, cfg, engine)

    def test_rls_strategy_for_pg(self) -> None:
        cfg = TenancyConfig(
            database_url="postgresql+asyncpg://u:p@h/db",
            isolation_strategy=IsolationStrategy.RLS,
            schema_prefix="t_",
            database_echo=False,
            database_pool_size=2,
            database_max_overflow=2,
            database_pool_timeout=10,
            database_pool_recycle=300,
            database_pool_pre_ping=False,
        )
        engine = create_async_engine("postgresql+asyncpg://u:p@h/db")
        p = _build_provider(IsolationStrategy.RLS, cfg, engine)
        assert isinstance(p, RLSIsolationProvider)


@pytest.mark.unit
class TestHybridConstruction:
    def test_non_hybrid_strategy_raises(self) -> None:
        cfg = TenancyConfig(
            database_url="sqlite+aiosqlite:///:memory:",
            isolation_strategy=IsolationStrategy.SCHEMA,
            schema_prefix="t_",
            database_echo=False,
            database_pool_size=2,
            database_max_overflow=2,
            database_pool_timeout=10,
            database_pool_recycle=300,
            database_pool_pre_ping=False,
        )
        with pytest.raises(ConfigurationError, match="isolation_strategy='hybrid'"):
            HybridIsolationProvider(cfg)

    def test_premium_provider_property(self, hybrid_provider: HybridIsolationProvider) -> None:
        assert hybrid_provider.premium_provider is hybrid_provider._premium_provider

    def test_standard_provider_property(self, hybrid_provider: HybridIsolationProvider) -> None:
        assert hybrid_provider.standard_provider is hybrid_provider._standard_provider

    def test_shared_engine_created(self, hybrid_provider: HybridIsolationProvider) -> None:
        assert hybrid_provider._shared_engine is not None

    def test_explicit_engines_accepted(self) -> None:
        cfg = _hybrid_cfg()
        pe = create_async_engine("sqlite+aiosqlite:///:memory:")
        se = create_async_engine("sqlite+aiosqlite:///:memory:")
        p = HybridIsolationProvider(cfg, premium_engine=pe, standard_engine=se)
        assert p is not None


@pytest.mark.unit
class TestProviderForDispatch:
    def _make_hybrid_with_fakes(
        self, premium_tenants: list[str] | None = None
    ) -> tuple[HybridIsolationProvider, _FakeProvider, _FakeProvider]:
        """Build a HybridProvider with fake inner providers for dispatch testing."""
        cfg = _hybrid_cfg(premium_tenants=premium_tenants or ["premium-001"])
        p = HybridIsolationProvider(cfg)
        fake_premium = _FakeProvider(cfg)
        fake_standard = _FakeProvider(cfg)
        p._premium_provider = fake_premium
        p._standard_provider = fake_standard
        return p, fake_premium, fake_standard

    def test_premium_tenant_routes_to_premium(self, make_tenant: Callable[..., Tenant]) -> None:
        p, fake_prem, _ = self._make_hybrid_with_fakes(["premium-001"])
        t = make_tenant(tenant_id="premium-001")
        provider = p._provider_for(t)
        assert provider is fake_prem

    def test_standard_tenant_routes_to_standard(self, make_tenant: Callable[..., Tenant]) -> None:
        p, _, fake_std = self._make_hybrid_with_fakes(["premium-001"])
        t = make_tenant(tenant_id="standard-tenant")
        provider = p._provider_for(t)
        assert provider is fake_std

    def test_per_tenant_override_matching_premium_uses_premium(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        cfg = _hybrid_cfg(
            premium=IsolationStrategy.SCHEMA,
            standard=IsolationStrategy.DATABASE,
        )
        p = HybridIsolationProvider(cfg)
        fake_prem = _FakeProvider(cfg)
        fake_std = _FakeProvider(cfg)
        p._premium_provider = fake_prem
        p._standard_provider = fake_std
        # Override = SCHEMA which is premium strategy
        t = make_tenant(isolation_strategy=IsolationStrategy.SCHEMA)
        assert p._provider_for(t) is fake_prem

    def test_per_tenant_override_matching_standard_uses_standard(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        cfg = _hybrid_cfg(
            premium=IsolationStrategy.SCHEMA,
            standard=IsolationStrategy.DATABASE,
        )
        p = HybridIsolationProvider(cfg)
        fake_prem = _FakeProvider(cfg)
        fake_std = _FakeProvider(cfg)
        p._premium_provider = fake_prem
        p._standard_provider = fake_std
        t = make_tenant(isolation_strategy=IsolationStrategy.DATABASE)
        assert p._provider_for(t) is fake_std

    def test_unknown_per_tenant_override_raises_isolation_error(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        cfg = _hybrid_cfg(
            premium=IsolationStrategy.SCHEMA,
            standard=IsolationStrategy.DATABASE,
        )
        p = HybridIsolationProvider(cfg)
        # Override set to RLS which matches neither premium nor standard
        t = make_tenant(isolation_strategy=IsolationStrategy.RLS)
        with pytest.raises(IsolationError, match="_provider_for"):
            p._provider_for(t)


@pytest.mark.unit
class TestHybridDelegation:
    def _make_wired(
        self,
    ) -> tuple[HybridIsolationProvider, _FakeProvider, _FakeProvider, TenancyConfig]:
        cfg = _hybrid_cfg(premium_tenants=["premium-001"])
        p = HybridIsolationProvider(cfg)
        fake_prem = _FakeProvider(cfg)
        fake_std = _FakeProvider(cfg)
        p._premium_provider = fake_prem
        p._standard_provider = fake_std
        return p, fake_prem, fake_std, cfg

    async def test_get_session_delegates_to_premium(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        p, fake_prem, fake_std, _ = self._make_wired()
        t = make_tenant(tenant_id="premium-001")
        async with p.get_session(t):
            pass
        assert t in fake_prem.sessions_opened
        assert t not in fake_std.sessions_opened

    async def test_get_session_delegates_to_standard(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        p, fake_prem, fake_std, _ = self._make_wired()
        t = make_tenant(tenant_id="standard-tenant")
        async with p.get_session(t):
            pass
        assert t in fake_std.sessions_opened
        assert t not in fake_prem.sessions_opened

    async def test_apply_filters_delegates(self, make_tenant: Callable[..., Tenant]) -> None:
        p, fake_prem, _, _ = self._make_wired()
        t = make_tenant(tenant_id="premium-001")
        meta = sa.MetaData()
        tbl = sa.Table("t", meta, sa.Column("id", sa.Integer))
        q = sa.select(tbl)
        await p.apply_filters(q, t)
        assert t in fake_prem.filters_applied

    async def test_initialize_tenant_delegates(self, make_tenant: Callable[..., Tenant]) -> None:
        p, fake_prem, fake_std, _ = self._make_wired()
        t_prem = make_tenant(tenant_id="premium-001")
        t_std = make_tenant(tenant_id="standard-001")
        await p.initialize_tenant(t_prem)
        await p.initialize_tenant(t_std)
        assert t_prem in fake_prem.initialized
        assert t_std in fake_std.initialized

    async def test_destroy_tenant_delegates(self, make_tenant: Callable[..., Tenant]) -> None:
        p, _, fake_std, _ = self._make_wired()
        t = make_tenant(tenant_id="standard-for-destroy")
        await p.destroy_tenant(t)
        assert t in fake_std.destroyed

    async def test_verify_isolation_delegates(self, make_tenant: Callable[..., Tenant]) -> None:
        p, fake_prem, _, _ = self._make_wired()
        t = make_tenant(tenant_id="premium-001")
        result = await p.verify_isolation(t)
        assert result is True
        assert t in fake_prem.verified

    def test_get_provider_for_tenant(self, make_tenant: Callable[..., Tenant]) -> None:
        p, fake_prem, fake_std, _ = self._make_wired()
        t_prem = make_tenant(tenant_id="premium-001")
        t_std = make_tenant(tenant_id="other")
        assert p.get_provider_for_tenant(t_prem) is fake_prem
        assert p.get_provider_for_tenant(t_std) is fake_std


@pytest.mark.unit
class TestHybridClose:
    async def test_close_calls_inner_providers(self, make_tenant: Callable[..., Tenant]) -> None:
        cfg = _hybrid_cfg()
        p = HybridIsolationProvider(cfg)
        fake_prem = _FakeProvider(cfg)
        fake_std = _FakeProvider(cfg)
        p._premium_provider = fake_prem
        p._standard_provider = fake_std
        await p.close()
        assert fake_prem.closed
        assert fake_std.closed

    async def test_close_disposes_shared_engine(
        self, hybrid_provider: HybridIsolationProvider
    ) -> None:
        # Should complete without error
        await hybrid_provider.close()
        # Idempotent second close must also not raise
        await hybrid_provider._shared_engine.dispose()


@pytest.mark.e2e
class TestHybridPostgresIntegration:
    async def test_premium_uses_schema_provider(
        self,
        pg_hybrid_provider: HybridIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant(tenant_id="pg-premium-001")
        provider = pg_hybrid_provider.get_provider_for_tenant(t)
        assert isinstance(provider, SchemaIsolationProvider)

    async def test_standard_uses_rls_provider(
        self,
        pg_hybrid_provider: HybridIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant(tenant_id="pg-standard-001")
        provider = pg_hybrid_provider.get_provider_for_tenant(t)
        assert isinstance(provider, RLSIsolationProvider)
