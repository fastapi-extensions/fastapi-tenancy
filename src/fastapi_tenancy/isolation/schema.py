"""Schema-per-tenant isolation provider — multi-database compatible.

Strategy selection (auto-detected from database URL)
------------------------------------------------------
+------------------+---------------------------------+----------------------+
| Dialect          | Mechanism                       | Fallback             |
+==================+===============================+========================+
| PostgreSQL       | ``CREATE SCHEMA`` + search_path | —                    |
+------------------+---------------------------------+----------------------+
| MSSQL            | ``CREATE SCHEMA``               | —                    |
+------------------+---------------------------------+----------------------+
| MySQL / MariaDB  | Delegates to Database provider  | —                    |
+------------------+---------------------------------+----------------------+
| SQLite           | Table-name prefix               | —                    |
+------------------+---------------------------------+----------------------+

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

Event listener design — ``Session.after_begin``
------------------------------------------------
The final implementation uses ``Session.after_begin(session, transaction,
connection)`` — a Session-level event that fires for **every** transaction
the session begins, including those that start after a ``commit()`` releases
and re-acquires the connection.

Earlier iterations tried:

1. **Engine-level ``begin`` on ``sync_engine``** — race condition: all
   concurrent requests share one listener, so Tenant A's listener fires on
   Tenant B's connection.

2. **Pool ``checkout``/``checkin``** — the asyncpg dialect wraps the raw
   DBAPI connection in ``AdaptedConnection``, which does **not** support
   SQLAlchemy events.  Attaching ``begin`` to it raises
   ``InvalidRequestError: No such event 'begin'``.

3. **``Connection.begin`` on ``conn.sync_connection``** — fires only while
   the same physical connection is held.  After ``session.commit()`` with
   ``autobegin=False``, the connection is released back to the pool and the
   next transaction uses a *new* ``Connection`` object, making the listener
   on the old object useless.

``Session.after_begin`` solves all three problems:

- It is scoped to **this session object** — invisible to other sessions.
- It fires for **every** transaction, even after commits that release
  the underlying connection back to the pool.
- It receives the **current** ``Connection`` as an argument so
  ``SET LOCAL search_path`` can be issued on the correct connection.

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

            self._mysql_delegate = DatabaseIsolationProvider(self.config, master_engine=self.engine)

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
        elif self.dialect == DbDialect.MSSQL:
            async with self._mssql_schema_session(tenant) as session:
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

        Isolation mechanism
        -------------------
        ``SET LOCAL search_path`` is **transaction-scoped**: PostgreSQL reverts
        it automatically when the current transaction ends.  To ensure every
        transaction on this session — including those that start after a
        ``commit()`` — uses the correct schema, we listen to the
        ``Session.after_begin`` event on the underlying sync session.

        Why ``Session.after_begin``, not ``Connection.begin``
        -------------------------------------------------------
        ``Connection.begin`` fires on the Connection object returned by
        ``await session.connection()``.  However, when a session commits with
        ``autobegin=False``, SQLAlchemy **releases the connection back to the
        pool** and acquires a new one for the next transaction.  The next
        transaction therefore runs on a *different* ``Connection`` object —
        making a listener on the old ``Connection`` useless for subsequent
        transactions.

        ``Session.after_begin(session, transaction, connection)`` fires
        **every** time the session starts a new transaction, regardless of how
        many commits have occurred, and provides the *current* ``Connection``
        as an argument.  This makes it the correct hook for re-applying
        ``SET LOCAL search_path`` after every commit/begin cycle.

        The listener is registered on ``session.sync_session`` (the underlying
        synchronous ``Session``), and the ``connection`` argument it receives
        is the current synchronous ``Connection``.  We call
        ``connection.exec_driver_sql`` on it directly to issue the SET command
        before any application SQL runs in that transaction.

        Per-session scope
        -----------------
        The listener is on the *session* object, which is created fresh for
        each call to ``_schema_session``.  It is invisible to any other
        concurrent session.  It is removed in ``finally`` before the session
        is discarded so the session object (and its cycle) is garbage-collected
        cleanly.

        PostgreSQL ``SET`` and bound parameters
        ----------------------------------------
        PostgreSQL's ``SET`` statement does **not** accept bound parameters.
        After :func:`~fastapi_tenancy.utils.validation.assert_safe_schema_name`
        validates that *schema* contains only ``[a-z0-9_]``, it is safe to
        interpolate as a literal string.

        Args:
            tenant: Currently active tenant.

        Yields:
            Configured :class:`~sqlalchemy.ext.asyncio.AsyncSession`.

        Raises:
            IsolationError: When the session cannot be configured.
        """
        from sqlalchemy import event  # noqa: PLC0415

        schema = self._validated_schema_name(tenant)
        # assert_safe_schema_name guarantees only [a-z0-9_] — safe to interpolate.
        set_path_sql = f'SET LOCAL search_path TO "{schema}", public'

        def _after_begin(
            sync_session: Any,
            transaction: Any,
            connection: Any,
        ) -> None:
            """Re-apply SET LOCAL search_path at the start of every transaction.

            Called by SQLAlchemy's Session.after_begin event with the current
            Connection for this transaction.  Using exec_driver_sql bypasses
            SQLAlchemy's parameter layer (necessary since SET LOCAL does not
            accept bound parameters in PostgreSQL).
            """
            connection.exec_driver_sql(set_path_sql)
            logger.debug(
                "search_path → %r (tenant %s) [after_begin]",
                schema,
                tenant.id,
            )

        try:
            async with AsyncSession(self.engine, expire_on_commit=False) as session:
                # Register the after_begin listener on the underlying sync
                # session — this fires for every transaction, including those
                # that start after a commit() releases the connection.
                event.listen(session.sync_session, "after_begin", _after_begin)
                try:
                    # Apply search_path immediately for the first transaction.
                    # The after_begin listener will not fire until the NEXT
                    # transaction, so we need this explicit call for the first one.
                    await session.execute(text(set_path_sql))
                    logger.debug("search_path → %r (tenant %s) [initial]", schema, tenant.id)
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
                finally:
                    # Remove the listener while the session object is still
                    # alive so the event system releases its reference cleanly.
                    if event.contains(session.sync_session, "after_begin", _after_begin):
                        event.remove(session.sync_session, "after_begin", _after_begin)
        except IsolationError:
            raise
        except Exception as exc:
            # Wrap any session-construction errors that escaped the inner try.
            raise IsolationError(
                operation="get_session",
                tenant_id=tenant.id,
                details={"schema": schema, "error": str(exc)},
            ) from exc

    @asynccontextmanager
    async def _mssql_schema_session(self, tenant: Tenant) -> AsyncIterator[AsyncSession]:
        """Yield a session scoped to the tenant's MSSQL schema.

        Why not ``ALTER USER … WITH DEFAULT_SCHEMA``
        ---------------------------------------------
        ``ALTER USER`` changes the *persistent* default schema of a database
        principal.  It is forbidden for the ``dbo`` user (error 15150), which
        is exactly the principal that the ``sa`` login — the standard
        development/container login — resolves to.  Any production deployment
        using a high-privilege login will hit the same restriction.

        Correct approach — schema translation map
        ------------------------------------------
        SQLAlchemy 2.0 supports per-execution schema translation via
        ``execution_options(schema_translate_map={None: schema})``.  This
        instructs the ORM/Core layer to rewrite every unqualified table
        reference (schema=None) to ``[schema].[table]`` at SQL-generation time,
        without issuing any DDL against the database user.

        The schema name is stored in ``session.info["schema"]`` so that route
        handlers can also build raw fully-qualified identifiers when they
        bypass the ORM (e.g. ``sa.text(f"SELECT … FROM [{session.info['schema']}].[items]")``).

        Args:
            tenant: Currently active tenant.

        Yields:
            Configured :class:`~sqlalchemy.ext.asyncio.AsyncSession` with
            ``schema_translate_map`` applied and ``session.info`` populated.

        Raises:
            IsolationError: When the session cannot be configured.
        """
        schema = self._validated_schema_name(tenant)

        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            try:
                # Apply schema translation so every unqualified ORM table
                # reference becomes [schema].[table] in generated SQL.
                await session.connection(execution_options={"schema_translate_map": {None: schema}})
                session.info["schema"] = schema
                session.info["tenant_id"] = tenant.id
                logger.debug("MSSQL schema_translate_map → %r (tenant %s)", schema, tenant.id)
                yield session
            except IsolationError:
                raise
            except Exception as exc:
                await session.rollback()
                raise IsolationError(
                    operation="get_session",
                    tenant_id=tenant.id,
                    details={"schema": schema, "dialect": "mssql", "error": str(exc)},
                ) from exc

    @asynccontextmanager
    async def _prefix_session(self, tenant: Tenant) -> AsyncIterator[AsyncSession]:
        """Yield a session with tenant metadata stored in ``session.info``.

        Used for SQLite and other dialects that do not support native schemas.
        Route handlers use ``session.info["table_prefix"]`` to select the
        correct prefixed table name.

        .. warning:: Multi-transaction sessions
            Unlike the schema/RLS session methods, this method does **not**
            install a connection-level ``begin`` event listener because there
            is no SQL command to re-apply — the prefix is stored in
            ``session.info``, which persists for the lifetime of the session
            object regardless of how many transactions are opened.

            However, if your route handler creates a *new* ``AsyncSession``
            instance manually (rather than using the one yielded here), it
            will **not** have ``session.info["table_prefix"]`` set.  Always
            use the session yielded by this context manager, or set
            ``session.info["table_prefix"]`` explicitly on any manually
            created session.

        Args:
            tenant: Currently active tenant.

        Yields:
            Configured :class:`~sqlalchemy.ext.asyncio.AsyncSession` with
            ``session.info["tenant_id"]`` and ``session.info["table_prefix"]``
            populated.

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

        if self.dialect == DbDialect.MSSQL:
            await self._initialize_mssql_schema(tenant, metadata)
        elif supports_native_schemas(self.dialect):
            await self._initialize_schema(tenant, metadata)
        else:
            await self._initialize_prefix(tenant, metadata)

    async def _initialize_mssql_schema(
        self,
        tenant: Tenant,
        metadata: MetaData | None,
    ) -> None:
        """Create a schema on MSSQL and optionally create tables inside it.

        MSSQL uses ``CREATE SCHEMA`` for namespace creation (same DDL syntax as
        PostgreSQL) but ``ALTER USER … WITH DEFAULT_SCHEMA`` is forbidden for
        the ``dbo`` user (error 15150) — the principal that ``sa`` resolves to.

        Tables are created by binding ``metadata`` to a schema-qualified
        ``MetaData`` copy so SQLAlchemy generates ``CREATE TABLE [schema].[table]``
        without requiring any changes to the database user's default schema.

        Args:
            tenant: Target tenant.
            metadata: Optional SQLAlchemy ``MetaData`` for table creation.

        Raises:
            IsolationError: When schema creation fails.
        """
        import sqlalchemy as sa  # noqa: PLC0415

        schema = self._validated_schema_name(tenant)
        assert_safe_schema_name(schema, context=f"initialize_tenant tenant={tenant.id!r}")

        async with self.engine.begin() as conn:
            try:
                # MSSQL does not support IF NOT EXISTS on CREATE SCHEMA —
                # check existence first to make this idempotent.
                result = await conn.execute(
                    text(
                        "SELECT schema_name FROM information_schema.schemata "
                        "WHERE schema_name = :name"
                    ),
                    {"name": schema},
                )
                if result.scalar() is not None:
                    logger.warning("MSSQL schema %r already exists — skipping CREATE", schema)
                else:
                    # CREATE SCHEMA must be the only statement in a batch on MSSQL.
                    await conn.execute(text(f"CREATE SCHEMA [{schema}]"))
                    logger.info("Created MSSQL schema %r for tenant %s", schema, tenant.id)

                if metadata is not None:
                    # Build a schema-qualified MetaData copy so SQLAlchemy
                    # generates "CREATE TABLE [schema].[table]" without
                    # requiring ALTER USER (which is forbidden for dbo).
                    qualified_meta = sa.MetaData(schema=schema)
                    for table in metadata.sorted_tables:
                        table.to_metadata(qualified_meta)
                    await conn.run_sync(qualified_meta.create_all)
                    logger.info("Created tables in MSSQL schema %r", schema)
            except IsolationError:
                raise
            except Exception as exc:
                try:  # noqa: SIM105
                    await conn.rollback()
                except Exception:  # pragma: no cover  # noqa: S110
                    pass
                async with self.engine.connect() as cleanup_conn:
                    try:
                        await cleanup_conn.execute(text(f"DROP SCHEMA [{schema}]"))
                        await cleanup_conn.commit()
                        logger.warning(
                            "Rolled back MSSQL schema %r after failed initialize_tenant "
                            "for tenant %s",
                            schema,
                            tenant.id,
                        )
                    except Exception as cleanup_exc:
                        logger.exception(
                            "MSSQL schema cleanup failed for %r (tenant %s): %s",
                            schema,
                            tenant.id,
                            cleanup_exc,  # noqa: TRY401
                        )
                raise IsolationError(
                    operation="initialize_tenant",
                    tenant_id=tenant.id,
                    details={"schema": schema, "dialect": "mssql", "error": str(exc)},
                ) from exc

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
                    await conn.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
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

        # Build name mapping: old_name -> prefixed_name
        name_map = {t.name: f"{prefix}{t.name}" for t in metadata.tables.values()}
        prefixed_meta = sa.MetaData()

        # Two-pass approach:
        #   Pass 1 — create tables with non-FK columns and non-FK constraints.
        #   Pass 2 — add FK constraints with remapped target table references.
        # This avoids the deprecated Column.copy() while correctly handling
        # cross-table FK references within the same tenant namespace.

        for table in metadata.sorted_tables:
            new_name = name_map[table.name]
            new_cols: list[sa.Column] = []  # type: ignore[type-arg]

            for col in table.columns:
                # Reconstruct the column without foreign keys so that the
                # first pass does not attempt to resolve FK targets (which
                # don't exist yet in prefixed_meta).
                new_col = sa.Column(
                    col.name,
                    col.type,
                    primary_key=col.primary_key,
                    nullable=col.nullable,
                    index=col.index,
                    unique=col.unique,
                    default=col.default,
                    onupdate=col.onupdate,
                    server_default=col.server_default,
                    comment=col.comment,
                )
                new_cols.append(new_col)

            # Include only non-FK constraints (UniqueConstraint, CheckConstraint…)
            # PrimaryKeyConstraint is implied by primary_key=True on the columns.
            non_fk_constraints = [
                c
                for c in table.constraints
                if not isinstance(c, (sa.ForeignKeyConstraint, sa.PrimaryKeyConstraint))
            ]

            sa.Table(
                new_name,
                prefixed_meta,
                *new_cols,
                *non_fk_constraints,
            )

        # Pass 2 — add FK constraints with remapped table names.
        for old_table in metadata.tables.values():
            new_table = prefixed_meta.tables[name_map[old_table.name]]

            for constraint in old_table.constraints:
                if not isinstance(constraint, sa.ForeignKeyConstraint):
                    continue

                local_col_names = [c.name for c in constraint.columns]
                remote_refs: list[str] = []

                for elem in constraint.elements:
                    target_table_name = elem._column_tokens[
                        1
                    ]  # (schema, table, column)  # type: ignore[attr-defined]
                    target_col_name = elem._column_tokens[2]
                    prefixed_target = name_map.get(target_table_name, target_table_name)
                    remote_refs.append(f"{prefixed_target}.{target_col_name}")

                sa.ForeignKeyConstraint(
                    local_col_names,
                    remote_refs,
                    table=new_table,
                )

        try:
            async with self.engine.begin() as conn:
                await conn.run_sync(prefixed_meta.create_all)
                logger.info(
                    "Created %d prefixed tables for tenant %s (prefix=%r)",
                    len(prefixed_meta.sorted_tables),
                    tenant.id,
                    prefix,
                )
        except IsolationError:
            raise
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

        if self.dialect == DbDialect.MSSQL:
            schema = self._validated_schema_name(tenant)
            assert_safe_schema_name(schema, context=f"destroy_tenant tenant={tenant.id!r}")
            async with self.engine.begin() as conn:
                try:
                    # Drop all tables in the schema before dropping the schema
                    # itself.  MSSQL has no CASCADE on DROP SCHEMA.
                    #
                    # QUOTENAME() wraps each identifier in [brackets], providing
                    # defence-in-depth against injection even though
                    # assert_safe_schema_name already validates the schema name.
                    # :schema is passed as a bound parameter to the outer query
                    # (WHERE TABLE_SCHEMA = :schema) — the only place the value
                    # crosses the parameterisation boundary.
                    await conn.execute(
                        text(
                            "DECLARE @sql NVARCHAR(MAX) = N'';"
                            "SELECT @sql = @sql"
                            "  + N'DROP TABLE '"
                            "  + QUOTENAME(TABLE_SCHEMA)"
                            "  + N'.'"
                            "  + QUOTENAME(TABLE_NAME)"
                            "  + N';' "
                            "FROM INFORMATION_SCHEMA.TABLES "
                            "WHERE TABLE_SCHEMA = :schema "
                            "  AND TABLE_TYPE = N'BASE TABLE';"
                            "IF LEN(@sql) > 0 EXEC sp_executesql @sql;"
                        ),
                        {"schema": schema},
                    )
                    await conn.execute(text(f"DROP SCHEMA [{schema}]"))
                    logger.warning("Destroyed MSSQL schema %r for tenant %s", schema, tenant.id)
                except Exception as exc:
                    raise IsolationError(
                        operation="destroy_tenant",
                        tenant_id=tenant.id,
                        details={"schema": schema, "dialect": "mssql", "error": str(exc)},
                    ) from exc
        elif supports_native_schemas(self.dialect):
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
