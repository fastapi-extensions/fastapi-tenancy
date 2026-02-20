"""Schema-per-tenant isolation provider — multi-database compatible.

Strategy selection (auto-detected from database URL)
------------------------------------------------------
+------------------+-------------------------------+------------------------+
| Dialect          | Mechanism                     | Fallback               |
+==================+===============================+========================+
| PostgreSQL       | ``CREATE SCHEMA`` + search_path | —                    |
+------------------+-------------------------------+------------------------+
| MSSQL            | ``CREATE SCHEMA``              | —                     |
+------------------+-------------------------------+------------------------+
| MySQL / MariaDB  | Delegates to Database provider| —                     |
+------------------+-------------------------------+------------------------+
| SQLite           | Table-name prefix             | —                     |
+------------------+-------------------------------+------------------------+

Security
--------
Every schema / table name is validated with :func:`assert_safe_schema_name`
*before* being interpolated into any DDL statement, providing defence against
SQL injection via tenant identifiers.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from fastapi_tenancy.core.exceptions import IsolationError
from fastapi_tenancy.isolation.base import BaseIsolationProvider
from fastapi_tenancy.utils.db_compat import (
    DbDialect,
    detect_dialect,
    make_table_prefix,
    requires_static_pool,
    supports_native_schemas,
)
from fastapi_tenancy.utils.validation import assert_safe_schema_name

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy import MetaData

    from fastapi_tenancy.core.config import TenancyConfig
    from fastapi_tenancy.core.types import Tenant

logger = logging.getLogger(__name__)


class SchemaIsolationProvider(BaseIsolationProvider):
    """Schema-per-tenant isolation with automatic dialect-based fallback.

    PostgreSQL / MSSQL — native schema isolation
        Creates a dedicated schema per tenant.  Sets ``search_path`` on
        every session so unqualified table references resolve correctly.

    SQLite / unknown dialects — table-name prefix
        Copies the application ``MetaData`` with a tenant-specific prefix
        applied to every table name (e.g. ``t_acme_corp_users``).

    MySQL / MariaDB — database-per-tenant delegation
        MySQL's ``SCHEMA`` == ``DATABASE``.  Transparently delegates to
        :class:`~fastapi_tenancy.isolation.database.DatabaseIsolationProvider`.

    Args:
        config: Tenancy configuration.
        engine: Optional pre-built engine to reuse (avoids a duplicate pool
            when this provider is used inside
            :class:`~fastapi_tenancy.isolation.hybrid.HybridIsolationProvider`).

    Example::

        provider = SchemaIsolationProvider(config)
        await provider.initialize_tenant(tenant, metadata=Base.metadata)

        async with provider.get_session(tenant) as session:
            result = await session.execute(select(User))
    """

    def __init__(self, config: TenancyConfig, engine: AsyncEngine | None = None) -> None:
        super().__init__(config)
        self.dialect = detect_dialect(str(config.database_url))

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
            "SchemaIsolationProvider dialect=%s native_schemas=%s",
            self.dialect.value,
            supports_native_schemas(self.dialect),
        )

    # ------------------------------------------------------------------
    # Schema name helpers
    # ------------------------------------------------------------------

    def _schema_name(self, tenant: Tenant) -> str:
        """Return the raw schema name for *tenant*."""
        return (
            tenant.schema_name
            if tenant.schema_name
            else self.config.get_schema_name(tenant.identifier)
        )

    def _validated_schema_name(self, tenant: Tenant) -> str:
        """Return and validate the schema name; raise :exc:`IsolationError` on failure."""
        name = self._schema_name(tenant)
        try:
            assert_safe_schema_name(name, context=f"tenant id={tenant.id!r}")
        except ValueError as exc:
            raise IsolationError(
                operation="validate_schema_name",
                tenant_id=tenant.id,
                details={"schema": name, "error": str(exc)},
            ) from exc
        return name

    def get_table_prefix(self, tenant: Tenant) -> str:
        """Return the table-name prefix for non-schema dialects."""
        return make_table_prefix(tenant.identifier)

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def get_session(self, tenant: Tenant) -> AsyncIterator[AsyncSession]:
        """Yield a tenant-scoped ``AsyncSession``."""
        if self.dialect == DbDialect.MYSQL:
            async with self._mysql_session(tenant) as session:
                yield session
        elif supports_native_schemas(self.dialect):
            async with self._schema_session(tenant) as session:
                yield session
        else:
            async with self._prefix_session(tenant) as session:
                yield session

    @asynccontextmanager
    async def _schema_session(self, tenant: Tenant) -> AsyncIterator[AsyncSession]:
        schema = self._validated_schema_name(tenant)
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            try:
                await session.execute(
                    text("SET search_path TO :schema, public").bindparams(schema=schema)
                )
                logger.debug("search_path → %r (tenant %s)", schema, tenant.id)
                yield session
            except IsolationError:
                raise
            except Exception as exc:
                await session.rollback()
                raise IsolationError(
                    operation="get_session",
                    tenant_id=tenant.id,
                    details={"schema": schema, "error": str(exc)},
                ) from exc

    @asynccontextmanager
    async def _prefix_session(self, tenant: Tenant) -> AsyncIterator[AsyncSession]:
        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            try:
                session.info["tenant_id"] = tenant.id
                session.info["table_prefix"] = self.get_table_prefix(tenant)
                yield session
            except Exception as exc:
                await session.rollback()
                raise IsolationError(
                    operation="get_session",
                    tenant_id=tenant.id,
                    details={"mode": "prefix", "error": str(exc)},
                ) from exc

    @asynccontextmanager
    async def _mysql_session(self, tenant: Tenant) -> AsyncIterator[AsyncSession]:
        from fastapi_tenancy.isolation.database import DatabaseIsolationProvider

        db_provider = DatabaseIsolationProvider(self.config, master_engine=self.engine)
        async with db_provider.get_session(tenant) as session:
            yield session

    # ------------------------------------------------------------------
    # Provisioning
    # ------------------------------------------------------------------

    async def initialize_tenant(
        self,
        tenant: Tenant,
        metadata: MetaData | None = None,
    ) -> None:
        """Create the tenant's isolation namespace.

        * Native schemas → ``CREATE SCHEMA IF NOT EXISTS``
        * Prefix mode → copy+rename tables in *metadata* with tenant prefix
        * MySQL → delegates to database provider

        Args:
            tenant: Target tenant.
            metadata: Application :class:`~sqlalchemy.MetaData`.  When supplied,
                ``create_all`` is executed in the tenant namespace.
        """
        if self.dialect == DbDialect.MYSQL:
            from fastapi_tenancy.isolation.database import DatabaseIsolationProvider

            db = DatabaseIsolationProvider(self.config, master_engine=self.engine)
            await db.initialize_tenant(tenant, metadata=metadata)
            return

        if supports_native_schemas(self.dialect):
            await self._initialize_schema(tenant, metadata)
        else:
            await self._initialize_prefix(tenant, metadata)

    async def _initialize_schema(
        self, tenant: Tenant, metadata: MetaData | None
    ) -> None:
        schema = self._validated_schema_name(tenant)
        # Explicit assertion at the DDL call-site for defence-in-depth.
        assert_safe_schema_name(schema, context=f"initialize_tenant tenant={tenant.id!r}")
        async with self.engine.begin() as conn:
            try:
                await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
                logger.info("Created schema %r for tenant %s", schema, tenant.id)
                if metadata is not None:
                    await conn.execute(
                        text("SET search_path TO :schema, public").bindparams(schema=schema)
                    )
                    await conn.run_sync(metadata.create_all)
                    logger.info("Created tables in schema %r", schema)
            except IsolationError:
                raise
            except Exception as exc:
                with suppress(Exception):
                    await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
                raise IsolationError(
                    operation="initialize_tenant",
                    tenant_id=tenant.id,
                    details={"schema": schema, "error": str(exc)},
                ) from exc

    async def _initialize_prefix(
        self, tenant: Tenant, metadata: MetaData | None
    ) -> None:
        """Create prefixed tables for SQLite / unknown dialects.

        Uses ``Table.to_metadata()`` to copy the full table definition
        (columns, constraints, indexes, FK relationships) and remaps FK
        references to point at the prefixed counterparts.
        """
        prefix = self.get_table_prefix(tenant)
        if metadata is None:
            logger.info(
                "No metadata supplied for prefix-mode tenant %s — skipping table creation.",
                tenant.id,
            )
            return

        import sqlalchemy as sa

        name_map = {t.name: f"{prefix}{t.name}" for t in metadata.sorted_tables}
        prefixed_meta = sa.MetaData()

        for table in metadata.sorted_tables:
            table.to_metadata(prefixed_meta, name=name_map[table.name])

        for table in prefixed_meta.sorted_tables:
            for col in table.columns:
                for fk in list(col.foreign_keys):
                    parts = fk.target_fullname.split(".")
                    if len(parts) == 2 and parts[0] in name_map:
                        col.foreign_keys.discard(fk)
                        col.append_foreign_key(
                            sa.ForeignKey(f"{name_map[parts[0]]}.{parts[1]}")
                        )

        async with self.engine.begin() as conn:
            try:
                await conn.run_sync(prefixed_meta.create_all)
                logger.info(
                    "Created %d prefixed tables for tenant %s (prefix=%r)",
                    len(prefixed_meta.sorted_tables),
                    tenant.id,
                    prefix,
                )
            except Exception as exc:
                raise IsolationError(
                    operation="initialize_tenant",
                    tenant_id=tenant.id,
                    details={"prefix": prefix, "error": str(exc)},
                ) from exc

    async def destroy_tenant(self, tenant: Tenant) -> None:
        """Drop the tenant's isolation namespace.

        .. warning::
            Permanently destroys all tenant data.
        """
        if self.dialect == DbDialect.MYSQL:
            from fastapi_tenancy.isolation.database import DatabaseIsolationProvider

            db = DatabaseIsolationProvider(self.config, master_engine=self.engine)
            await db.destroy_tenant(tenant)
            return

        if supports_native_schemas(self.dialect):
            schema = self._validated_schema_name(tenant)
            assert_safe_schema_name(schema, context=f"destroy_tenant tenant={tenant.id!r}")
            async with self.engine.begin() as conn:
                try:
                    await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
                    logger.warning("Destroyed schema %r for tenant %s", schema, tenant.id)
                except Exception as exc:
                    raise IsolationError(
                        operation="destroy_tenant",
                        tenant_id=tenant.id,
                        details={"schema": schema, "error": str(exc)},
                    ) from exc
        else:
            await self._destroy_prefix(tenant)

    async def _destroy_prefix(self, tenant: Tenant) -> None:
        prefix = self.get_table_prefix(tenant)
        async with self.engine.begin() as conn:
            try:
                from sqlalchemy import inspect as sa_inspect

                def _drop_tables(sync_conn: Any) -> None:
                    insp = sa_inspect(sync_conn)
                    tables = [t for t in insp.get_table_names() if t.startswith(prefix)]
                    for tbl in tables:
                        assert_safe_schema_name(
                            tbl, context=f"prefix table drop tenant={tenant.id!r}"
                        )
                        sync_conn.execute(text(f'DROP TABLE IF EXISTS "{tbl}"'))
                    logger.warning(
                        "Destroyed %d prefixed tables for tenant %s",
                        len(tables),
                        tenant.id,
                    )

                await conn.run_sync(_drop_tables)
            except Exception as exc:
                raise IsolationError(
                    operation="destroy_tenant",
                    tenant_id=tenant.id,
                    details={"prefix": prefix, "error": str(exc)},
                ) from exc

    async def apply_filters(self, query: Any, tenant: Tenant) -> Any:
        """Apply ``WHERE tenant_id = :id`` as defence-in-depth.

        For native-schema dialects the ``search_path`` already enforces
        isolation; this filter is an additional safety net.  For prefix-mode
        dialects it is the primary isolation mechanism.
        """
        if hasattr(query, "where"):
            from sqlalchemy import column

            return query.where(column("tenant_id") == tenant.id)
        return query

    async def verify_isolation(self, tenant: Tenant) -> bool:
        """Verify that the tenant's schema / tables exist and are reachable."""
        if supports_native_schemas(self.dialect):
            schema = self._schema_name(tenant)
            try:
                async with self.engine.connect() as conn:
                    result = await conn.execute(
                        text(
                            "SELECT schema_name FROM information_schema.schemata "
                            "WHERE schema_name = :name"
                        ),
                        {"name": schema},
                    )
                    return result.scalar() is not None
            except Exception:
                return False
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def close(self) -> None:
        """Dispose the engine and release pooled connections."""
        await self.engine.dispose()
        logger.info("SchemaIsolationProvider closed")


__all__ = ["SchemaIsolationProvider"]
