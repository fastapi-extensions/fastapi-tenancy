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

Critical fix: ``SET search_path``
-----------------------------------
PostgreSQL's ``SET`` command does **not** accept bound parameters (``%s`` /
``:name``).  The asyncpg driver raises a ``ProgrammingError`` at runtime when
``text("SET search_path TO :schema").bindparams(schema=...)`` is used.

The correct pattern is to validate the schema name with
:func:`~fastapi_tenancy.utils.validation.assert_safe_schema_name` — which
raises on invalid input — and then interpolate the pre-validated name as a
literal using an f-string:

.. code-block:: python

    assert_safe_schema_name(schema, context="...")
    await conn.execute(text(f'SET LOCAL search_path TO "{schema}", public'))

This module applies that pattern consistently in every DDL and session-setup
call.  The ``LOCAL`` modifier confines the ``search_path`` change to the
current transaction, preventing it from leaking to other sessions in the pool.

Security
--------
Every schema / table name is validated with :func:`assert_safe_schema_name`
*before* being interpolated into any DDL statement, providing defence against
SQL injection via tenant identifiers.
"""

from __future__ import annotations

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
    make_table_prefix,
    requires_static_pool,
    supports_native_schemas,
)
from fastapi_tenancy.utils.validation import assert_safe_schema_name

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy import MetaData

    from fastapi_tenancy.core.config import TenancyConfig
    from fastapi_tenancy.core.types import SelectT, Tenant
    from fastapi_tenancy.isolation.database import DatabaseIsolationProvider

logger = logging.getLogger(__name__)


class SchemaIsolationProvider(BaseIsolationProvider):
    """Schema-per-tenant isolation with automatic dialect-based fallback.

    PostgreSQL / MSSQL — native schema isolation
        Creates a dedicated schema per tenant.  Sets ``search_path`` (with
        ``SET LOCAL``) on every session connection so unqualified table
        references resolve correctly.

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
            kw: dict[str, Any] = {
                "echo": config.database_echo,
                "pool_pre_ping": config.database_pool_pre_ping,
            }
            if requires_static_pool(self.dialect):
                kw["poolclass"] = StaticPool
                kw["connect_args"] = {"check_same_thread": False}
                del kw["pool_pre_ping"]
            else:
                kw["pool_size"] = config.database_pool_size
                kw["max_overflow"] = config.database_max_overflow
                kw["pool_timeout"] = config.database_pool_timeout
                kw["pool_recycle"] = config.database_pool_recycle
            self.engine = create_async_engine(str(config.database_url), **kw)

        logger.info(
            "SchemaIsolationProvider dialect=%s native_schemas=%s",
            self.dialect.value,
            supports_native_schemas(self.dialect),
        )

        # For MySQL, delegate all operations to a single cached
        # DatabaseIsolationProvider that shares this provider's engine.
        self._mysql_delegate: DatabaseIsolationProvider | None = None
        if self.dialect == DbDialect.MYSQL:
            from fastapi_tenancy.isolation.database import (  # noqa: PLC0415
                DatabaseIsolationProvider,
            )

            self._mysql_delegate = DatabaseIsolationProvider(
                self.config, master_engine=self.engine
            )

    #######################
    # Schema name helpers #
    #######################

    def _schema_name(self, tenant: Tenant) -> str:
        """Return the raw schema name for *tenant* (no validation)."""
        return (
            tenant.schema_name
            if tenant.schema_name
            else self.config.get_schema_name(tenant.identifier)
        )

    def _validated_schema_name(self, tenant: Tenant) -> str:
        """Return and validate the schema name; raise :exc:`IsolationError` on failure.

        Args:
            tenant: Target tenant.

        Returns:
            Validated schema name string.

        Raises:
            IsolationError: When the schema name contains invalid characters.
        """
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
        """Return the table-name prefix for non-schema dialects.

        Args:
            tenant: Target tenant.

        Returns:
            Table-name prefix string ending with ``_``.
        """
        return make_table_prefix(tenant.identifier)

    ###########
    # Session #
    ###########

    @asynccontextmanager
    async def get_session(self, tenant: Tenant) -> AsyncIterator[AsyncSession]:
        """Yield a tenant-scoped ``AsyncSession``.

        Dispatches to the correct session strategy based on the detected
        database dialect.

        Args:
            tenant: Currently active tenant.

        Yields:
            Configured :class:`~sqlalchemy.ext.asyncio.AsyncSession`.

        Raises:
            IsolationError: When the session cannot be opened.
        """
        if self.dialect == DbDialect.MYSQL:
            assert self._mysql_delegate is not None
            async with self._mysql_delegate.get_session(tenant) as session:
                yield session
        elif supports_native_schemas(self.dialect):
            async with self._schema_session(tenant) as session:
                yield session
        else:
            async with self._prefix_session(tenant) as session:
                yield session

    @asynccontextmanager
    async def _schema_session(self, tenant: Tenant) -> AsyncIterator[AsyncSession]:
        """Yield a session with ``search_path`` set to the tenant's schema.

        Critical implementation detail — connection-level event listener
        -----------------------------------------------------------------
        ``SET LOCAL search_path`` is transaction-scoped: it reverts when the
        transaction commits or rolls back.  Under SQLAlchemy's autocommit
        mode, or whenever the route handler opens its own ``session.begin()``
        block after this method yields, the ``search_path`` would silently
        revert to the database default — breaking schema isolation.

        The fix is to install a ``@event.listens_for(conn.sync_connection,
        "begin")`` listener that re-applies ``SET LOCAL search_path`` at the
        start of **every** transaction opened on this connection during the
        request lifetime.  This is the pattern recommended by the SQLAlchemy
        docs for connection-level configuration that must survive multiple
        transactions on the same connection.

        PostgreSQL's ``SET`` command does **not** support bound parameters.
        The asyncpg driver rejects ``SET search_path TO :schema``.  After
        :func:`~fastapi_tenancy.utils.validation.assert_safe_schema_name`
        validates *schema* contains only safe characters, it is safe to
        interpolate as a literal.

        Args:
            tenant: Currently active tenant.

        Yields:
            Configured :class:`~sqlalchemy.ext.asyncio.AsyncSession`.

        Raises:
            IsolationError: When the session cannot be configured.
        """
        from sqlalchemy import event  # noqa: PLC0415

        schema = self._validated_schema_name(tenant)
        # After assert_safe_schema_name passes, schema contains only
        # lowercase letters, digits, and underscores — safe to interpolate.
        set_path_sql = f'SET LOCAL search_path TO "{schema}", public'

        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            try:
                # Install a connection-level listener so search_path is
                # re-applied at the start of every transaction, not just the
                # first one.  This is essential when the route handler opens
                # its own session.begin() block after the session is yielded.
                conn = await session.connection()
                sync_conn = conn.sync_connection

                @event.listens_for(sync_conn, "begin")
                def _set_search_path(sync_connection) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
                    sync_connection.exec_driver_sql(set_path_sql)

                # Apply immediately for the first (implicit) transaction.
                await session.execute(text(set_path_sql))
                logger.debug(
                    "search_path → %r (tenant %s)", schema, tenant.id
                )
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
        """Yield a session with tenant metadata stored in ``session.info``.

        Used for SQLite and other dialects that do not support native schemas.
        Route handlers use ``session.info["table_prefix"]`` to select the
        correct prefixed table name.

        Args:
            tenant: Currently active tenant.

        Yields:
            Configured :class:`~sqlalchemy.ext.asyncio.AsyncSession`.

        Raises:
            IsolationError: When the session cannot be opened.
        """
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

    #################
    # Query filters #
    #################

    async def apply_filters(self, query: SelectT, tenant: Tenant) -> SelectT:
        """Apply ``WHERE tenant_id = :id`` as a defence-in-depth filter.

        For native-schema dialects the ``search_path`` already enforces
        isolation; this filter is an additional safety net.  For prefix-mode
        dialects it is the primary isolation mechanism.

        Args:
            query: SQLAlchemy ``Select`` query.
            tenant: Currently active tenant.

        Returns:
            Filtered query.
        """
        from sqlalchemy import column  # noqa: PLC0415

        return query.where(column("tenant_id") == tenant.id)

    ################
    # Provisioning #
    ################

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

        Raises:
            IsolationError: When provisioning fails.
        """
        if self.dialect == DbDialect.MYSQL:
            assert self._mysql_delegate is not None
            await self._mysql_delegate.initialize_tenant(tenant, metadata=metadata)
            return

        if supports_native_schemas(self.dialect):
            await self._initialize_schema(tenant, metadata)
        else:
            await self._initialize_prefix(tenant, metadata)

    async def _initialize_schema(
        self,
        tenant: Tenant,
        metadata: MetaData | None,
    ) -> None:
        """Create ``CREATE SCHEMA IF NOT EXISTS`` and optionally create tables.

        Args:
            tenant: Target tenant.
            metadata: Optional SQLAlchemy ``MetaData`` for table creation.

        Raises:
            IsolationError: When schema creation fails.
        """
        schema = self._validated_schema_name(tenant)
        # Double-check at the DDL call-site for defence-in-depth.
        assert_safe_schema_name(schema, context=f"initialize_tenant tenant={tenant.id!r}")

        async with self.engine.begin() as conn:
            try:
                await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
                logger.info("Created schema %r for tenant %s", schema, tenant.id)
                if metadata is not None:
                    # SET LOCAL confines the change to this transaction only.
                    await conn.execute(
                        text(f'SET LOCAL search_path TO "{schema}", public')
                    )
                    await conn.run_sync(metadata.create_all)
                    logger.info("Created tables in schema %r", schema)
            except IsolationError:
                raise
            except Exception as exc:
                # Roll back and attempt cleanup on a separate connection so
                # we don't hide the real error with a "InFailedSqlTransaction".
                try:  # noqa: SIM105
                    await conn.rollback()
                except Exception:  # pragma: no cover  # noqa: S110
                    pass
                async with self.engine.connect() as cleanup_conn:
                    try:
                        await cleanup_conn.execute(
                            text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                        )
                        await cleanup_conn.commit()
                        logger.warning(
                            "Rolled back schema %r after failed initialize_tenant for tenant %s",
                            schema,
                            tenant.id,
                        )
                    except Exception as cleanup_exc:
                        # Log but don't re-raise — the original error is more important.
                        logger.exception(
                            "Schema cleanup failed for %r (tenant %s) after init error: %s",
                            schema,
                            tenant.id,
                            cleanup_exc,  # noqa: TRY401
                        )
                raise IsolationError(
                    operation="initialize_tenant",
                    tenant_id=tenant.id,
                    details={"schema": schema, "error": str(exc)},
                ) from exc

    async def _initialize_prefix(
        self,
        tenant: Tenant,
        metadata: MetaData | None,
    ) -> None:
        """Create prefixed tables for SQLite / unknown dialects.

        Uses ``Table.to_metadata()`` to copy the full table definition
        (columns, constraints, indexes, FK relationships) and remaps FK
        references to point at the prefixed counterparts.

        Args:
            tenant: Target tenant.
            metadata: Application ``MetaData`` — required for table creation.

        Raises:
            IsolationError: When table creation fails.
        """
        prefix = self.get_table_prefix(tenant)
        if metadata is None:
            logger.info(
                "No metadata supplied for prefix-mode tenant %s — skipping.",
                tenant.id,
            )
            return

        import sqlalchemy as sa  # noqa: PLC0415

        name_map = {t.name: f"{prefix}{t.name}" for t in metadata.sorted_tables}
        prefixed_meta = sa.MetaData()

        for table in metadata.sorted_tables:
            table.to_metadata(prefixed_meta, name=name_map[table.name])

        # Remap ForeignKeyConstraints to reference prefixed table names.
        for table in prefixed_meta.sorted_tables:
            for constraint in list(table.constraints):
                if not isinstance(constraint, sa.ForeignKeyConstraint):
                    continue
                new_refcols: list[str] = []
                needs_remap = False
                for fk in constraint.elements:
                    parts = fk.target_fullname.split(".")
                    if len(parts) == 2 and parts[0] in name_map:
                        new_refcols.append(f"{name_map[parts[0]]}.{parts[1]}")
                        needs_remap = True
                    else:
                        new_refcols.append(fk.target_fullname)
                if needs_remap:
                    local_cols = [c.key for c in constraint.columns]
                    table.constraints.discard(constraint)
                    sa.ForeignKeyConstraint(local_cols, new_refcols, table=table)

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

    async def destroy_tenant(self, tenant: Tenant, **kwargs: Any) -> None:  # noqa: D417
        """Drop the tenant's isolation namespace.

        .. warning::
            Permanently destroys all tenant data.

        Args:
            tenant: The tenant to deprovision.

        Raises:
            IsolationError: When the drop operation fails.
        """
        if self.dialect == DbDialect.MYSQL:
            assert self._mysql_delegate is not None
            await self._mysql_delegate.destroy_tenant(tenant)
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
        """Drop all prefixed tables for a prefix-mode tenant.

        Args:
            tenant: Target tenant.

        Raises:
            IsolationError: When the drop operation fails.
        """
        prefix = self.get_table_prefix(tenant)
        async with self.engine.begin() as conn:
            try:
                def _drop_tables(sync_conn: Any) -> int:
                    from sqlalchemy import inspect as sa_inspect  # noqa: PLC0415
                    from sqlalchemy import text as sync_text  # noqa: PLC0415

                    insp = sa_inspect(sync_conn)
                    tables = [t for t in insp.get_table_names() if t.startswith(prefix)]
                    for tbl in tables:
                        assert_safe_schema_name(
                            tbl, context=f"prefix table drop tenant={tenant.id!r}"
                        )
                        sync_conn.execute(sync_text(f'DROP TABLE IF EXISTS "{tbl}"'))
                    logger.warning(
                        "Destroyed %d prefixed tables for tenant %s",
                        len(tables),
                        tenant.id,
                    )
                    return len(tables)

                await conn.run_sync(_drop_tables)
            except Exception as exc:
                raise IsolationError(
                    operation="destroy_tenant",
                    tenant_id=tenant.id,
                    details={"prefix": prefix, "error": str(exc)},
                ) from exc

    ################
    # Verification #
    ################

    async def verify_isolation(self, tenant: Tenant) -> bool:
        """Verify that the tenant's schema / tables exist and are reachable.

        Args:
            tenant: Tenant to verify.

        Returns:
            ``True`` when isolation structures exist; ``False`` on failure.
        """
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

        prefix = self.get_table_prefix(tenant)
        try:
            async with self.engine.connect() as conn:
                def _check_tables(sync_conn: Any) -> bool:
                    from sqlalchemy import inspect as sa_inspect  # noqa: PLC0415

                    insp = sa_inspect(sync_conn)
                    tables = insp.get_table_names()
                    return any(t.startswith(prefix) for t in tables)

                return await conn.run_sync(_check_tables)
        except Exception:
            return False

    async def close(self) -> None:
        """Dispose the engine and release pooled connections."""
        await self.engine.dispose()
        logger.info("SchemaIsolationProvider closed")


__all__ = ["SchemaIsolationProvider"]
