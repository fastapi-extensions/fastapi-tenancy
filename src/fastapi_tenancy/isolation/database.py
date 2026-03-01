"""Database-per-tenant isolation — multi-database compatible.

Each tenant owns a separate database (or ``.db`` file for SQLite).  This
provides the strongest data isolation at the cost of the highest resource
overhead (one connection pool per tenant).

Critical fix: LRU engine cache
---------------------------------
The original implementation used an unbounded ``dict[str, AsyncEngine]``
engine cache.  With 1 000 tenants each engine holds a pool of 20+ idle
connections → thousands of open file descriptors and unbounded memory growth.

:class:`_LRUEngineCache` implements a bounded LRU cache with configurable
``max_size``.  When the cache is full, the least-recently-used engine is
evicted and ``await engine.dispose()`` is called immediately to release its
pooled connections.

Dialect support
---------------
+------------------+---------------------------------------------+
| Dialect          | Mechanism                                   |
+==================+=============================================+
| PostgreSQL       | ``CREATE DATABASE`` via master connection   |
+------------------+---------------------------------------------+
| MySQL / MariaDB  | ``CREATE DATABASE`` (SCHEMA == DATABASE)    |
+------------------+---------------------------------------------+
| SQLite           | Per-tenant ``.db`` file path               |
+------------------+---------------------------------------------+
| MSSQL            | Raises :exc:`IsolationError` (manual setup)|
+------------------+---------------------------------------------+

Concurrency safety
------------------
A single :class:`asyncio.Lock` guards engine creation so that two concurrent
first-requests for the same new tenant cannot race and leak a second engine.

Security
--------
Every database name is validated via :func:`assert_safe_database_name`
before being interpolated into any DDL statement.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from fastapi_tenancy.core.exceptions import IsolationError
from fastapi_tenancy.isolation.base import BaseIsolationProvider
from fastapi_tenancy.utils.db_compat import (
    DbDialect,
    detect_dialect,
    requires_static_pool,
)
from fastapi_tenancy.utils.validation import assert_safe_database_name, sanitize_identifier

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy import MetaData

    from fastapi_tenancy.core.config import TenancyConfig
    from fastapi_tenancy.core.types import SelectT, Tenant

logger = logging.getLogger(__name__)


class _LRUEngineCache:
    """Thread-safe LRU cache for per-tenant ``AsyncEngine`` objects.

    When the cache reaches ``max_size``, the least-recently-used engine is
    evicted and ``await engine.dispose()`` is called to release its connection
    pool immediately.

    Args:
        max_size: Maximum number of engines to keep cached.
    """

    def __init__(self, max_size: int = 100) -> None:
        self._cache: OrderedDict[str, AsyncEngine] = OrderedDict()
        self._max_size = max_size
        self._lock: asyncio.Lock = asyncio.Lock()

    async def get(self, key: str) -> AsyncEngine | None:
        """Return the cached engine for *key* and promote it to MRU position.

        Args:
            key: Engine cache key (typically the tenant ID).

        Returns:
            Cached engine or ``None`` on miss.
        """
        async with self._lock:
            engine = self._cache.get(key)
            if engine is not None:
                self._cache.move_to_end(key)
            return engine

    async def put(self, key: str, engine: AsyncEngine) -> AsyncEngine | None:
        """Insert *engine* under *key*, evicting LRU entries as needed.

        If adding *key* would exceed ``max_size``, the oldest entry is removed
        and returned so the caller can dispose it outside the lock.

        Args:
            key: Engine cache key.
            engine: Engine to cache.

        Returns:
            The evicted engine (if any) that the caller must dispose, or
            ``None`` when no eviction occurred.
        """
        async with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return None
            evicted_engine: AsyncEngine | None = None
            if len(self._cache) >= self._max_size:
                evicted_key, evicted_engine = self._cache.popitem(last=False)
                logger.debug("LRU evicted engine key=%s", evicted_key)
            self._cache[key] = engine
            return evicted_engine  # caller disposes outside lock

    async def remove(self, key: str) -> AsyncEngine | None:
        """Remove and return the engine for *key*, or ``None`` on miss.

        The caller is responsible for calling ``await engine.dispose()``.

        Args:
            key: Engine cache key.

        Returns:
            Removed engine or ``None``.
        """
        async with self._lock:
            return self._cache.pop(key, None)

    async def dispose_all(self) -> int:
        """Dispose all cached engines and clear the cache.

        Returns:
            Number of engines disposed.
        """
        async with self._lock:
            engines = list(self._cache.values())
            self._cache.clear()

        disposed = 0
        for engine in engines:
            try:
                await engine.dispose()
                disposed += 1
            except Exception as exc:
                logger.warning("Error disposing engine: %s", exc)
        return disposed

    @property
    def size(self) -> int:
        """Return the number of cached engines."""
        return len(self._cache)


class DatabaseIsolationProvider(BaseIsolationProvider):
    """Separate database per tenant with automatic dialect-based provisioning.

    A single master engine connects to the admin/default database for DDL.
    Per-tenant engines are created lazily on the first request and cached
    in a bounded LRU cache for the lifetime of the application.

    Args:
        config: Tenancy configuration.
        master_engine: Optional pre-built master engine (shared engine
            injected by :class:`~fastapi_tenancy.isolation.hybrid.HybridIsolationProvider`
            to avoid a duplicate connection pool).

    Example::

        provider = DatabaseIsolationProvider(config)
        await provider.initialize_tenant(tenant, metadata=Base.metadata)

        async with provider.get_session(tenant) as session:
            result = await session.execute(select(Order))

        await provider.close()  # disposes all per-tenant engines
    """

    def __init__(
        self,
        config: TenancyConfig,
        master_engine: AsyncEngine | None = None,
    ) -> None:
        super().__init__(config)
        self.dialect = detect_dialect(str(config.database_url))
        self._engine_cache = _LRUEngineCache(max_size=config.max_cached_engines)
        # Per-tenant asyncio.Lock objects prevent two concurrent coroutines from
        # racing through _get_engine() and creating duplicate engines for the same
        # tenant.  Without this guard a second engine is created, never added to
        # the cache, and its connection pool leaks until the process exits.
        self._creation_locks: dict[str, asyncio.Lock] = {}
        self._creation_locks_lock: asyncio.Lock = asyncio.Lock()

        if master_engine is not None:
            self._master = master_engine
        else:
            kw: dict[str, Any] = {
                "echo": config.database_echo,
                "isolation_level": "AUTOCOMMIT",
            }
            if requires_static_pool(self.dialect):
                kw["poolclass"] = StaticPool
                kw["connect_args"] = {"check_same_thread": False}
                del kw["isolation_level"]
            else:
                kw["pool_size"] = max(config.database_pool_size, 5)
                kw["max_overflow"] = config.database_max_overflow
                kw["pool_pre_ping"] = config.database_pool_pre_ping
            self._master = create_async_engine(str(config.database_url), **kw)

        logger.info(
            "DatabaseIsolationProvider dialect=%s max_cached_engines=%d",
            self.dialect.value,
            config.max_cached_engines,
        )

    ####################
    # Internal helpers #
    ####################

    def _database_name(self, tenant: Tenant) -> str:
        """Compute the database name for *tenant*.

        Args:
            tenant: Target tenant.

        Returns:
            A safe, deterministic database name string.
        """
        slug = sanitize_identifier(tenant.identifier)
        return f"tenant_{slug}_db"

    def _tenant_url(self, tenant: Tenant) -> str:
        """Build the connection URL for *tenant*'s dedicated database.

        Args:
            tenant: Target tenant.

        Returns:
            Fully-qualified async database URL.
        """
        import re  # noqa: PLC0415

        base = str(self.config.database_url)
        slug = sanitize_identifier(tenant.identifier)
        db_name = self._database_name(tenant)

        if self.dialect == DbDialect.SQLITE:
            parts = base.rsplit("/", 1)
            return f"{parts[0]}/{slug}.db" if len(parts) == 2 else base

        if self.config.database_url_template:
            return self.config.database_url_template.format(
                tenant_id=tenant.id,
                database_name=db_name,
            )

        # Replace the database name at the end of the URL safely.
        return re.sub(
            r"(/[^/?]*)(\?.*)?$",
            lambda m: f"/{db_name}{m.group(2) or ''}",
            base,
        )

    async def _get_engine(self, tenant: Tenant) -> AsyncEngine:
        """Return (or lazily create) the per-tenant engine from the LRU cache.

        A fast path checks the cache first without acquiring any lock.  On a
        cache miss, a per-tenant ``asyncio.Lock`` is acquired so that only one
        coroutine creates and caches the engine; all other coroutines racing on
        the same tenant will block briefly and then use the cached engine.

        Without the per-tenant lock, two concurrent first-requests for the same
        tenant would both call ``create_async_engine``, one would be cached and
        the other would be discarded — leaking its connection pool indefinitely.

        Args:
            tenant: Target tenant.

        Returns:
            A ready ``AsyncEngine`` for *tenant*'s database.
        """
        # Fast path: cache hit (no lock needed).
        cached = await self._engine_cache.get(tenant.id)
        if cached is not None:
            return cached

        # Slow path: acquire a per-tenant creation lock.
        async with self._creation_locks_lock:
            if tenant.id not in self._creation_locks:
                self._creation_locks[tenant.id] = asyncio.Lock()
            tenant_lock = self._creation_locks[tenant.id]

        async with tenant_lock:
            # Double-check after acquiring the per-tenant lock — another
            # coroutine may have created and cached the engine while we waited.
            cached = await self._engine_cache.get(tenant.id)
            if cached is not None:
                return cached

            url = self._tenant_url(tenant)
            kw: dict[str, Any] = {"echo": self.config.database_echo}
            if requires_static_pool(self.dialect):
                kw["poolclass"] = StaticPool
                kw["connect_args"] = {"check_same_thread": False}
            else:
                kw["pool_size"] = self.config.database_pool_size
                kw["max_overflow"] = self.config.database_max_overflow
                kw["pool_pre_ping"] = self.config.database_pool_pre_ping
                kw["pool_recycle"] = self.config.database_pool_recycle

            engine = create_async_engine(url, **kw)
            evicted = await self._engine_cache.put(tenant.id, engine)
            if evicted is not None:
                try:
                    await evicted.dispose()
                except Exception as exc:
                    logger.warning("Error disposing evicted engine: %s", exc)
            logger.debug(
                "Created engine tenant=%s cached_count=%d", tenant.id, self._engine_cache.size
            )

        # Clean up the per-tenant creation lock after the engine is cached so
        # that the _creation_locks dict does not grow without bound in
        # high-churn environments (many tenants provisioned and deprovisioned).
        # It is safe to do this outside the tenant_lock — the engine is already
        # in the cache, so any subsequent caller will take the fast path and
        # never reach this slow path again for this tenant.
        async with self._creation_locks_lock:
            self._creation_locks.pop(tenant.id, None)

        return engine

    ###########
    # Session #
    ###########

    @asynccontextmanager
    async def get_session(self, tenant: Tenant) -> AsyncIterator[AsyncSession]:
        """Yield a session connected to *tenant*'s dedicated database.

        Args:
            tenant: Currently active tenant.

        Yields:
            An :class:`~sqlalchemy.ext.asyncio.AsyncSession`.

        Raises:
            IsolationError: When the session cannot be opened.
        """
        engine = await self._get_engine(tenant)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            try:
                yield session
            except Exception as exc:
                await session.rollback()
                raise IsolationError(
                    operation="get_session",
                    tenant_id=tenant.id,
                    details={"error": str(exc)},
                ) from exc

    async def apply_filters(self, query: SelectT, tenant: Tenant) -> SelectT:
        """No filtering required — each tenant has a dedicated database.

        Args:
            query: SQLAlchemy ``Select`` query (returned unchanged).
            tenant: Currently active tenant (ignored).

        Returns:
            *query* unchanged.
        """
        return query

    ################
    # Provisioning #
    ################

    async def initialize_tenant(
        self,
        tenant: Tenant,
        metadata: MetaData | None = None,
    ) -> None:
        """Create *tenant*'s dedicated database and optionally create tables.

        Args:
            tenant: Target tenant.
            metadata: Application :class:`~sqlalchemy.MetaData`.  When supplied,
                ``create_all`` is executed in the newly created database.

        Raises:
            IsolationError: When database creation or table creation fails.
        """
        if self.dialect == DbDialect.SQLITE:
            engine = await self._get_engine(tenant)
            if metadata is not None:
                async with engine.begin() as conn:
                    await conn.run_sync(metadata.create_all)
            logger.info("SQLite tenant %s initialised", tenant.id)
            return

        if self.dialect == DbDialect.MSSQL:
            raise IsolationError(
                operation="initialize_tenant",
                tenant_id=tenant.id,
                details={
                    "reason": (
                        "DATABASE isolation on MSSQL requires manual database creation. "
                        "Use SCHEMA isolation or create the database manually."
                    )
                },
            )

        db_name = self._database_name(tenant)
        try:
            assert_safe_database_name(db_name, context=f"tenant id={tenant.id!r}")
        except ValueError as exc:
            raise IsolationError(
                operation="initialize_tenant",
                tenant_id=tenant.id,
                details={"database": db_name, "error": str(exc)},
            ) from exc

        try:
            async with self._master.connect() as conn:
                if self.dialect == DbDialect.POSTGRESQL:
                    result = await conn.execute(
                        text("SELECT 1 FROM pg_database WHERE datname = :name"),
                        {"name": db_name},
                    )
                    if result.scalar() is not None:
                        logger.warning("Database %r already exists — skipping CREATE", db_name)
                    else:
                        await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
                        logger.info("Created database %r for tenant %s", db_name, tenant.id)
                elif self.dialect == DbDialect.MYSQL:
                    await conn.execute(
                        text(
                            f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                        )
                    )
                    logger.info("Created database %r for tenant %s", db_name, tenant.id)

            if metadata is not None:
                engine = await self._get_engine(tenant)
                async with engine.begin() as conn:
                    await conn.run_sync(metadata.create_all)
                logger.info("Created tables in database %r", db_name)

        except IsolationError:
            raise
        except Exception as exc:
            raise IsolationError(
                operation="initialize_tenant",
                tenant_id=tenant.id,
                details={"database": db_name, "error": str(exc)},
            ) from exc

    async def destroy_tenant(self, tenant: Tenant, **kwargs: Any) -> None:
        """Drop *tenant*'s dedicated database.

        .. warning::
            Permanently destroys all tenant data.

        Args:
            tenant: The tenant to destroy.
            kwargs: Option to tenants

        Raises:
            IsolationError: When the database cannot be dropped.
        """
        if self.dialect == DbDialect.SQLITE:
            from pathlib import Path  # noqa: PLC0415
            from urllib.parse import urlparse  # noqa: PLC0415

            engine = await self._engine_cache.remove(tenant.id)
            if engine:
                await engine.dispose()
            url = self._tenant_url(tenant)
            # Use urlparse to correctly extract the file path from the URL,
            # handling both absolute (sqlite:////abs/path) and relative
            # (sqlite:///./rel/path) forms without fragile string splitting.
            parsed = urlparse(url)
            raw_path = parsed.netloc + parsed.path  # netloc is empty for sqlite
            db_path = Path(raw_path) if raw_path else None
            if db_path and db_path.exists():
                try:
                    db_path.unlink()
                    logger.warning("Deleted SQLite file %s for tenant %s", db_path, tenant.id)
                except OSError as exc:
                    raise IsolationError(
                        operation="destroy_tenant",
                        tenant_id=tenant.id,
                        details={"path": str(db_path), "error": str(exc)},
                    ) from exc
            # SQLite handling is complete — return early so we don't attempt
            # pg_database / DROP DATABASE logic below, which would fail against
            # a SQLite connection and raise a misleading IsolationError.
            return

        db_name = self._database_name(tenant)
        try:
            assert_safe_database_name(db_name, context=f"tenant id={tenant.id!r}")
        except ValueError as exc:
            raise IsolationError(
                operation="destroy_tenant",
                tenant_id=tenant.id,
                details={"database": db_name, "error": str(exc)},
            ) from exc

        evicted = await self._engine_cache.remove(tenant.id)
        if evicted:
            await evicted.dispose()

        try:
            async with self._master.connect() as conn:
                if self.dialect == DbDialect.POSTGRESQL:
                    # Terminate active connections before dropping.
                    await conn.execute(
                        text(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = :name AND pid <> pg_backend_pid()"
                        ),
                        {"name": db_name},
                    )
                    await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
                elif self.dialect == DbDialect.MYSQL:
                    await conn.execute(text(f"DROP DATABASE IF EXISTS `{db_name}`"))
            logger.warning("Destroyed database %r for tenant %s", db_name, tenant.id)
        except IsolationError:
            raise
        except Exception as exc:
            raise IsolationError(
                operation="destroy_tenant",
                tenant_id=tenant.id,
                details={"database": db_name, "error": str(exc)},
            ) from exc

    async def verify_isolation(self, tenant: Tenant) -> bool:
        """Return ``True`` if *tenant*'s database exists and is reachable.

        Args:
            tenant: Tenant to verify.

        Returns:
            ``True`` when the database exists; ``False`` otherwise.
        """
        if self.dialect == DbDialect.SQLITE:
            from pathlib import Path  # noqa: PLC0415
            from urllib.parse import urlparse  # noqa: PLC0415

            url = self._tenant_url(tenant)
            parsed = urlparse(url)
            raw_path = parsed.netloc + parsed.path
            db_path = Path(raw_path) if raw_path else None
            return db_path is not None and db_path.exists()

        db_name = self._database_name(tenant)
        try:
            assert_safe_database_name(db_name)
        except ValueError:
            return False

        try:
            async with self._master.connect() as conn:
                if self.dialect == DbDialect.POSTGRESQL:
                    r = await conn.execute(
                        text("SELECT 1 FROM pg_database WHERE datname = :name"),
                        {"name": db_name},
                    )
                elif self.dialect == DbDialect.MYSQL:
                    r = await conn.execute(
                        text(
                            "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
                            "WHERE SCHEMA_NAME = :name"
                        ),
                        {"name": db_name},
                    )
                else:
                    return False
                return r.scalar() is not None
        except Exception:
            return False

    async def close(self) -> None:
        """Dispose all per-tenant engines from the LRU cache and the master engine."""
        disposed = await self._engine_cache.dispose_all()
        logger.debug("Disposed %d per-tenant engines", disposed)
        await self._master.dispose()
        logger.info("DatabaseIsolationProvider closed")


__all__ = ["DatabaseIsolationProvider"]
