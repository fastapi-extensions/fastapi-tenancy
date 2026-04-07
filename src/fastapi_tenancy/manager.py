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

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

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

    from sqlalchemy import MetaData

    from fastapi_tenancy.core.config import TenancyConfig
    from fastapi_tenancy.core.types import AuditLog, TenantResolver
    from fastapi_tenancy.isolation.base import BaseIsolationProvider
    from fastapi_tenancy.storage.tenant_store import TenantStore

logger = logging.getLogger(__name__)


###########################
# CachingStoreProxy       #
###########################


class _CachingStoreProxy:
    """Transparent proxy that adds an in-process L1 LRU+TTL cache in front of any store.

    Only ``get_by_identifier`` is intercepted (the hot path called on every
    request by the resolver).  All other ``TenantStore`` methods are delegated
    directly to the underlying store so the proxy is transparent to callers.

    The L1 cache is populated on miss and invalidated on write operations
    (``create``, ``update``, ``set_status``, ``delete``) to prevent stale reads.

    Args:
        store: Underlying :class:`~fastapi_tenancy.storage.tenant_store.TenantStore`.
        l1_cache: Configured :class:`~fastapi_tenancy.cache.tenant_cache.TenantCache`.
    """

    def __init__(self, store: Any, l1_cache: Any) -> None:
        self._store = store
        self._l1 = l1_cache

    def __getattr__(self, name: str) -> Any:
        # Delegate all non-overridden attributes to the backing store.
        return getattr(self._store, name)

    async def get_by_identifier(self, identifier: str) -> Tenant:
        """Return tenant from L1 cache on hit, or store on miss (and populate cache).

        Args:
            identifier: Tenant slug.

        Returns:
            The resolved :class:`~fastapi_tenancy.core.types.Tenant`.

        Raises:
            TenantNotFoundError: When the identifier is not found in store.
        """
        cached = self._l1.get_by_identifier(identifier)
        if cached is not None:
            logger.debug("L1 cache hit for identifier=%r", identifier)
            return cached

        tenant = await self._store.get_by_identifier(identifier)
        await self._l1.aset(tenant)
        logger.debug("L1 cache miss — populated for identifier=%r", identifier)
        return tenant

    async def create(self, tenant: Tenant) -> Tenant:
        result = await self._store.create(tenant)
        self._l1.invalidate(result.id)
        return result

    async def update(self, tenant: Tenant) -> Tenant:
        # Invalidate the *old* identifier before writing so that a
        # rename does not leave a stale identifier id mapping in L1.
        old_cached = self._l1.get(tenant.id)
        if old_cached is not None and old_cached.identifier != tenant.identifier:
            self._l1.invalidate_by_identifier(old_cached.identifier)
        result = await self._store.update(tenant)
        self._l1.invalidate(result.id)
        return result

    async def set_status(self, tenant_id: str, status: Any) -> Tenant:
        result = await self._store.set_status(tenant_id, status)
        self._l1.invalidate(tenant_id)
        return result

    async def delete(self, tenant_id: str) -> None:
        await self._store.delete(tenant_id)
        self._l1.invalidate(tenant_id)


@runtime_checkable
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


####################
# Resolver factory #
####################


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

        # Build L1 cache first so we can wrap the store before the resolver
        # is constructed — the resolver will then call get_by_identifier on
        # the proxy, getting automatic L1 cache hits on every warm request.
        self._l1_cache: Any = None
        _effective_store: Any = store
        if config.cache_enabled:
            from fastapi_tenancy.cache.tenant_cache import TenantCache  # noqa: PLC0415

            self._l1_cache = TenantCache(
                max_size=config.l1_cache_max_size,
                ttl=config.l1_cache_ttl_seconds,
            )
            _effective_store = _CachingStoreProxy(store, self._l1_cache)
            logger.info(
                "L1 TenantCache wired max_size=%d ttl=%ds",
                self._l1_cache._max_size,
                self._l1_cache._ttl,
            )

        self.store = _effective_store
        self.resolver: TenantResolver = _build_resolver(config, _effective_store, custom_resolver)
        self.isolation_provider: BaseIsolationProvider = (
            isolation_provider if isolation_provider is not None else _build_provider(config)
        )
        # Accept any object with an async ``write(entry)`` method — satisfies
        # the AuditLogWriter protocol.  Falls back to the default logger writer.
        self._audit_writer: Any = (
            audit_writer if audit_writer is not None else _DefaultAuditLogWriter()
        )
        self._rate_limiter: Any = None  # Lazy-initialised from Redis.
        self._rate_limiting_enabled: bool = config.enable_rate_limiting

        # Background task that periodically evicts expired L1 cache entries.
        # Initialised to None here; started by initialize() when the cache is
        # enabled; cancelled by close() on shutdown.
        self._purge_task: asyncio.Task[None] | None = None

        # Field-level encryption — None when enable_encryption=False.
        from fastapi_tenancy.utils.encryption import TenancyEncryption  # noqa: PLC0415

        self._encryption: TenancyEncryption | None = TenancyEncryption.from_config(config)
        if self._encryption is not None:
            logger.info("Field-level encryption enabled (Fernet/AES-128-CBC+HMAC-SHA256)")

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
        2. Warms the Redis cache when configured.
        3. Starts the L1 cache background purge task (when cache is enabled).
        4. Establishes the Redis rate-limiter connection (when rate limiting is enabled).

        Safe to call multiple times — all operations are idempotent.  A second
        call while the purge task is already running will not start a duplicate.
        """
        if hasattr(self.store, "initialize"):
            await self.store.initialize()
            logger.info("Store initialised: %s", type(self.store).__name__)

        if self.config.cache_enabled and hasattr(self.store, "warm_cache"):
            await self.store.warm_cache()
            logger.info("Cache warmed")

        if self._l1_cache is not None:
            logger.info("L1 in-process tenant cache active")
            # Start the background purge task only when it is not already running.
            # The task runs every half-TTL interval so that on average no entry
            # survives longer than 1.5x its configured TTL in memory.  This is
            # a best-effort sweep — lazy eviction on access is still the primary
            # mechanism; the task merely reclaims memory in low-traffic processes.
            if self._purge_task is None or self._purge_task.done():
                self._purge_task = asyncio.create_task(
                    self._run_cache_purge_loop(),
                    name="fastapi-tenancy:l1-cache-purge",
                )
                logger.info(
                    "L1 cache purge task started (interval=%ds)",
                    max(1, self.config.l1_cache_ttl_seconds // 2),
                )

        if self.config.enable_rate_limiting and self.config.redis_url:
            await self._init_rate_limiter()

        logger.info("TenancyManager initialised")

    async def close(self) -> None:
        """Dispose all resources.

        Call this inside a FastAPI lifespan ``finally`` block or on SIGTERM.
        Disposes engine pools, closes Redis connections, and cancels the
        background L1 cache purge task.

        Both ``isolation_provider.close()`` and ``store.close()`` are called
        unconditionally — ``TenantStore`` now declares a concrete no-op
        ``close()`` that subclasses override when they hold external resources,
        so the old ``hasattr`` guard is no longer necessary.
        """
        # Cancel the background purge task first so it cannot reference the
        # cache after the store is closed.
        if self._purge_task is not None and not self._purge_task.done():
            self._purge_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._purge_task
            logger.info("L1 cache purge task cancelled")
        self._purge_task = None

        if hasattr(self.isolation_provider, "close"):
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

        # Encrypt sensitive fields before writing to the store.
        if self._encryption is not None:
            tenant = self._encryption.encrypt_tenant_fields(tenant)

        created = await self.store.create(tenant)
        logger.info("Registered tenant id=%s identifier=%s", created.id, created.identifier)

        try:
            await self.isolation_provider.initialize_tenant(created, metadata=app_metadata)
        except Exception as exc:
            # Rollback: remove from store so the identifier is not poisoned.
            try:
                await self.store.delete(created.id)
            except Exception as rollback_exc:  # pragma: no cover
                logger.error(  # noqa: TRY400
                    "Rollback failed for tenant %s after initialize_tenant error — "
                    "identifier %r may be poisoned in the store: %s",
                    created.id,
                    identifier,
                    rollback_exc,
                )
            raise TenancyError(
                f"Failed to initialise tenant {created.id!r}: {exc}",
                details={"identifier": identifier},
            ) from exc

        # Return a decrypted copy so callers receive plaintext values.
        if self._encryption is not None:
            return self._encryption.decrypt_tenant_fields(created)
        return created

    def decrypt_tenant(self, tenant: Tenant) -> Tenant:
        """Return *tenant* with sensitive fields decrypted.

        Call this after loading a tenant from the store whenever the result
        will be passed to application code (e.g. in route handlers, background
        tasks, or audit logs that should not see ciphertext).

        When encryption is disabled (enable_encryption=False) this method
        is a no-op and returns *tenant* unchanged.

        Args:
            tenant: A :class: instance
                that may have encrypted field values.

        Returns:
            A Tenant instance with plaintext field values.
        """
        if self._encryption is None:
            return tenant
        return self._encryption.decrypt_tenant_fields(tenant)

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

    ##########################
    # L1 cache purge loop    #
    ##########################

    async def _run_cache_purge_loop(self) -> None:
        """Periodically evict expired entries from the L1 TenantCache.

        Runs as a long-lived background ``asyncio.Task`` created by
        :meth:`initialize` and cancelled by :meth:`close`.

        Design decisions
        ----------------
        **Interval** — ``max(1, l1_cache_ttl_seconds // 2)`` seconds.
        This ensures that on average, an expired entry survives at most
        ``1.5 * TTL`` in memory rather than up to ``2 * TTL`` if the purge
        ran at the same interval as the TTL.  The ``max(1, ...)`` guard
        prevents a zero-second tight loop for very short TTLs in tests.

        **Lazy eviction is still primary** — every cache *read* already
        evicts stale entries on access.  This loop is a secondary sweep
        that reclaims memory in low-traffic applications where hot tenants
        are accessed infrequently enough that stale entries accumulate.

        **CancelledError** — ``asyncio.sleep`` is a cancellation point.
        When ``close()`` cancels this task the ``CancelledError`` propagates
        cleanly out of the ``while True`` loop; no try/except is needed here
        because the caller (``close``) already awaits with a try/except.

        Raises:
            asyncio.CancelledError: When the task is cancelled by ``close()``.
        """
        if self._l1_cache is None:
            return

        interval = max(1, self.config.l1_cache_ttl_seconds // 2)
        logger.debug("L1 purge loop running every %ds", interval)

        while True:
            await asyncio.sleep(interval)
            evicted = self._l1_cache.purge_expired()
            if evicted:
                logger.debug("L1 cache purge: evicted %d expired entries", evicted)

    #################
    # Rate limiting #
    #################

    async def _init_rate_limiter(self) -> None:
        """Lazily initialise the Redis-backed sliding-window rate limiter."""
        try:
            import redis.asyncio as aioredis  # noqa: PLC0415

            self._rate_limiter = await aioredis.from_url(
                self.config.redis_url,  # type: ignore
                decode_responses=True,
            )
            logger.info("Rate limiter Redis connection established")
        except ImportError:
            logger.warning("redis[hiredis] not installed — rate limiting disabled.")
            self._rate_limiting_enabled = False

    # Lua script for atomic sliding-window rate-limit check-and-increment.
    # Executes entirely server-side in Redis so there is no TOCTOU race between
    # "check count" and "add request" — the two-pipeline approach had an
    # off-by-one window where concurrent requests at the exact boundary could
    # all read count = limit-1, all pass, and then all add, breaching the limit.
    #
    # Fix — unique member per request:
    # The original script used `now` (a float) as *both* the sorted-set score
    # and the member string.  Two requests arriving within the same microsecond
    # produce the same float value; the second ZADD call silently overwrites
    # the first member rather than adding a new one, under-counting the window
    # and allowing an extra request to slip through.  Using a UUID-suffixed
    # member guarantees uniqueness regardless of timestamp resolution.
    #
    # Script arguments:
    #   KEYS[1]  — sorted-set key for this tenant
    #   ARGV[1]  — current timestamp (float, seconds) — used as ZADD score
    #   ARGV[2]  — window start timestamp (float, seconds) — eviction boundary
    #   ARGV[3]  — rate limit (integer)
    #   ARGV[4]  — window size in seconds (integer, for EXPIRE)
    #   ARGV[5]  — unique request identifier (timestamp:uuid4 string) — member
    #
    # Returns: the request count AFTER this request (1 = first allowed, >limit = denied).
    _RATE_LIMIT_LUA = """local key        = KEYS[1]
local now        = tonumber(ARGV[1])
local win_start  = tonumber(ARGV[2])
local limit      = tonumber(ARGV[3])
local window     = tonumber(ARGV[4])
local member     = ARGV[5]

-- 1. Evict entries older than the window.
redis.call('ZREMRANGEBYSCORE', key, '-inf', win_start)

-- 2. Count requests still inside the window.
local count = redis.call('ZCARD', key)

-- 3. If already at (or over) the limit, deny without adding.
if count >= limit then
    return count
end

-- 4. Add this request with a unique member and refresh the key TTL.
--    score=now for time-based eviction; member=unique so concurrent requests
--    at the same timestamp each produce a distinct sorted-set entry.
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, window)

-- 5. Return the new count (will be <= limit).
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

        Each call generates a unique member string (``"{now}:{uuid4}"``) so
        that two requests arriving within the same microsecond each add a
        distinct sorted-set entry rather than overwriting each other.

        Args:
            tenant: The tenant whose rate limit to check.

        Raises:
            RateLimitExceededError: When the tenant has exceeded its limit.
        """
        if not self._rate_limiting_enabled or self._rate_limiter is None:
            return

        import time  # noqa: PLC0415
        import uuid  # noqa: PLC0415

        key = f"tenancy:ratelimit:{tenant.id}"
        window = self.config.rate_limit_window_seconds
        limit = self.config.rate_limit_per_minute
        now = time.time()
        window_start = now - window
        # Unique member: timestamp prefix for human readability, uuid4 suffix
        # to guarantee per-request uniqueness within the same microsecond.
        member = f"{now}:{uuid.uuid4().hex}"

        try:
            count: int = await self._rate_limiter.eval(
                self._RATE_LIMIT_LUA,
                1,  # number of KEYS
                key,
                now,
                window_start,
                limit,
                window,
                member,
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
            # Redis unavailability previously caused silent fail-open.
            # Log at Exception level so it surfaces in alerting dashboards, and
            # honour the fail_closed flag when operators want strict enforcement.
            logger.exception(
                f"Rate limit Redis failure for tenant {tenant.id!r} — {exc.__repr__()}. "
                "Operating in fail-{} mode.".format(
                    "closed" if getattr(self.config, "rate_limit_fail_closed", False) else "open"
                )
            )
            if getattr(self.config, "rate_limit_fail_closed", False):
                raise RateLimitExceededError(
                    tenant_id=tenant.id,
                    limit=self.config.rate_limit_per_minute,
                    window_seconds=self.config.rate_limit_window_seconds,
                ) from exc

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

    ###########
    # Metrics #
    ###########

    def get_metrics(self) -> dict[str, Any]:
        """Return a snapshot of runtime metrics for this manager instance.

        Metrics are always collected internally.  When ``enable_metrics=False``
        this method still returns the snapshot — the flag is intended to gate
        *external* exposure (e.g. a ``/metrics`` endpoint) rather than
        collection itself.

        Returns:
            A dict with the following keys:

            * ``l1_cache`` — L1 TenantCache statistics (hit/miss counts,
              hit rate, current size) or ``None`` when the cache is disabled.
            * ``engine_cache_size`` — number of per-tenant engines currently
              cached (DATABASE isolation only), or ``None`` for other providers.
            * ``metrics_enabled`` — mirrors ``config.enable_metrics``.

        Example::

            @app.get("/metrics")
            async def metrics():
                if not manager.config.enable_metrics:
                    raise HTTPException(status_code=404)
                return manager.get_metrics()
        """
        from fastapi_tenancy.isolation.database import (  # noqa: PLC0415
            DatabaseIsolationProvider,
        )

        l1_stats: dict[str, Any] | None = None
        if self._l1_cache is not None:
            l1_stats = self._l1_cache.stats()

        engine_cache_size: int | None = None
        if isinstance(self.isolation_provider, DatabaseIsolationProvider):
            engine_cache_size = self.isolation_provider._engine_cache.size

        return {
            "metrics_enabled": self.config.enable_metrics,
            "l1_cache": l1_stats,
            "engine_cache_size": engine_cache_size,
        }


__all__ = ["AuditLogWriter", "TenancyManager"]
