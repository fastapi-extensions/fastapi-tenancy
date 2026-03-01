"""``TenancyManager`` — the central orchestrator for fastapi-tenancy.

The manager wires together every component — resolver, store, isolation
provider, cache, rate limiter — and provides:

1. A FastAPI lifespan context manager for clean startup/shutdown.
2. Factory methods that create the correct resolver and provider from
   ``TenancyConfig`` without manual wiring.
3. High-level tenant management operations (create, suspend, delete, …).
4. Optional rate limiting backed by Redis.

Typical setup — full featured::

    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from fastapi_tenancy import TenancyManager, TenancyConfig
    from fastapi_tenancy.storage.database import SQLAlchemyTenantStore
    from fastapi_tenancy.middleware.tenancy import TenancyMiddleware
    from fastapi_tenancy.dependencies import make_tenant_db_dependency

    config = TenancyConfig(
        database_url="postgresql+asyncpg://user:pass@localhost/myapp",
        resolution_strategy="header",
        isolation_strategy="schema",
    )
    store = SQLAlchemyTenantStore(config.database_url)
    manager = TenancyManager(config, store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.initialize()
        ...
        await manager.close()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(TenancyMiddleware, manager=manager, excluded_paths=["/health"])

    get_tenant_db = make_tenant_db_dependency(manager)

Minimal setup — in-memory store::

    from fastapi_tenancy.storage.memory import InMemoryTenantStore
    store = InMemoryTenantStore()
    manager = TenancyManager(config, store)
    # No initialize() needed for InMemoryTenantStore.

Custom audit log writer::

    from fastapi_tenancy.manager import AuditLogWriter
    from fastapi_tenancy.core.types import AuditLog

    class DatabaseAuditWriter:
        async def write(self, entry: AuditLog) -> None:
            await db.execute(insert(AuditTable).values(**entry.model_dump()))

    manager = TenancyManager(config, store, audit_writer=DatabaseAuditWriter())
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi_tenancy.core.exceptions import (
    ConfigurationError,
    RateLimitExceededError,
    TenancyError,
    TenantNotFoundError,
)
from fastapi_tenancy.core.types import (
    IsolationStrategy,
    ResolutionStrategy,
    Tenant,
    TenantStatus,
)
from fastapi_tenancy.utils.security import generate_tenant_id

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from typing import Protocol

    from sqlalchemy import MetaData

    from fastapi_tenancy.core.config import TenancyConfig
    from fastapi_tenancy.core.types import AuditLog, TenantResolver
    from fastapi_tenancy.isolation.base import BaseIsolationProvider
    from fastapi_tenancy.storage.tenant_store import TenantStore

logger = logging.getLogger(__name__)


###########################
# AuditLogWriter protocol #
###########################


if TYPE_CHECKING:
    from typing import Protocol

    class AuditLogWriter(Protocol):
        """Structural protocol for audit log persistence backends.

        Implement this interface to persist audit log entries to any backend —
        a database table, CloudWatch, Datadog, a message queue, etc.::

            from fastapi_tenancy.manager import AuditLogWriter
            from fastapi_tenancy.core.types import AuditLog

            class DatabaseAuditWriter:
                async def write(self, entry: AuditLog) -> None:
                    await db.execute(insert(AuditTable).values(**entry.model_dump()))

            manager = TenancyManager(config, store, audit_writer=DatabaseAuditWriter())

        The default implementation (used when no writer is supplied) logs the
        entry at ``INFO`` level via Python's standard logging.
        """

        async def write(self, entry: AuditLog) -> None:
            """Persist *entry* to the audit log.

            Args:
                entry: The audit log entry to persist.
            """
            ...


class _DefaultAuditLogWriter:
    """Default audit log writer — logs entries at INFO level."""

    async def write(self, entry: AuditLog) -> None:
        logger.info(
            "AUDIT tenant=%s user=%s action=%s resource=%s resource_id=%s",
            entry.tenant_id,
            entry.user_id,
            entry.action,
            entry.resource,
            entry.resource_id,
        )


###########################
# Resolver factory
###########################


def _build_resolver(
    config: TenancyConfig,
    store: TenantStore[Tenant],
    custom_resolver: TenantResolver | None = None,
) -> TenantResolver:
    """Instantiate the correct resolver from *config*.

    Args:
        config: Tenancy configuration.
        store: Tenant store used by strategy-based resolvers.
        custom_resolver: User-supplied resolver (used when strategy is
            ``CUSTOM`` or as an override).

    Returns:
        A configured :class:`~fastapi_tenancy.core.types.TenantResolver`.

    Raises:
        ConfigurationError: When the strategy requires an unset field.
    """
    if config.resolution_strategy == ResolutionStrategy.CUSTOM:
        if custom_resolver is None:
            raise ConfigurationError(
                parameter="resolution_strategy",
                reason=(
                    "resolution_strategy='custom' requires a custom_resolver to be "
                    "passed to TenancyManager.__init__()."
                ),
            )
        return custom_resolver

    if custom_resolver is not None:
        logger.warning(
            "custom_resolver supplied but resolution_strategy=%r — "
            "the custom resolver will be ignored.",
            config.resolution_strategy.value,
        )

    if config.resolution_strategy == ResolutionStrategy.HEADER:
        from fastapi_tenancy.resolution.header import HeaderTenantResolver  # noqa: PLC0415

        return HeaderTenantResolver(store, header_name=config.tenant_header_name)

    if config.resolution_strategy == ResolutionStrategy.SUBDOMAIN:
        from fastapi_tenancy.resolution.subdomain import SubdomainTenantResolver  # noqa: PLC0415

        return SubdomainTenantResolver(store, domain_suffix=config.domain_suffix or "")

    if config.resolution_strategy == ResolutionStrategy.PATH:
        from fastapi_tenancy.resolution.path import PathTenantResolver  # noqa: PLC0415

        return PathTenantResolver(store, path_prefix=config.path_prefix)

    if config.resolution_strategy == ResolutionStrategy.JWT:
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver  # noqa: PLC0415

        if not config.jwt_secret:
            raise ConfigurationError(
                parameter="jwt_secret",
                reason="JWT resolution requires jwt_secret to be configured.",
            )
        return JWTTenantResolver(
            store,
            secret=config.jwt_secret,
            algorithm=config.jwt_algorithm,
            tenant_claim=config.jwt_tenant_claim,
        )

    raise ConfigurationError(
        parameter="resolution_strategy",
        reason=f"Unknown resolution strategy: {config.resolution_strategy!r}",
    )


##############################
# Isolation provider factory #
##############################


def _build_provider(config: TenancyConfig) -> BaseIsolationProvider:
    """Instantiate the correct isolation provider from *config*.

    Args:
        config: Tenancy configuration.

    Returns:
        A configured :class:`~fastapi_tenancy.isolation.base.BaseIsolationProvider`.

    Raises:
        ConfigurationError: On unsupported strategy.
    """
    if config.isolation_strategy == IsolationStrategy.SCHEMA:
        from fastapi_tenancy.isolation.schema import SchemaIsolationProvider  # noqa: PLC0415

        return SchemaIsolationProvider(config)

    if config.isolation_strategy == IsolationStrategy.DATABASE:
        from fastapi_tenancy.isolation.database import DatabaseIsolationProvider  # noqa: PLC0415

        return DatabaseIsolationProvider(config)

    if config.isolation_strategy == IsolationStrategy.RLS:
        from fastapi_tenancy.isolation.rls import RLSIsolationProvider  # noqa: PLC0415

        return RLSIsolationProvider(config)

    if config.isolation_strategy == IsolationStrategy.HYBRID:
        from fastapi_tenancy.isolation.hybrid import HybridIsolationProvider  # noqa: PLC0415

        return HybridIsolationProvider(config)

    raise ConfigurationError(
        parameter="isolation_strategy",
        reason=f"Unknown isolation strategy: {config.isolation_strategy!r}",
    )


##################
# TenancyManager #
##################


class TenancyManager:
    """Central orchestrator wiring resolver, store, and isolation provider.

    Args:
        config: Application-wide tenancy configuration.
        store: Tenant metadata storage backend.
        custom_resolver: Optional custom resolver (required when
            ``config.resolution_strategy == "custom"``).
        isolation_provider: Optional pre-built isolation provider.  When
            ``None``, one is built from ``config.isolation_strategy``.

    Attributes:
        config: The ``TenancyConfig`` this manager was constructed with.
        store: The underlying ``TenantStore``.
        resolver: The active ``TenantResolver``.
        isolation_provider: The active ``BaseIsolationProvider``.
    """

    def __init__(
        self,
        config: TenancyConfig,
        store: TenantStore[Tenant],
        custom_resolver: TenantResolver | None = None,
        isolation_provider: BaseIsolationProvider | None = None,
        audit_writer: Any | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.resolver: TenantResolver = _build_resolver(config, store, custom_resolver)
        self.isolation_provider: BaseIsolationProvider = (
            isolation_provider if isolation_provider is not None else _build_provider(config)
        )
        # Accept any object with an async ``write(entry)`` method — satisfies
        # the AuditLogWriter protocol.  Falls back to the default logger writer.
        self._audit_writer: Any = audit_writer if audit_writer is not None else _DefaultAuditLogWriter()  # noqa: E501
        self._rate_limiter: Any = None  # Lazy-initialised from Redis.
        self._rate_limiting_enabled: bool = config.enable_rate_limiting
        logger.info(
            "TenancyManager created resolver=%s isolation=%s audit_writer=%s",
            type(self.resolver).__name__,
            type(self.isolation_provider).__name__,
            type(self._audit_writer).__name__,
        )

    #############
    # Lifecycle #
    #############

    async def initialize(self) -> None:
        """Initialise all components.

        Call this once at application startup — typically inside a FastAPI
        lifespan context manager.  It:

        1. Initialises the store (creates tables if applicable).
        2. Warms the cache when configured.

        Safe to call multiple times (all operations are idempotent).
        """
        if hasattr(self.store, "initialize"):
            await self.store.initialize()
            logger.info("Store initialised: %s", type(self.store).__name__)

        if self.config.cache_enabled and hasattr(self.store, "warm_cache"):
            await self.store.warm_cache()
            logger.info("Cache warmed")

        if self.config.enable_rate_limiting and self.config.redis_url:
            await self._init_rate_limiter()

        logger.info("TenancyManager initialised")

    async def close(self) -> None:
        """Dispose all resources.

        Call this inside a FastAPI lifespan ``finally`` block or on SIGTERM.
        Disposes engine pools and closes Redis connections.

        Both ``isolation_provider.close()`` and ``store.close()`` are called
        unconditionally — ``TenantStore`` now declares a concrete no-op
        ``close()`` that subclasses override when they hold external resources,
        so the old ``hasattr`` guard is no longer necessary.
        """
        await self.isolation_provider.close()
        logger.info("Isolation provider closed")

        await self.store.close()
        logger.info("Store closed")

        logger.info("TenancyManager shut down cleanly")

    ###########################
    # FastAPI lifespan helper #
    ###########################

    def create_lifespan(self) -> Any:
        """Return an async context manager suitable for FastAPI's ``lifespan`` parameter.

        Example::

            app = FastAPI(lifespan=manager.create_lifespan())

        Returns:
            An async context manager that calls ``initialize()`` on enter
            and ``close()`` on exit.
        """
        from contextlib import asynccontextmanager  # noqa: PLC0415

        @asynccontextmanager
        async def _lifespan(app: Any) -> AsyncIterator[None]:
            await self.initialize()
            try:
                yield
            finally:
                await self.close()

        return _lifespan

    ################################
    # High-level tenant management #
    ################################

    async def register_tenant(
        self,
        identifier: str,
        name: str,
        metadata: dict[str, Any] | None = None,
        isolation_strategy: IsolationStrategy | None = None,
        app_metadata: MetaData | None = None,
    ) -> Tenant:
        """Register a new tenant and provision its database namespace.

        This is the recommended way to onboard tenants programmatically.
        It:

        1. Validates the identifier.
        2. Generates a cryptographically secure ``id``.
        3. Persists the tenant in the store.
        4. Calls ``isolation_provider.initialize_tenant()`` to create the
           schema/database.

        .. note::
            This does **not** auto-seed demo tenants.  Call this explicitly
            from your onboarding flow or admin CLI.

        Args:
            identifier: Human-readable slug (must pass
                :func:`~fastapi_tenancy.utils.validation.validate_tenant_identifier`).
            name: Display name.
            metadata: Optional initial metadata dict.
            isolation_strategy: Per-tenant isolation override.
            app_metadata: SQLAlchemy ``MetaData`` to create tables in the
                new tenant namespace.

        Returns:
            The newly created, stored :class:`~fastapi_tenancy.core.types.Tenant`.

        Raises:
            ValueError: When *identifier* is invalid or already taken.
            TenancyError: When store or isolation provider raises.
        """
        from fastapi_tenancy.utils.validation import validate_tenant_identifier  # noqa: PLC0415

        if not validate_tenant_identifier(identifier):
            msg = (
                f"Invalid tenant identifier {identifier!r}. "
                "Must be 3-63 lowercase alphanumeric characters and hyphens."
            )
            raise ValueError(msg)

        tenant_id = generate_tenant_id()
        tenant = Tenant(
            id=tenant_id,
            identifier=identifier,
            name=name,
            status=TenantStatus(self.config.default_tenant_status),
            isolation_strategy=isolation_strategy,
            metadata=metadata or {},
        )

        created = await self.store.create(tenant)
        logger.info("Registered tenant id=%s identifier=%s", created.id, created.identifier)

        try:
            await self.isolation_provider.initialize_tenant(created, metadata=app_metadata)
        except Exception as exc:
            # Rollback: remove from store so the identifier is not poisoned.
            try:  # noqa: SIM105
                await self.store.delete(created.id)
            except Exception:  # pragma: no cover  # noqa: S110
                pass
            raise TenancyError(
                f"Failed to initialise tenant {created.id!r}: {exc}",
                details={"identifier": identifier},
            ) from exc

        return created

    async def suspend_tenant(self, tenant_id: str) -> Tenant:
        """Suspend a tenant, blocking all future requests.

        Args:
            tenant_id: ID of the tenant to suspend.

        Returns:
            The updated tenant with ``status=SUSPENDED``.

        Raises:
            TenantNotFoundError: When *tenant_id* does not exist.
        """
        tenant = await self.store.set_status(tenant_id, TenantStatus.SUSPENDED)
        logger.warning("Suspended tenant %s", tenant_id)
        return tenant

    async def activate_tenant(self, tenant_id: str) -> Tenant:
        """Reinstate a suspended tenant.

        Args:
            tenant_id: ID of the tenant to activate.

        Returns:
            The updated tenant with ``status=ACTIVE``.

        Raises:
            TenantNotFoundError: When *tenant_id* does not exist.
        """
        tenant = await self.store.set_status(tenant_id, TenantStatus.ACTIVE)
        logger.info("Activated tenant %s", tenant_id)
        return tenant

    async def delete_tenant(
        self,
        tenant_id: str,
        destroy_data: bool = False,
        app_metadata: MetaData | None = None,
    ) -> None:
        """Delete a tenant, optionally destroying all associated data.

        When ``destroy_data=True`` **and** ``config.enable_soft_delete=False``,
        the tenant's isolation namespace (schema/database/rows) is permanently
        removed.  This is irreversible.

        Args:
            tenant_id: ID of the tenant to delete.
            destroy_data: When ``True``, call
                ``isolation_provider.destroy_tenant()`` to purge all data.
                Default: ``False`` (soft-delete only).
            app_metadata: SQLAlchemy ``MetaData`` passed to the isolation
                provider when ``destroy_data=True`` (required for RLS mode).

        Raises:
            TenantNotFoundError: When *tenant_id* does not exist.
            TenancyError: When data destruction fails.
        """
        try:
            tenant = await self.store.get_by_id(tenant_id)
        except TenantNotFoundError:  # noqa: TRY203
            raise

        if destroy_data and not self.config.enable_soft_delete:
            await self.isolation_provider.destroy_tenant(
                tenant,
                metadata=app_metadata,
            )
            logger.warning("Destroyed data for tenant %s", tenant_id)

        if self.config.enable_soft_delete:
            await self.store.set_status(tenant_id, TenantStatus.DELETED)
            logger.warning("Soft-deleted tenant %s", tenant_id)
        else:
            await self.store.delete(tenant_id)
            logger.warning("Hard-deleted tenant %s", tenant_id)

    #############
    # Rate limiting
    #############

    async def _init_rate_limiter(self) -> None:
        """Lazily initialise the Redis-backed sliding-window rate limiter."""
        try:
            import redis.asyncio as aioredis  # noqa: PLC0415

            self._rate_limiter = await aioredis.from_url(
                self.config.redis_url,
                decode_responses=True,
            )
            logger.info("Rate limiter Redis connection established")
        except ImportError:
            logger.warning(
                "redis[hiredis] not installed — rate limiting disabled."
            )
            self._rate_limiting_enabled = False

    # Lua script for atomic sliding-window rate-limit check-and-increment.
    # Executes entirely server-side in Redis so there is no TOCTOU race between
    # "check count" and "add request" — the two-pipeline approach had an
    # off-by-one window where concurrent requests at the exact boundary could
    # all read count = limit-1, all pass, and then all add, breaching the limit.
    #
    # Script arguments:
    #   KEYS[1]  — sorted-set key for this tenant
    #   ARGV[1]  — current timestamp (float, seconds)
    #   ARGV[2]  — window start timestamp (float, seconds)
    #   ARGV[3]  — rate limit (integer)
    #   ARGV[4]  — window size in seconds (integer, for EXPIRE)
    #
    # Returns: the request count AFTER this request (1 = allowed, >limit = denied).
    _RATE_LIMIT_LUA = """local key        = KEYS[1]
local now        = tonumber(ARGV[1])
local win_start  = tonumber(ARGV[2])
local limit      = tonumber(ARGV[3])
local window     = tonumber(ARGV[4])

-- 1. Evict entries older than the window.
redis.call('ZREMRANGEBYSCORE', key, '-inf', win_start)

-- 2. Count requests still inside the window.
local count = redis.call('ZCARD', key)

-- 3. If already at (or over) the limit, deny without adding.
if count >= limit then
    return count
end

-- 4. Add this request and refresh the key TTL.
redis.call('ZADD', key, now, now)
redis.call('EXPIRE', key, window)

-- 5. Return the new count (will be ≤ limit).
return count + 1
"""

    async def check_rate_limit(self, tenant: Tenant) -> None:
        """Check and atomically increment the sliding-window rate limit for *tenant*.

        Uses a single Lua script executed server-side by Redis, replacing the
        previous two-pipeline approach that had an off-by-one race condition:
        concurrent requests at the exact boundary could all read ``count =
        limit - 1``, all pass the check, and then all add their timestamps,
        silently breaching the limit.

        The Lua script is atomic — Redis executes it without interleaving any
        other commands — so the check-and-increment is always consistent.

        Args:
            tenant: The tenant whose rate limit to check.

        Raises:
            RateLimitExceededError: When the tenant has exceeded its limit.
        """
        if not self._rate_limiting_enabled or self._rate_limiter is None:
            return

        import time  # noqa: PLC0415

        key = f"tenancy:ratelimit:{tenant.id}"
        window = self.config.rate_limit_window_seconds
        limit = self.config.rate_limit_per_minute
        now = time.time()
        window_start = now - window

        try:
            count: int = await self._rate_limiter.eval(
                self._RATE_LIMIT_LUA,
                1,  # number of KEYS
                key,
                now,
                window_start,
                limit,
                window,
            )
            if count > limit:
                raise RateLimitExceededError(  # noqa: TRY301
                    tenant_id=tenant.id,
                    limit=limit,
                    window_seconds=window,
                )
        except RateLimitExceededError:
            raise
        except Exception as exc:
            logger.warning("Rate limit check failed for tenant %s: %s", tenant.id, exc)

    #############
    # Audit log #
    #############

    async def write_audit_log(self, entry: AuditLog) -> None:
        """Persist an audit log entry via the configured ``AuditLogWriter``.

        Delegates to the ``AuditLogWriter`` supplied at construction time.
        The default writer logs the entry at ``INFO`` level.  Supply a custom
        writer to persist to a database, message queue, or external service::

            class CloudWatchAuditWriter:
                async def write(self, entry: AuditLog) -> None:
                    await cw.put_log_events(...)

            manager = TenancyManager(config, store, audit_writer=CloudWatchAuditWriter())

        Args:
            entry: The audit log entry to persist.
        """
        await self._audit_writer.write(entry)


__all__ = ["AuditLogWriter", "TenancyManager"]
