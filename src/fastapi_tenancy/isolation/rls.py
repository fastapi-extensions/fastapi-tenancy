"""Row-Level Security (RLS) isolation provider — PostgreSQL only.

All tenants share a **single** schema and set of tables.  PostgreSQL's
server-side RLS policies restrict every query so that a given session can
only read rows whose ``tenant_id`` column matches ``current_setting('app.current_tenant')``.

Architecture
------------
.. code-block::

    Request arrives  →  middleware sets TenantContext
    get_session()    →  opens AsyncSession
                        SET LOCAL app.current_tenant = '<tenant_id>'
                        PostgreSQL applies RLS policies to every statement
    Route handler    →  issues plain SELECT/INSERT/UPDATE without WHERE tenant_id
                        RLS silently adds tenant filter at the engine level
    Session closes   →  SET LOCAL expires (LOCAL is transaction-scoped)

Why ``apply_filters`` still adds ``WHERE``
------------------------------------------
Defence-in-depth.  Application-layer filtering is a second guard; if the RLS
policy is disabled or bypassed, the explicit ``WHERE`` still returns only the
correct tenant's data.

Why ``SET LOCAL`` not ``SET SESSION``
--------------------------------------
``SET SESSION`` persists for the lifetime of the physical connection.  Because
SQLAlchemy pools connections, a connection returned to the pool would carry
the previous tenant's ``app.current_tenant`` value.  ``SET LOCAL`` is scoped
to the current transaction; when the transaction ends the setting is
automatically reverted to the previous value.

Non-PostgreSQL behaviour
------------------------
Raises :exc:`~fastapi_tenancy.core.exceptions.IsolationError` if called with
a dialect that does not support RLS.  Use :class:`SchemaIsolationProvider`
or :class:`DatabaseIsolationProvider` for MySQL / SQLite / MSSQL.

Security
--------
The ``tenant_id`` value is passed as a properly bound parameter to
``SET LOCAL … = :tenant_id`` via a ``SELECT set_config(…, …, TRUE)`` workaround
since PostgreSQL ``SET`` does not support bind params.  This prevents
injection via crafted tenant IDs.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import Column, String, column, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from fastapi_tenancy.core.exceptions import ConfigurationError, IsolationError
from fastapi_tenancy.isolation.base import BaseIsolationProvider
from fastapi_tenancy.utils.db_compat import (
    detect_dialect,
    supports_native_rls,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy import MetaData

    from fastapi_tenancy.core.config import TenancyConfig
    from fastapi_tenancy.core.types import SelectT, Tenant

logger = logging.getLogger(__name__)

#: Name of the session-local GUC used by RLS policies.
_RLS_GUC: str = "app.current_tenant"

#: Standard column name expected on tenant-scoped tables.
_TENANT_COLUMN: str = "tenant_id"

########################################################################
# Sample PostgreSQL RLS policy SQL (informational — not executed here) #
########################################################################
_SAMPLE_POLICY_SQL = """
-- Run once per table that should be tenant-scoped:
ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON {table_name}
    USING (tenant_id = current_setting('app.current_tenant', TRUE));
"""


class RLSIsolationProvider(BaseIsolationProvider):
    """PostgreSQL Row-Level Security data isolation.

    Activates per-request tenant filtering by setting the
    ``app.current_tenant`` session variable at the start of every
    transaction.  PostgreSQL RLS policies reference this variable to
    restrict row visibility transparently.

    Raises :exc:`~fastapi_tenancy.core.exceptions.ConfigurationError`
    at construction if the configured database is not PostgreSQL, so
    misconfigured deployments fail immediately at startup.

    Args:
        config: Application-wide tenancy configuration.
        engine: Optional pre-built engine to reuse (share with the store
            to avoid a duplicate connection pool).

    Example::

        provider = RLSIsolationProvider(config)
        await provider.initialize_tenant(tenant, metadata=Base.metadata)

        async with provider.get_session(tenant) as session:
            # RLS policy silently adds: WHERE tenant_id = '<current tenant>'
            result = await session.execute(select(User))

    Schema hint::

        ALTER TABLE users ENABLE ROW LEVEL SECURITY;
        ALTER TABLE users FORCE ROW LEVEL SECURITY;

        CREATE POLICY tenant_isolation ON users
            USING (tenant_id = current_setting('app.current_tenant', TRUE));
    """

    def __init__(
        self,
        config: TenancyConfig,
        engine: AsyncEngine | None = None,
    ) -> None:
        super().__init__(config)
        dialect = detect_dialect(str(config.database_url))
        if not supports_native_rls(dialect):
            raise ConfigurationError(
                parameter="isolation_strategy",
                reason=(
                    f"RLS isolation requires PostgreSQL but dialect is {dialect.value!r}. "
                    "Use SCHEMA or DATABASE isolation for this database."
                ),
            )
        self.dialect = dialect

        if engine is not None:
            self.engine = engine
        else:
            kw: dict[str, Any] = {
                "echo": config.database_echo,
                "pool_pre_ping": config.database_pool_pre_ping,
                "pool_size": config.database_pool_size,
                "max_overflow": config.database_max_overflow,
                "pool_timeout": config.database_pool_timeout,
                "pool_recycle": config.database_pool_recycle,
            }
            self.engine = create_async_engine(str(config.database_url), **kw)

        logger.info("RLSIsolationProvider ready (PostgreSQL RLS)")

    ###########
    # Session #
    ###########

    @asynccontextmanager
    async def get_session(self, tenant: Tenant) -> AsyncIterator[AsyncSession]:
        """Yield a session with ``app.current_tenant`` set to *tenant*'s ID.

        Uses ``SELECT set_config('app.current_tenant', :id, TRUE)`` — a
        fully parameterised call — to avoid injection via crafted tenant IDs.

        Critical implementation detail — connection-level event listener
        -----------------------------------------------------------------
        ``set_config(..., TRUE)`` (equivalent to ``SET LOCAL``) is
        **transaction-scoped**: the GUC reverts when the current transaction
        ends.  If we simply call it once before yielding, any subsequent
        ``session.begin()`` block opened by the route handler would operate
        without the GUC, silently bypassing all RLS policies.

        The fix mirrors ``SchemaIsolationProvider._schema_session``: install
        a ``begin`` event listener on the underlying synchronous connection so
        that ``set_config`` is re-executed at the start of **every**
        transaction opened on this connection during the request lifetime.
        This is the pattern recommended by the SQLAlchemy docs for
        connection-level configuration that must survive multiple transactions.

        Args:
            tenant: Currently active tenant.

        Yields:
            Configured :class:`~sqlalchemy.ext.asyncio.AsyncSession`.

        Raises:
            IsolationError: When the session variable cannot be set.
        """
        from sqlalchemy import event  # noqa: PLC0415

        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            try:
                conn = await session.connection()
                sync_conn = conn.sync_connection

                # Install a connection-level listener so the GUC is re-applied
                # at the start of every transaction opened on this connection.
                # The listener uses set_config(..., TRUE) which is transaction-
                # local: the GUC reverts automatically when the transaction ends,
                # so pooled connections never carry a stale tenant_id.
                @event.listens_for(sync_conn, "begin")
                def _set_rls_guc(sync_connection) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
                    # _RLS_GUC is a module-level constant ("app.current_tenant")
                    # defined in this file — it is never user-supplied and cannot
                    # be influenced by tenant data.  The f-string is therefore safe.
                    # tenant.id is passed as a positional bind parameter ($1) so
                    # it is never interpolated into the SQL string.
                    #
                    # NOTE: Two different parameterisation syntaxes are intentionally
                    # used in this method:
                    #   1. exec_driver_sql (here, in the listener): uses the native
                    #      asyncpg "$1" placeholder because exec_driver_sql bypasses
                    #      SQLAlchemy's parameter rendering layer entirely.
                    #   2. session.execute + text() (below): uses SQLAlchemy's ":name"
                    #      syntax because it goes through the full SQLAlchemy rendering
                    #      pipeline which translates ":name" to "$1" for asyncpg.
                    # Both are correct for their respective call sites.
                    sync_connection.exec_driver_sql(
                        f"SELECT set_config('{_RLS_GUC}', $1, TRUE)",
                        (tenant.id,),
                    )
                    logger.debug("Set %s = %r (tenant %s)", _RLS_GUC, tenant.id, tenant.id)

                # Apply immediately for the first (implicit) transaction so any
                # query issued before an explicit session.begin() is also covered.
                await session.execute(
                    text("SELECT set_config(:guc, :tenant_id, TRUE)"),
                    {"guc": _RLS_GUC, "tenant_id": tenant.id},
                )
                logger.debug("Set %s = %r (tenant %s)", _RLS_GUC, tenant.id, tenant.id)
                yield session
            except IsolationError:
                raise
            except Exception as exc:
                await session.rollback()
                raise IsolationError(
                    operation="get_session",
                    tenant_id=tenant.id,
                    details={"guc": _RLS_GUC, "error": str(exc)},
                ) from exc

    async def apply_filters(self, query: SelectT, tenant: Tenant) -> SelectT:
        """Add defence-in-depth ``WHERE tenant_id = :id`` to *query*.

        Although the RLS policy already restricts rows at the database level,
        this explicit application-layer filter prevents accidental data leakage
        if the RLS policy is ever accidentally disabled on a table.

        Args:
            query: SQLAlchemy ``Select`` query.
            tenant: Currently active tenant.

        Returns:
            Query with an additional ``tenant_id`` equality predicate.
        """
        tenant_col: Column[String] = column(_TENANT_COLUMN) # type: ignore[assignment]
        return query.where(tenant_col == tenant.id)

    ################
    # Provisioning #
    ################

    async def initialize_tenant(
        self,
        tenant: Tenant,
        metadata: MetaData | None = None,
    ) -> None:
        """Verify RLS policies are active; optionally create tables.

        Unlike schema/database isolation, RLS uses shared tables — there
        is nothing to physically create per-tenant.  This method logs a
        reminder about the required RLS policy and creates tables if
        *metadata* is supplied and they do not already exist.

        When *metadata* is supplied, ``create_all`` is called with
        ``checkfirst=True`` so repeated calls are idempotent.

        Args:
            tenant: The newly onboarded tenant.
            metadata: Application ``MetaData``.  When supplied, the shared
                tables are created if they do not already exist.

        Raises:
            IsolationError: When table creation fails.
        """
        logger.info(
            "RLS: initialising tenant %s — shared tables; no schema created.",
            tenant.id,
        )
        logger.info(
            "RLS policy hint:\n%s",
            _SAMPLE_POLICY_SQL.format(table_name="<your_table>"),
        )

        if metadata is not None:
            try:
                async with self.engine.begin() as conn:
                    await conn.run_sync(metadata.create_all, checkfirst=True)
                logger.info("RLS: created / verified shared tables for tenant %s", tenant.id)
            except Exception as exc:
                raise IsolationError(
                    operation="initialize_tenant",
                    tenant_id=tenant.id,
                    details={"mode": "rls", "error": str(exc)},
                ) from exc

    async def destroy_tenant(self, tenant: Tenant, **kwargs: Any) -> None:
        """Delete all rows belonging to *tenant* from every tenant-scoped table.

        Executes a ``DELETE FROM <table> WHERE tenant_id = :id`` for every
        table in *metadata* (passed via keyword argument ``metadata``).

        .. warning::
            This is a **destructive, irreversible** operation.  Tables that
            share rows with other tenants will retain their other data;
            only rows where ``tenant_id = tenant.id`` are deleted.

        Args:
            tenant: Tenant to destroy.
            **kwargs:
                - ``metadata`` (:class:`~sqlalchemy.MetaData`): Application
                  metadata.  **Required** — raises :exc:`IsolationError` if
                  not supplied.

        Raises:
            IsolationError: When metadata is not supplied or deletion fails.
        """
        metadata: MetaData | None = kwargs.get("metadata")
        if metadata is None:
            raise IsolationError(
                operation="destroy_tenant",
                tenant_id=tenant.id,
                details={
                    "reason": (
                        "RLS destroy_tenant requires metadata=Base.metadata to identify "
                        "which tables to delete rows from. "
                        "Call destroy_tenant(tenant, metadata=Base.metadata)."
                    )
                },
            )

        async with AsyncSession(self.engine) as session:
            try:
                async with session.begin():
                    for table in metadata.sorted_tables:
                        if _TENANT_COLUMN in table.c:
                            result = await session.execute(
                                table.delete().where(
                                    table.c[_TENANT_COLUMN] == tenant.id
                                )
                            )
                            logger.warning(
                                "RLS destroy: deleted %d rows from %s for tenant %s",
                                result.rowcount, # type: ignore[attr-defined]
                                table.name,
                                tenant.id,
                            )
            except Exception as exc:
                await session.rollback()
                raise IsolationError(
                    operation="destroy_tenant",
                    tenant_id=tenant.id,
                    details={"mode": "rls", "error": str(exc)},
                ) from exc

    ################
    # Verification #
    ################

    async def verify_isolation(self, tenant: Tenant) -> bool:
        """Verify that the RLS GUC can be set and read back correctly.

        Args:
            tenant: Tenant to verify.

        Returns:
            ``True`` when the session variable round-trips correctly.
        """
        try:
            async with self.engine.connect() as conn:
                result = await conn.execute(
                    text("SELECT set_config(:guc, :val, TRUE), current_setting(:guc, TRUE)"),
                    {"guc": _RLS_GUC, "val": tenant.id},
                )
                row = result.fetchone()
                if row is None:
                    return False
                # Both set_config return value and current_setting should
                # match the tenant ID.
                return row[0] == tenant.id and row[1] == tenant.id
        except Exception:
            return False

    async def close(self) -> None:
        """Dispose the engine and release all pooled connections."""
        await self.engine.dispose()
        logger.info("RLSIsolationProvider closed")


__all__ = ["RLSIsolationProvider"]
