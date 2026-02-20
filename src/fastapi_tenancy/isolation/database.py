"""Database-per-tenant isolation — multi-database compatible.

Each tenant owns a separate database (or ``.db`` file for SQLite).  This
provides the strongest data isolation at the cost of the highest resource
overhead (one connection pool per tenant).

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
import logging
from contextlib import asynccontextmanager
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
    from fastapi_tenancy.core.types import Tenant

logger = logging.getLogger(__name__)


class DatabaseIsolationProvider(BaseIsolationProvider):
    """Separate database per tenant with automatic dialect-based provisioning.

    A single master engine connects to the admin/default database for DDL.
    Per-tenant engines are created lazily on the first request and cached for
    the lifetime of the application.

    Args:
        config: Tenancy configuration.
        master_engine: Optional pre-built master engine (used when this provider
            is created by :class:`~fastapi_tenancy.isolation.hybrid.HybridIsolationProvider`
            to share the underlying connection pool).

    Example::

        provider = DatabaseIsolationProvider(config)
        await provider.initialize_tenant(tenant, metadata=Base.metadata)

        async with provider.get_session(tenant) as session:
            result = await session.execute(select(Order))

        await provider.close()  # dispose all per-tenant engines
    """

    def __init__(
        self,
        config: TenancyConfig,
        master_engine: AsyncEngine | None = None,
    ) -> None:
        super().__init__(config)
        self.dialect = detect_dialect(str(config.database_url))
        self._engines: dict[str, AsyncEngine] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

        if master_engine is not None:
            self._master = master_engine
        else:
            kw: dict[str, Any] = {"echo": config.database_echo, "isolation_level": "AUTOCOMMIT"}
            if requires_static_pool(self.dialect):
                kw["poolclass"] = StaticPool
                kw["connect_args"] = {"check_same_thread": False}
                kw.pop("isolation_level", None)
            else:
                kw["pool_size"] = max(config.database_pool_size, 5)
                kw["max_overflow"] = config.database_max_overflow
                kw["pool_pre_ping"] = True
            self._master = create_async_engine(str(config.database_url), **kw)

        logger.info("DatabaseIsolationProvider dialect=%s", self.dialect.value)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _database_name(self, tenant: Tenant) -> str:
        slug = sanitize_identifier(tenant.identifier)
        return f"tenant_{slug}_db"

    def _tenant_url(self, tenant: Tenant) -> str:
        """Build the connection URL for *tenant*'s dedicated database."""
        import re

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

        # Replace the database name segment at the end of the URL.
        return re.sub(r"(/[^/?]*)(\?.*)?$", f"/{db_name}\\2", base)

    async def _get_engine(self, tenant: Tenant) -> AsyncEngine:
        """Return (or lazily create) the per-tenant engine.

        Double-checked locking prevents two coroutines from racing on the
        first request for the same tenant.
        """
        if tenant.id in self._engines:
            return self._engines[tenant.id]

        async with self._lock:
            if tenant.id in self._engines:  # re-check after acquiring lock
                return self._engines[tenant.id]

            url = self._tenant_url(tenant)
            kw: dict[str, Any] = {"echo": self.config.database_echo}
            if requires_static_pool(self.dialect):
                kw["poolclass"] = StaticPool
                kw["connect_args"] = {"check_same_thread": False}
            else:
                kw["pool_size"] = self.config.database_pool_size
                kw["max_overflow"] = self.config.database_max_overflow
                kw["pool_pre_ping"] = True
                kw["pool_recycle"] = self.config.database_pool_recycle

            engine = create_async_engine(url, **kw)
            self._engines[tenant.id] = engine
            logger.debug("Created engine tenant=%s", tenant.id)
            return engine

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

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

    async def apply_filters(self, query: Any, tenant: Tenant) -> Any:
        """No filtering required — each tenant has a dedicated database."""
        return query

    # ------------------------------------------------------------------
    # Provisioning
    # ------------------------------------------------------------------

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

    async def destroy_tenant(self, tenant: Tenant) -> None:
        """Drop *tenant*'s dedicated database.

        .. warning::
            Permanently destroys all tenant data.

        Args:
            tenant: The tenant to destroy.

        Raises:
            IsolationError: When the database cannot be dropped.
        """
        if self.dialect == DbDialect.SQLITE:
            import os

            engine = self._engines.pop(tenant.id, None)
            if engine:
                await engine.dispose()
            url = self._tenant_url(tenant)
            path = url.split("///", 1)[-1].lstrip("./")
            if path and os.path.exists(path):
                os.remove(path)
                logger.warning("Deleted SQLite file %s for tenant %s", path, tenant.id)
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

        if tenant.id in self._engines:
            await self._engines.pop(tenant.id).dispose()

        try:
            async with self._master.connect() as conn:
                if self.dialect == DbDialect.POSTGRESQL:
                    # Terminate all active connections before dropping.
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
        """Return ``True`` if *tenant*'s database exists and is reachable."""
        if self.dialect == DbDialect.SQLITE:
            import os

            url = self._tenant_url(tenant)
            path = url.split("///", 1)[-1].lstrip("./")
            return os.path.exists(path)

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
        """Dispose all per-tenant engines and the master engine."""
        for tid, engine in list(self._engines.items()):
            await engine.dispose()
            logger.debug("Closed engine tenant=%s", tid)
        self._engines.clear()
        await self._master.dispose()
        logger.info("DatabaseIsolationProvider closed")


__all__ = ["DatabaseIsolationProvider"]
