"""Central tenancy manager — lifecycle, middleware, and component orchestration.

The :class:`TenancyManager` is the single entry-point for integrating
fastapi-tenancy into a FastAPI application.  It:

* Validates configuration at construction time (fast-fail, no I/O).
* Lazily creates storage, resolver, and isolation components on first
  ``initialize()`` call.
* Provides :meth:`create_lifespan` — the recommended one-line integration
  that registers middleware and handles the full lifecycle.

Design decisions
----------------
* **No ``app`` argument in ``__init__``** — the manager is app-agnostic so
  it can be constructed before the ``FastAPI`` instance and shared across
  multiple apps in test scenarios.

* **Middleware registration timing** — :meth:`create_lifespan` calls
  ``app.add_middleware`` *inside* the lifespan callable, which runs during
  the ASGI startup phase — before any request is processed but while
  Starlette still allows middleware registration.  Calling ``add_middleware``
  after startup raises ``RuntimeError("Cannot add middleware after an
  application has started")``.

* **Idempotent ``initialize``** — safe to call multiple times; only the
  first call performs I/O.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi_tenancy.core.context import TenantContext
from fastapi_tenancy.core.types import Tenant, TenantStatus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI
    from starlette.types import Lifespan

    from fastapi_tenancy.core.config import TenancyConfig
    from fastapi_tenancy.isolation.base import BaseIsolationProvider
    from fastapi_tenancy.resolution.base import BaseTenantResolver
    from fastapi_tenancy.storage.tenant_store import TenantStore

logger = logging.getLogger(__name__)


class TenancyManager:
    """Orchestrator for all multi-tenancy components.

    The manager is **app-agnostic**: it holds configuration and references to
    storage, resolver, and isolation components without coupling to a specific
    FastAPI instance.  This makes it independently testable and reusable
    across multiple apps.

    Lifecycle
    ---------
    1. **Construct** — validates config; stores overrides.  No I/O.
    2. **initialize()** — creates database tables, initialises the resolver,
       builds isolation structures.  All heavy I/O is here.
    3. **shutdown()** — disposes engines, closes Redis connections.

    Recommended integration::

        from fastapi import FastAPI
        from fastapi_tenancy import TenancyManager, TenancyConfig

        config = TenancyConfig(
            database_url="postgresql+asyncpg://user:pass@localhost/myapp",
            resolution_strategy="header",
            isolation_strategy="schema",
        )

        app = FastAPI(lifespan=TenancyManager.create_lifespan(config))

    Advanced manual wiring::

        manager = TenancyManager(config)

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            from fastapi_tenancy.middleware.tenancy import TenancyMiddleware

            # Middleware MUST be registered before the lifespan yields.
            app.add_middleware(TenancyMiddleware, manager=manager)

            await manager.initialize()
            yield
            await manager.shutdown()

        app = FastAPI(lifespan=lifespan)

    Args:
        config: Validated :class:`~fastapi_tenancy.core.config.TenancyConfig`.
        tenant_store: Override the default
            :class:`~fastapi_tenancy.storage.database.SQLAlchemyTenantStore`.
            Useful for testing with
            :class:`~fastapi_tenancy.storage.memory.InMemoryTenantStore`.
        resolver: Override the resolver built from ``config.resolution_strategy``.
        isolation_provider: Override the isolation provider built from
            ``config.isolation_strategy``.
    """

    def __init__(
        self,
        config: TenancyConfig,
        *,
        tenant_store: TenantStore | None = None,
        resolver: BaseTenantResolver | None = None,
        isolation_provider: BaseIsolationProvider | None = None,
    ) -> None:
        self.config = config
        self._initialized = False
        self._custom_store = tenant_store
        self._custom_resolver = resolver
        self._custom_isolation = isolation_provider

        # Set during initialize()
        self.tenant_store: TenantStore
        self.resolver: BaseTenantResolver
        self.isolation_provider: BaseIsolationProvider

        logger.info(
            "TenancyManager created resolution=%s isolation=%s",
            config.resolution_strategy.value,
            config.isolation_strategy.value,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialise all components.

        Idempotent — subsequent calls after the first are no-ops.  Performs
        all I/O: creating database engines, storage tables, resolvers, and
        isolation structures.
        """
        if self._initialized:
            return

        logger.info("TenancyManager initialising …")

        self._init_storage()
        self._init_resolver()
        self._init_isolation()

        if hasattr(self.tenant_store, "initialize"):
            await self.tenant_store.initialize()

        await self._seed_default_tenants()

        self._initialized = True
        logger.info("TenancyManager initialised")

    async def shutdown(self) -> None:
        """Release all resources — connection pools, Redis connections, etc."""
        if not self._initialized:
            return

        logger.info("TenancyManager shutting down …")

        if hasattr(self.tenant_store, "close"):
            await self.tenant_store.close()

        if hasattr(self.isolation_provider, "close"):
            await self.isolation_provider.close()

        self._initialized = False
        logger.info("TenancyManager shutdown complete")

    async def __aenter__(self) -> TenancyManager:
        """Support ``async with TenancyManager(config) as m:`` in tests."""
        await self.initialize()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.shutdown()

    # ------------------------------------------------------------------
    # create_lifespan — recommended integration
    # ------------------------------------------------------------------

    @staticmethod
    def create_lifespan(
        config: TenancyConfig,
        *,
        tenant_store: TenantStore | None = None,
        resolver: BaseTenantResolver | None = None,
        isolation_provider: BaseIsolationProvider | None = None,
        skip_paths: list[str] | None = None,
        debug_headers: bool = False,
    ) -> Lifespan[FastAPI]:
        """Build a FastAPI ``lifespan`` context manager for the tenancy stack.

        This is the **recommended** integration pattern.  Pass the return
        value directly to ``FastAPI(lifespan=...)``.

        The lifespan callable:

        1. Creates the :class:`TenancyManager`.
        2. Registers :class:`~fastapi_tenancy.middleware.tenancy.TenancyMiddleware`
           on the real ``app`` **before** yielding (the only safe window).
        3. Calls ``manager.initialize()`` to perform all I/O setup.
        4. Yields — the application serves requests.
        5. Calls ``manager.shutdown()`` on teardown.

        Args:
            config: Validated tenancy configuration.
            tenant_store: Optional custom storage backend.
            resolver: Optional custom resolver.
            isolation_provider: Optional custom isolation provider.
            skip_paths: URL prefixes that bypass tenant resolution.
            debug_headers: When ``True``, add ``X-Tenant-*`` response headers.

        Returns:
            An ``asynccontextmanager``-wrapped lifespan callable for FastAPI.

        Example::

            from fastapi import FastAPI
            from fastapi_tenancy import TenancyManager, TenancyConfig

            config = TenancyConfig(
                database_url="postgresql+asyncpg://user:pass@localhost/myapp",
                resolution_strategy="subdomain",
                isolation_strategy="schema",
                domain_suffix=".example.com",
            )

            app = FastAPI(lifespan=TenancyManager.create_lifespan(config))
        """
        from fastapi_tenancy.middleware.tenancy import TenancyMiddleware

        @asynccontextmanager
        async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
            manager = TenancyManager(
                config,
                tenant_store=tenant_store,
                resolver=resolver,
                isolation_provider=isolation_provider,
            )

            # ──────────────────────────────────────────────────────────────
            # Middleware registration — MUST happen before yield.
            #
            # Starlette rebuilds the middleware stack on the first request.
            # Calling add_middleware after the stack is built raises:
            #   RuntimeError("Cannot add middleware after an application has started")
            #
            # The lifespan callable is invoked during ASGI startup — after
            # FastAPI is constructed but before any request is processed —
            # making this the only correct registration window.
            # ──────────────────────────────────────────────────────────────
            app.add_middleware(
                TenancyMiddleware,
                config=config,
                manager=manager,
                skip_paths=skip_paths,
                debug_headers=debug_headers,
            )

            # Expose the manager on app.state so dependencies can access it.
            app.state.tenancy_manager = manager
            app.state.tenancy_config = config

            # I/O initialisation (creates tables, engines, etc.)
            await manager.initialize()

            # Post-init state for dependency injection
            app.state.tenant_store = manager.tenant_store
            app.state.isolation_provider = manager.isolation_provider

            try:
                yield
            finally:
                await manager.shutdown()

        return _lifespan

    # ------------------------------------------------------------------
    # Private initialisation helpers
    # ------------------------------------------------------------------

    def _init_storage(self) -> None:
        if self._custom_store is not None:
            self.tenant_store = self._custom_store
            return
        from fastapi_tenancy.storage.database import SQLAlchemyTenantStore

        self.tenant_store = SQLAlchemyTenantStore(
            database_url=str(self.config.database_url),
            pool_size=self.config.database_pool_size,
            max_overflow=self.config.database_max_overflow,
        )

    def _init_resolver(self) -> None:
        if self._custom_resolver is not None:
            self.resolver = self._custom_resolver
            return
        from fastapi_tenancy.resolution.factory import ResolverFactory

        self.resolver = ResolverFactory.create(
            strategy=self.config.resolution_strategy,
            config=self.config,
            tenant_store=self.tenant_store,
        )

    def _init_isolation(self) -> None:
        if self._custom_isolation is not None:
            self.isolation_provider = self._custom_isolation
            return
        from fastapi_tenancy.isolation.factory import IsolationProviderFactory

        self.isolation_provider = IsolationProviderFactory.create(
            strategy=self.config.isolation_strategy,
            config=self.config,
        )

    async def _seed_default_tenants(self) -> None:
        """Create a demo tenant record when self-registration is enabled and no tenants exist.

        Only creates the store record — does **not** call
        :meth:`~fastapi_tenancy.isolation.base.BaseIsolationProvider.initialize_tenant`.
        The application is responsible for provisioning schemas / running migrations.
        """
        if not self.config.allow_tenant_registration:
            return
        try:
            if await self.tenant_store.count() > 0:
                return
        except Exception as exc:
            logger.warning("Could not count tenants during seed: %s", exc)
            return

        demo = Tenant(
            id="demo-tenant-001",
            identifier="demo",
            name="Demo Tenant",
            status=TenantStatus.ACTIVE,
            metadata={"demo": True},
        )
        try:
            await self.tenant_store.create(demo)
            logger.info("Created demo tenant record (run migrations to provision schema)")
        except Exception as exc:
            logger.warning("Could not create demo tenant: %s", exc)

    # ------------------------------------------------------------------
    # Utility API
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def tenant_scope(self, tenant_id: str) -> AsyncIterator[Tenant]:
        """Context manager that activates a tenant scope for background tasks.

        Args:
            tenant_id: Opaque ID of the tenant to activate.

        Yields:
            The resolved :class:`~fastapi_tenancy.core.types.Tenant`.

        Example::

            async with manager.tenant_scope("acme-corp-001") as tenant:
                await do_background_work(tenant)
        """
        tenant = await self.tenant_store.get_by_id(tenant_id)
        async with TenantContext.scope(tenant):
            yield tenant

    async def health_check(self) -> dict[str, Any]:
        """Return a health summary for all managed components.

        Returns:
            Dict with ``status`` and per-component ``components`` details.
        """
        health: dict[str, Any] = {"status": "healthy", "components": {}}
        try:
            count = await self.tenant_store.count()
            health["components"]["tenant_store"] = {
                "status": "healthy",
                "tenant_count": count,
            }
        except Exception as exc:
            health["status"] = "unhealthy"
            health["components"]["tenant_store"] = {
                "status": "unhealthy",
                "error": str(exc),
            }
        return health

    async def get_metrics(self) -> dict[str, Any]:
        """Return basic tenancy metrics fetched in parallel.

        Returns:
            Dict with ``total_tenants``, ``active_tenants``,
            ``suspended_tenants``, strategy names, and ``initialized`` flag.
        """
        total, active, suspended = await asyncio.gather(
            self.tenant_store.count(),
            self.tenant_store.count(status=TenantStatus.ACTIVE),
            self.tenant_store.count(status=TenantStatus.SUSPENDED),
        )
        return {
            "total_tenants": total,
            "active_tenants": active,
            "suspended_tenants": suspended,
            "resolution_strategy": self.config.resolution_strategy.value,
            "isolation_strategy": self.config.isolation_strategy.value,
            "initialized": self._initialized,
        }


__all__ = ["TenancyManager"]
