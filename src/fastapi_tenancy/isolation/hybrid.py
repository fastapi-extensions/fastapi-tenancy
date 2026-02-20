"""Hybrid isolation provider — routes tenants to different strategies by tier.

The :class:`HybridIsolationProvider` enables a tiered SaaS model:

* **Premium tenants** — strong isolation (e.g. dedicated schema or database).
* **Standard tenants** — cost-efficient shared isolation (e.g. RLS or schema).

A single ``AsyncEngine`` is built here and injected into both sub-providers so
they share the same connection pool, avoiding duplicate resource allocation
when both strategies target the same database server.

Configuration::

    config = TenancyConfig(
        isolation_strategy="hybrid",
        premium_isolation_strategy="schema",  # dedicated schema per premium tenant
        standard_isolation_strategy="rls",    # shared tables for standard tenants
        premium_tenants=["acme-corp", "enterprise-org"],
    )
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from fastapi_tenancy.core.types import IsolationStrategy
from fastapi_tenancy.isolation.base import BaseIsolationProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from fastapi_tenancy.core.config import TenancyConfig
    from fastapi_tenancy.core.types import Tenant

logger = logging.getLogger(__name__)


class HybridIsolationProvider(BaseIsolationProvider):
    """Route tenant requests to the appropriate isolation strategy by tier.

    The routing decision is made by :meth:`_get_provider` based on whether
    the tenant's ID appears in :attr:`~fastapi_tenancy.core.config.TenancyConfig.premium_tenants`.

    The shared ``AsyncEngine`` is passed to sub-providers via their ``engine=``
    constructor argument so there is always exactly one connection pool,
    regardless of how many strategies are configured.

    Args:
        config: Tenancy configuration.  Must have
            :attr:`~fastapi_tenancy.core.config.TenancyConfig.isolation_strategy`
            set to ``"hybrid"`` with distinct
            :attr:`~fastapi_tenancy.core.config.TenancyConfig.premium_isolation_strategy`
            and
            :attr:`~fastapi_tenancy.core.config.TenancyConfig.standard_isolation_strategy`
            values.

    Example::

        config = TenancyConfig(
            database_url="postgresql+asyncpg://user:pass@localhost/myapp",
            isolation_strategy="hybrid",
            premium_isolation_strategy="schema",
            standard_isolation_strategy="rls",
            premium_tenants=["enterprise-org"],
        )
        provider = HybridIsolationProvider(config)

        # Premium tenant → SchemaIsolationProvider
        async with provider.get_session(enterprise_tenant) as session:
            ...

        # Standard tenant → RLSIsolationProvider
        async with provider.get_session(standard_tenant) as session:
            ...
    """

    def __init__(self, config: TenancyConfig) -> None:
        super().__init__(config)

        from fastapi_tenancy.utils.db_compat import detect_dialect, requires_static_pool

        dialect = detect_dialect(str(config.database_url))
        kw: dict[str, Any] = {"echo": config.database_echo}

        if requires_static_pool(dialect):
            from sqlalchemy.pool import StaticPool

            kw["poolclass"] = StaticPool
            kw["connect_args"] = {"check_same_thread": False}
        else:
            kw["pool_size"] = config.database_pool_size
            kw["max_overflow"] = config.database_max_overflow
            kw["pool_timeout"] = config.database_pool_timeout
            kw["pool_recycle"] = config.database_pool_recycle
            kw["pool_pre_ping"] = True

        self._shared_engine: AsyncEngine = create_async_engine(
            str(config.database_url), **kw
        )

        self._premium = self._create_provider(
            config.premium_isolation_strategy, self._shared_engine
        )
        self._standard = self._create_provider(
            config.standard_isolation_strategy, self._shared_engine
        )

        logger.info(
            "HybridIsolationProvider premium=%s standard=%s",
            config.premium_isolation_strategy.value,
            config.standard_isolation_strategy.value,
        )

    def _create_provider(
        self, strategy: IsolationStrategy, engine: AsyncEngine
    ) -> BaseIsolationProvider:
        """Instantiate a sub-provider that reuses *engine* (no duplicate pool)."""
        from fastapi_tenancy.isolation.database import DatabaseIsolationProvider
        from fastapi_tenancy.isolation.rls import RLSIsolationProvider
        from fastapi_tenancy.isolation.schema import SchemaIsolationProvider

        if strategy == IsolationStrategy.SCHEMA:
            return SchemaIsolationProvider(self.config, engine=engine)
        if strategy == IsolationStrategy.RLS:
            return RLSIsolationProvider(self.config, engine=engine)
        if strategy == IsolationStrategy.DATABASE:
            return DatabaseIsolationProvider(self.config, master_engine=engine)
        raise ValueError(
            f"Unsupported isolation strategy for HybridIsolationProvider: {strategy!r}"
        )

    def _get_provider(self, tenant: Tenant) -> BaseIsolationProvider:
        """Return the appropriate sub-provider for *tenant*'s tier."""
        return (
            self._premium
            if self.config.is_premium_tenant(tenant.id)
            else self._standard
        )

    # ------------------------------------------------------------------
    # Delegation
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def get_session(self, tenant: Tenant) -> AsyncIterator[AsyncSession]:
        """Yield a session from the provider appropriate for *tenant*'s tier."""
        async with self._get_provider(tenant).get_session(tenant) as session:
            yield session

    async def apply_filters(self, query: Any, tenant: Tenant) -> Any:
        """Delegate to the provider appropriate for *tenant*'s tier."""
        return await self._get_provider(tenant).apply_filters(query, tenant)

    async def initialize_tenant(self, tenant: Tenant) -> None:
        """Delegate to the provider appropriate for *tenant*'s tier."""
        await self._get_provider(tenant).initialize_tenant(tenant)

    async def destroy_tenant(self, tenant: Tenant) -> None:
        """Delegate to the provider appropriate for *tenant*'s tier."""
        await self._get_provider(tenant).destroy_tenant(tenant)

    async def verify_isolation(self, tenant: Tenant) -> bool:
        """Delegate to the provider appropriate for *tenant*'s tier."""
        return await self._get_provider(tenant).verify_isolation(tenant)

    async def close(self) -> None:
        """Dispose the shared engine — automatically closes all sub-providers."""
        await self._shared_engine.dispose()
        logger.info("HybridIsolationProvider closed")


__all__ = ["HybridIsolationProvider"]
