"""Hybrid isolation provider — route requests to per-tenant strategy.

The :class:`HybridIsolationProvider` transparently dispatches every operation
to one of two inner providers based on whether a tenant is in the
*premium* tier (full schema or database isolation) or the *standard* tier
(shared tables with RLS or schema).

Architecture
------------
.. code-block::

    HybridIsolationProvider
    ├── premium_provider  (SchemaIsolationProvider  or DatabaseIsolationProvider)
    └── standard_provider (RLSIsolationProvider     or SchemaIsolationProvider)

A **single** :class:`~sqlalchemy.ext.asyncio.AsyncEngine` is created here and
injected into both inner providers.  This is the key design decision: it
prevents two separate connection pools from being created for the same
database URL, which would double resource usage.

If different dialects are needed for the two tiers (e.g. premium tenants on
a PostgreSQL cluster, standard tenants on a separate RDS instance), pass
pre-built :class:`~sqlalchemy.ext.asyncio.AsyncEngine` instances via
``premium_engine`` / ``standard_engine``.

Configuration
-------------
The decision of which provider to route to is made by::

    config.get_isolation_strategy_for_tenant(tenant.id)

which checks ``config.premium_tenants`` (a ``list[str]`` of tenant IDs).

Per-tenant override
-------------------
If ``tenant.isolation_strategy`` is set on the individual ``Tenant`` object,
that value takes precedence over both config-level routing and the hybrid
routing logic.  This allows single-tenant overrides without changing global
config.

Lifecycle
---------
Call :meth:`close` on application shutdown to dispose all engines.
Calling ``close`` on the inner providers is safe even when they share the
same engine — ``AsyncEngine.dispose()`` is idempotent and the shared engine
is disposed exactly once via ``self._shared_engine.dispose()``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from fastapi_tenancy.core.exceptions import ConfigurationError, IsolationError
from fastapi_tenancy.core.types import IsolationStrategy, SelectT
from fastapi_tenancy.isolation.base import BaseIsolationProvider
from fastapi_tenancy.utils.db_compat import detect_dialect, requires_static_pool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy import MetaData
    from sqlalchemy.ext.asyncio import AsyncSession

    from fastapi_tenancy.core.config import TenancyConfig
    from fastapi_tenancy.core.types import Tenant

logger = logging.getLogger(__name__)


def _build_provider(
    strategy: IsolationStrategy,
    config: TenancyConfig,
    engine: AsyncEngine,
) -> BaseIsolationProvider:
    """Instantiate the correct inner provider for *strategy* with a shared engine.

    Args:
        strategy: The isolation strategy to use.
        config: Tenancy configuration.
        engine: A pre-built ``AsyncEngine`` to inject (avoids duplicate pools).

    Returns:
        A configured :class:`BaseIsolationProvider` subclass instance.

    Raises:
        ConfigurationError: When *strategy* is not supported by this factory
            (``HYBRID`` is invalid here — nesting hybrid providers is forbidden).
    """
    if strategy == IsolationStrategy.SCHEMA:
        from fastapi_tenancy.isolation.schema import SchemaIsolationProvider  # noqa: PLC0415

        return SchemaIsolationProvider(config, engine=engine)

    if strategy == IsolationStrategy.DATABASE:
        from fastapi_tenancy.isolation.database import DatabaseIsolationProvider  # noqa: PLC0415

        return DatabaseIsolationProvider(config, master_engine=engine)

    if strategy == IsolationStrategy.RLS:
        from fastapi_tenancy.isolation.rls import RLSIsolationProvider  # noqa: PLC0415

        return RLSIsolationProvider(config, engine=engine)

    raise ConfigurationError(
        parameter="isolation_strategy",
        reason=(
            f"Strategy {strategy.value!r} cannot be used inside HybridIsolationProvider. "
            "Only SCHEMA, DATABASE, and RLS are valid inner strategies."
        ),
    )


class HybridIsolationProvider(BaseIsolationProvider):
    """Two-tier hybrid isolation: premium → strong; standard → economical.

    Premium and standard isolation strategies are set in ``TenancyConfig``::

        config = TenancyConfig(
            ...
            isolation_strategy="hybrid",
            premium_tenants=["tenant-enterprise-1", "tenant-enterprise-2"],
            premium_isolation_strategy="schema",
            standard_isolation_strategy="rls",
        )

    Then::

        provider = HybridIsolationProvider(config)

        # Premium tenant → SchemaIsolationProvider
        async with provider.get_session(premium_tenant) as session: ...

        # Standard tenant → RLSIsolationProvider
        async with provider.get_session(standard_tenant) as session: ...

    Args:
        config: Application-wide tenancy configuration.
        premium_engine: Pre-built engine to inject into the premium provider.
            When ``None`` (default), a shared engine is created for both
            inner providers.
        standard_engine: Pre-built engine to inject into the standard
            provider.  When ``None``, the shared engine is used.

    Raises:
        ConfigurationError: When ``config.isolation_strategy`` is not
            ``HYBRID``, or when the inner strategies are invalid.
    """

    def __init__(
        self,
        config: TenancyConfig,
        premium_engine: AsyncEngine | None = None,
        standard_engine: AsyncEngine | None = None,
    ) -> None:
        super().__init__(config)

        if config.isolation_strategy != IsolationStrategy.HYBRID:
            raise ConfigurationError(
                parameter="isolation_strategy",
                reason=(
                    "HybridIsolationProvider requires isolation_strategy='hybrid' "
                    f"but got {config.isolation_strategy.value!r}."
                ),
            )

        # Build a shared engine when explicit engines are not supplied.
        dialect = detect_dialect(str(config.database_url))
        kw: dict[str, Any] = {"echo": config.database_echo}
        if requires_static_pool(dialect):
            kw["poolclass"] = StaticPool
            kw["connect_args"] = {"check_same_thread": False}
        else:
            kw["pool_size"] = config.database_pool_size
            kw["max_overflow"] = config.database_max_overflow
            kw["pool_pre_ping"] = config.database_pool_pre_ping
            kw["pool_recycle"] = config.database_pool_recycle

        self._shared_engine: AsyncEngine = create_async_engine(str(config.database_url), **kw)

        # Inner providers share the same physical engine.
        effective_premium_engine = premium_engine or self._shared_engine
        effective_standard_engine = standard_engine or self._shared_engine

        self._premium_provider: BaseIsolationProvider = _build_provider(
            config.premium_isolation_strategy,
            config,
            effective_premium_engine,
        )
        self._standard_provider: BaseIsolationProvider = _build_provider(
            config.standard_isolation_strategy,
            config,
            effective_standard_engine,
        )

        logger.info(
            "HybridIsolationProvider ready: premium=%s standard=%s shared_engine=%s",
            config.premium_isolation_strategy.value,
            config.standard_isolation_strategy.value,
            id(self._shared_engine),
        )

    ###################
    # Dispatch helper #
    ###################

    def _provider_for(self, tenant: Tenant) -> BaseIsolationProvider:
        """Return the correct inner provider for *tenant*.

        Per-tenant ``isolation_strategy`` overrides the config-level routing.

        Args:
            tenant: The tenant being served.

        Returns:
            The inner :class:`BaseIsolationProvider` to delegate to.

        Raises:
            IsolationError: When a per-tenant override specifies an
                unsupported strategy for this provider.
        """
        # Per-tenant override takes precedence over config routing.
        if tenant.isolation_strategy is not None:
            override = tenant.isolation_strategy
            if override == self.config.premium_isolation_strategy:
                return self._premium_provider
            if override == self.config.standard_isolation_strategy:
                return self._standard_provider
            raise IsolationError(
                operation="_provider_for",
                tenant_id=tenant.id,
                details={
                    "override": override.value,
                    "valid_strategies": [
                        self.config.premium_isolation_strategy.value,
                        self.config.standard_isolation_strategy.value,
                    ],
                },
            )

        effective = self.config.get_isolation_strategy_for_tenant(tenant.id)
        return (
            self._premium_provider
            if effective == self.config.premium_isolation_strategy
            else self._standard_provider
        )

    #######################################################################
    # IsolationProvider interface — all delegate to the resolved provider #
    #######################################################################

    @asynccontextmanager
    async def get_session(self, tenant: Tenant) -> AsyncIterator[AsyncSession]:
        """Yield a session from the tier-appropriate inner provider.

        Args:
            tenant: Currently active tenant.

        Yields:
            Configured :class:`~sqlalchemy.ext.asyncio.AsyncSession`.

        Raises:
            IsolationError: When the session cannot be obtained.
        """
        provider = self._provider_for(tenant)
        async with provider.get_session(tenant) as session:
            yield session

    async def apply_filters(self, query: SelectT, tenant: Tenant) -> SelectT:
        """Delegate query filtering to the tier-appropriate inner provider.

        Args:
            query: SQLAlchemy ``Select`` query.
            tenant: Currently active tenant.

        Returns:
            Filtered query.
        """
        return await self._provider_for(tenant).apply_filters(query, tenant)

    async def initialize_tenant(
        self,
        tenant: Tenant,
        metadata: MetaData | None = None,
    ) -> None:
        """Provision *tenant*'s isolation namespace using the correct inner provider.

        Args:
            tenant: Tenant to provision.
            metadata: Application :class:`~sqlalchemy.MetaData`.

        Raises:
            IsolationError: When provisioning fails.
        """
        provider = self._provider_for(tenant)
        logger.info(
            "HybridIsolationProvider: initialising tenant %s via %s",
            tenant.id,
            type(provider).__name__,
        )
        await provider.initialize_tenant(tenant, metadata=metadata)

    async def destroy_tenant(self, tenant: Tenant, **kwargs: Any) -> None:
        """Deprovision *tenant* using the correct inner provider.

        .. warning::
            Permanently destroys all tenant data.

        Args:
            tenant: Tenant to destroy.
            **kwargs: Forwarded to the inner provider's ``destroy_tenant``.

        Raises:
            IsolationError: When deprovisioning fails.
        """
        provider = self._provider_for(tenant)
        logger.warning(
            "HybridIsolationProvider: destroying tenant %s via %s",
            tenant.id,
            type(provider).__name__,
        )
        await provider.destroy_tenant(tenant, **kwargs)

    async def verify_isolation(self, tenant: Tenant) -> bool:
        """Delegate isolation verification to the correct inner provider.

        Args:
            tenant: Tenant to verify.

        Returns:
            ``True`` when isolation is verified.
        """
        return await self._provider_for(tenant).verify_isolation(tenant)

    #################
    # Introspection #
    #################

    def get_provider_for_tenant(self, tenant: Tenant) -> BaseIsolationProvider:
        """Return the inner provider that would handle *tenant* (no I/O).

        Useful for introspection and testing.

        Args:
            tenant: Target tenant.

        Returns:
            The inner :class:`BaseIsolationProvider`.
        """
        return self._provider_for(tenant)

    @property
    def premium_provider(self) -> BaseIsolationProvider:
        """The inner provider used for premium tenants."""
        return self._premium_provider

    @property
    def standard_provider(self) -> BaseIsolationProvider:
        """The inner provider used for standard tenants."""
        return self._standard_provider

    async def close(self) -> None:
        """Dispose all engines and close inner providers.

        Closes both inner providers first (which disposes any engines they hold
        that were passed via ``premium_engine`` / ``standard_engine`` at
        construction time), then disposes the shared engine created by this
        provider.  ``AsyncEngine.dispose()`` is idempotent, so disposing an
        engine that was already disposed by an inner provider is safe and
        produces no error.
        """
        # Close inner providers first so any custom engines they hold are
        # released before we dispose the shared engine.
        await self._premium_provider.close() # type: ignore[attr-defined]
        await self._standard_provider.close() # type: ignore[attr-defined]
        # Dispose the shared engine last.  If both inner providers share this
        # engine, dispose() on it is idempotent — safe to call multiple times.
        await self._shared_engine.dispose()
        logger.info("HybridIsolationProvider closed (all engines disposed)")


__all__ = ["HybridIsolationProvider"]
