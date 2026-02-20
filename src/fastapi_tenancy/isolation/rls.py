"""Row-Level Security (RLS) isolation — multi-database compatible.

Dialect behaviour
-----------------
+------------------+---------------------------------------+-------------------------------+
| Dialect          | Session configuration                 | Primary filter                |
+==================+=======================================+===============================+
| PostgreSQL       | ``SET app.current_tenant = :id``      | DB-level RLS + WHERE clause   |
+------------------+---------------------------------------+-------------------------------+
| MySQL            | ``SET @current_tenant = :id``         | Explicit WHERE clause         |
+------------------+---------------------------------------+-------------------------------+
| SQLite / other   | ``session.info["tenant_id"] = id``    | Explicit WHERE clause         |
+------------------+---------------------------------------+-------------------------------+

The ``WHERE tenant_id = :id`` filter applied by :meth:`apply_filters` acts as
defence-in-depth even for PostgreSQL where RLS policies are the primary guard.
All filters use bound parameters — never string interpolation.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from fastapi_tenancy.core.exceptions import IsolationError
from fastapi_tenancy.isolation.base import BaseIsolationProvider
from fastapi_tenancy.utils.db_compat import (
    detect_dialect,
    get_set_tenant_sql,
    requires_static_pool,
    supports_native_rls,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy import MetaData

    from fastapi_tenancy.core.config import TenancyConfig
    from fastapi_tenancy.core.types import Tenant

logger = logging.getLogger(__name__)


class RLSIsolationProvider(BaseIsolationProvider):
    """RLS-style isolation with automatic dialect-based degradation.

    On PostgreSQL, ``SET app.current_tenant`` activates server-side RLS
    policies.  On all other databases the provider falls back to explicit
    ``WHERE tenant_id = :id`` filtering via :meth:`apply_filters`.

    A one-time warning is emitted when initialised on a non-PostgreSQL
    dialect so operators are aware that native RLS is not active.

    PostgreSQL setup (one-time DDL per tenant-scoped table)::

        ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
        ALTER TABLE orders FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON orders
            USING (tenant_id = current_setting('app.current_tenant'));

    Args:
        config: Tenancy configuration.
        engine: Optional pre-built engine (avoids a duplicate pool when
            used inside :class:`~fastapi_tenancy.isolation.hybrid.HybridIsolationProvider`).

    Example::

        provider = RLSIsolationProvider(config)

        async with provider.get_session(tenant) as session:
            q = await provider.apply_filters(select(Order), tenant)
            result = await session.execute(q)
    """

    def __init__(self, config: TenancyConfig, engine: AsyncEngine | None = None) -> None:
        super().__init__(config)
        self.dialect = detect_dialect(str(config.database_url))

        if not supports_native_rls(self.dialect):
            logger.warning(
                "RLSIsolationProvider: dialect=%s does not support native RLS. "
                "Falling back to explicit WHERE tenant_id filter. "
                "Ensure apply_filters() is called on every query.",
                self.dialect.value,
            )

        if engine is not None:
            self.engine = engine
        else:
            kw: dict[str, Any] = {"echo": config.database_echo}
            if requires_static_pool(self.dialect):
                kw["poolclass"] = StaticPool
                kw["connect_args"] = {"check_same_thread": False}
            else:
                kw["pool_size"] = config.database_pool_size
                kw["max_overflow"] = config.database_max_overflow
                kw["pool_timeout"] = config.database_pool_timeout
                kw["pool_recycle"] = config.database_pool_recycle
                kw["pool_pre_ping"] = True
            self.engine = create_async_engine(str(config.database_url), **kw)

        logger.info(
            "RLSIsolationProvider dialect=%s native_rls=%s",
            self.dialect.value,
            supports_native_rls(self.dialect),
        )

    @asynccontextmanager
    async def get_session(self, tenant: Tenant) -> AsyncIterator[AsyncSession]:
        """Yield a session with the tenant context variable configured.

        Args:
            tenant: The currently active tenant.

        Yields:
            A configured :class:`~sqlalchemy.ext.asyncio.AsyncSession`.

        Raises:
            IsolationError: When the session cannot be opened or the tenant
                context variable cannot be set.
        """
        set_sql = get_set_tenant_sql(self.dialect)
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            try:
                if set_sql:
                    await session.execute(text(set_sql), {"tenant_id": tenant.id})
                    logger.debug(
                        "Set tenant context tenant=%s dialect=%s",
                        tenant.id,
                        self.dialect.value,
                    )
                else:
                    # Store in session.info for apply_filters() on non-RLS dialects.
                    session.info["tenant_id"] = tenant.id
                yield session
            except IsolationError:
                raise
            except Exception as exc:
                await session.rollback()
                raise IsolationError(
                    operation="get_session",
                    tenant_id=tenant.id,
                    details={"dialect": self.dialect.value, "error": str(exc)},
                ) from exc

    async def apply_filters(self, query: Any, tenant: Tenant) -> Any:
        """Return *query* with ``WHERE tenant_id = :id`` appended.

        Uses a SQLAlchemy bound parameter — never string interpolation.

        Args:
            query: SQLAlchemy Core or ORM query.
            tenant: Currently active tenant.

        Returns:
            Filtered query (or *query* unchanged if it has no ``.where()``).
        """
        if hasattr(query, "where"):
            from sqlalchemy import column

            return query.where(column("tenant_id") == tenant.id)
        return query

    async def initialize_tenant(self, tenant: Tenant) -> None:
        """No structural provisioning required for RLS / filter mode.

        The application is responsible for creating RLS policies via manual
        DDL migrations.  This method logs readiness information only.

        Args:
            tenant: Newly created tenant.
        """
        logger.info(
            "RLS tenant %s ready (dialect=%s native_rls=%s)",
            tenant.id,
            self.dialect.value,
            supports_native_rls(self.dialect),
        )

    async def destroy_tenant(
        self,
        tenant: Tenant,
        *,
        table_names: list[str] | None = None,
        metadata: MetaData | None = None,
    ) -> None:
        """Delete all rows belonging to *tenant* from the shared tables.

        Either ``table_names`` or ``metadata`` must be supplied so the
        provider knows which tables to purge.

        Args:
            tenant: The tenant whose data should be deleted.
            table_names: Explicit list of table names to purge.
            metadata: Application :class:`~sqlalchemy.MetaData`.  Tables with
                a ``tenant_id`` column are purged automatically.

        Raises:
            IsolationError: When neither ``table_names`` nor ``metadata`` is
                provided, or when a DELETE statement fails.
        """
        from fastapi_tenancy.utils.validation import assert_safe_schema_name

        if table_names is None and metadata is None:
            raise IsolationError(
                operation="destroy_tenant",
                tenant_id=tenant.id,
                details={
                    "reason": (
                        "RLS destroy_tenant requires either table_names= or metadata=. "
                        "Pass the application metadata or an explicit list of table names."
                    )
                },
            )

        tables: list[str] = list(table_names or [])
        if metadata is not None:
            for t in metadata.sorted_tables:
                if "tenant_id" in t.c:
                    tables.append(t.name)

        if not tables:
            logger.warning(
                "destroy_tenant: no tables with tenant_id found for tenant %s",
                tenant.id,
            )
            return

        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            try:
                for tbl in tables:
                    try:
                        assert_safe_schema_name(tbl, context="destroy_tenant")
                    except ValueError as exc:
                        raise IsolationError(
                            operation="destroy_tenant",
                            tenant_id=tenant.id,
                            details={"table": tbl, "error": str(exc)},
                        ) from exc
                    await session.execute(
                        text(f'DELETE FROM "{tbl}" WHERE tenant_id = :tid'),  # noqa: S608
                        {"tid": tenant.id},
                    )
                    logger.info("Deleted rows from %r for tenant %s", tbl, tenant.id)
                await session.commit()
                logger.warning(
                    "destroy_tenant completed: purged %d tables for tenant %s",
                    len(tables),
                    tenant.id,
                )
            except IsolationError:
                await session.rollback()
                raise
            except Exception as exc:
                await session.rollback()
                raise IsolationError(
                    operation="destroy_tenant",
                    tenant_id=tenant.id,
                    details={"tables": tables, "error": str(exc)},
                ) from exc

    async def close(self) -> None:
        """Dispose the engine and release pooled connections."""
        await self.engine.dispose()
        logger.info("RLSIsolationProvider closed")


__all__ = ["RLSIsolationProvider"]
