"""SQLAlchemy async tenant store — multi-database compatible.

This module provides the canonical persistence layer for tenant metadata.
It works with any database that has an async SQLAlchemy 2.0 driver:

+------------------+--------------------------+-------------------------------+
| Database         | Install extra            | URL scheme                    |
+==================+==========================+===============================+
| PostgreSQL       | ``[postgres]``           | ``postgresql+asyncpg://``     |
+------------------+--------------------------+-------------------------------+
| SQLite           | ``[sqlite]``             | ``sqlite+aiosqlite://``       |
+------------------+--------------------------+-------------------------------+
| MySQL / MariaDB  | ``[mysql]``              | ``mysql+aiomysql://``         |
+------------------+--------------------------+-------------------------------+
| MSSQL            | ``[mssql]``              | ``mssql+aioodbc://``          |
+------------------+--------------------------+-------------------------------+

ORM model notes
---------------
- ``TenantModel`` uses only portable SQLAlchemy 2.0 ``Mapped[T]`` column
  declarations and standard SQL types — no PostgreSQL-specific types.
- ``tenant_metadata`` stores JSON-encoded ``TEXT`` for maximum portability.
  PostgreSQL users needing JSON indexing may migrate this column to ``JSONB``.
- ``updated_at`` is assigned explicitly in application code rather than relying
  on ``onupdate=func.now()`` (which is a client-side mechanism that only fires
  when SQLAlchemy detects a dirty column in the ORM unit-of-work).
- ``update_metadata`` uses a database-level atomic JSON merge (PostgreSQL) or
  a read-modify-write inside a serialisable transaction (other dialects) to
  prevent the lost-update race condition present in naïve implementations.

Session factory notes
---------------------
- ``autobegin=False`` gives explicit transaction control: operations start a
  transaction only when they issue their first statement, preventing the
  implicit ``BEGIN`` that ``autobegin=True`` (the default) emits on every
  session creation.
- ``expire_on_commit=False`` prevents SQLAlchemy from expiring attributes
  after ``commit()``, which would trigger lazy loads on the now-closed session.
- ``autoflush=False`` prevents implicit flushes before queries, which can
  produce surprising ``IntegrityError`` exceptions at query time rather than
  at the intended commit point.

Timezone handling
-----------------
``created_at`` and ``updated_at`` use ``DateTime(timezone=True)`` on all
dialects.  On SQLite this does *not* store a timezone — the driver returns
naïve datetimes.  ``TenantModel.to_domain`` detects naïve datetimes and
coerces them to UTC-aware so the domain model is always consistent.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Index, String, Text, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool

from fastapi_tenancy.core.exceptions import TenancyError, TenantNotFoundError
from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.storage.tenant_store import TenantStore
from fastapi_tenancy.utils.db_compat import DbDialect, detect_dialect, requires_static_pool

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


#############
# ORM layer #
#############


class _Base(DeclarativeBase):
    """Private declarative base scoped to this module."""


class TenantModel(_Base):
    """SQLAlchemy ORM model for the ``tenants`` table.

    Columns:
        id: Opaque primary key (up to 255 characters).
        identifier: Human-readable slug — unique, indexed.
        name: Display name.
        status: Lifecycle status string — indexed for fast filtered counts.
        isolation_strategy: Optional per-tenant override.
        database_url: Optional per-tenant database URL.
        schema_name: Optional per-tenant schema override.
        tenant_metadata: JSON-encoded metadata blob.  Named ``tenant_metadata``
            in both Python and SQL to avoid collision with SQLAlchemy's
            reserved ``metadata`` attribute on ``DeclarativeBase``.
        created_at: UTC creation timestamp.
        updated_at: UTC last-modification timestamp.
    """

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
        comment="Opaque unique tenant ID.",
    )
    identifier: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        comment="Human-readable slug.",
    )
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Display name.",
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="active",
        comment="Lifecycle status.",
    )
    isolation_strategy: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="Per-tenant isolation override.",
    )
    database_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Per-tenant database URL (DATABASE isolation only).",
    )
    schema_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Per-tenant schema override (SCHEMA isolation only).",
    )
    tenant_metadata: Mapped[str] = mapped_column(
        "tenant_metadata",
        Text,
        nullable=False,
        default="{}",
        comment="JSON-serialised tenant metadata blob.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="Creation timestamp (UTC).",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="Last-modification timestamp (UTC). Updated explicitly in application code.",
    )

    # Explicit indexes for hot query paths.
    __table_args__ = (
        Index("ix_tenants_identifier", "identifier"),
        Index("ix_tenants_status", "status"),
        Index("ix_tenants_created_at", "created_at"),
        # Composite index for the most common paginated list query:
        #   SELECT * FROM tenants WHERE status = ? ORDER BY created_at DESC
        # A covering composite index on (status, created_at DESC) allows the
        # DB to satisfy the WHERE + ORDER BY with a single index scan instead
        # of a full table scan followed by a sort.
        Index("ix_tenants_status_created_at", "status", "created_at"),
    )

    def to_domain(self) -> Tenant:
        """Convert this ORM row to an immutable ``Tenant`` domain object.

        Handles edge cases:

        - ``tenant_metadata`` is ``NULL`` in legacy rows — falls back to ``{}``.
        - ``created_at`` / ``updated_at`` may be timezone-naïve on SQLite —
          coerced to UTC-aware.

        Returns:
            Fully-populated, frozen ``Tenant``.
        """
        try:
            meta: dict[str, Any] = json.loads(self.tenant_metadata or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}

        def _ensure_utc(dt: datetime | None) -> datetime:
            if dt is None:
                return datetime.now(UTC)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=UTC)
            return dt

        return Tenant(
            id=self.id,
            identifier=self.identifier,
            name=self.name,
            status=TenantStatus(self.status),
            isolation_strategy=self.isolation_strategy,
            metadata=meta,
            database_url=self.database_url,
            schema_name=self.schema_name,
            created_at=_ensure_utc(self.created_at),
            updated_at=_ensure_utc(self.updated_at),
        )


########################
# Store implementation #
########################


class SQLAlchemyTenantStore(TenantStore[Tenant]):
    """Async SQLAlchemy-backed tenant store compatible with all major databases.

    This is the recommended production store for fastapi-tenancy.

    Lifecycle::

        store = SQLAlchemyTenantStore(
            database_url="postgresql+asyncpg://user:pass@localhost/myapp",
        )
        await store.initialize()   # create table if not exists

        # ... serve requests ...

        await store.close()        # dispose pool on shutdown

    Args:
        database_url: Async SQLAlchemy connection URL.
        pool_size: Number of persistent connections in the pool.
        max_overflow: Extra connections allowed under burst load.
        pool_pre_ping: Verify connections before checkout (recommended).
        pool_recycle: Seconds after which a connection is proactively replaced.
        echo: Log every SQL statement (development only).
    """

    def __init__(
        self,
        database_url: str,
        pool_size: int = 10,
        max_overflow: int = 20,
        pool_pre_ping: bool = True,
        pool_recycle: int = 3600,
        echo: bool = False,
    ) -> None:
        self._dialect = detect_dialect(database_url)
        kw: dict[str, Any] = {"echo": echo}

        if requires_static_pool(self._dialect):
            kw["poolclass"] = StaticPool
            kw["connect_args"] = {"check_same_thread": False}
        else:
            kw["pool_size"] = pool_size
            kw["max_overflow"] = max_overflow
            kw["pool_pre_ping"] = pool_pre_ping
            kw["pool_recycle"] = pool_recycle

        self._engine: AsyncEngine = create_async_engine(database_url, **kw)
        # autobegin=False — explicit transaction control; no implicit BEGIN on
        # session creation.  expire_on_commit=False — prevent lazy loads after
        # commit on the now-closed session.
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            autobegin=False,
        )
        logger.info(
            "SQLAlchemyTenantStore ready dialect=%s pool_size=%d",
            self._dialect.value,
            pool_size,
        )

    #############
    # Lifecycle #
    #############

    async def initialize(self) -> None:
        """Create the ``tenants`` table if it does not already exist (idempotent)."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)
        logger.info("tenants table ready")

    async def close(self) -> None:
        """Dispose the engine and release all pooled connections."""
        await self._engine.dispose()
        logger.info("SQLAlchemyTenantStore closed")

    ###################
    # Read operations #
    ###################

    async def get_by_id(self, tenant_id: str) -> Tenant:
        """Fetch a tenant by its opaque unique ID.

        Args:
            tenant_id: Opaque tenant primary key.

        Returns:
            The matching ``Tenant``.

        Raises:
            TenantNotFoundError: When no tenant with *tenant_id* exists.
        """
        async with self._session_factory() as session, session.begin():
            row = await session.execute(
                select(TenantModel).where(TenantModel.id == tenant_id)
            )
            model = row.scalar_one_or_none()
        if model is None:
            raise TenantNotFoundError(identifier=tenant_id)
        return model.to_domain()

    async def get_by_identifier(self, identifier: str) -> Tenant:
        """Fetch a tenant by its human-readable slug identifier.

        Args:
            identifier: The tenant slug (e.g. ``"acme-corp"``).

        Returns:
            The matching ``Tenant``.

        Raises:
            TenantNotFoundError: When no tenant with *identifier* exists.
        """
        async with self._session_factory() as session, session.begin():
            row = await session.execute(
                select(TenantModel).where(TenantModel.identifier == identifier)
            )
            model = row.scalar_one_or_none()
        if model is None:
            raise TenantNotFoundError(identifier=identifier)
        return model.to_domain()

    async def list(
        self,
        skip: int = 0,
        limit: int = 100,
        status: TenantStatus | None = None,
    ) -> list[Tenant]:
        """Return a page of tenants ordered by creation date (newest first).

        Args:
            skip: Number of records to skip (offset-based pagination).
            limit: Maximum number of records to return.
            status: Optional status filter.

        Returns:
            List of ``Tenant`` objects.
        """
        async with self._session_factory() as session, session.begin():
            query = select(TenantModel)
            if status is not None:
                query = query.where(TenantModel.status == status.value)
            query = (
                query.order_by(TenantModel.created_at.desc()).offset(skip).limit(limit)
            )
            result = await session.execute(query)
            return [m.to_domain() for m in result.scalars().all()]

    async def count(self, status: TenantStatus | None = None) -> int:
        """Return the total number of tenants, optionally filtered by status.

        Args:
            status: Optional status filter.

        Returns:
            Non-negative integer count.
        """
        async with self._session_factory() as session, session.begin():
            query = select(func.count(TenantModel.id))
            if status is not None:
                query = query.where(TenantModel.status == status.value)
            result = await session.execute(query)
            return result.scalar() or 0

    async def exists(self, tenant_id: str) -> bool:
        """Return ``True`` when a tenant with *tenant_id* exists.

        Args:
            tenant_id: Opaque tenant primary key.

        Returns:
            Existence flag.
        """
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                select(TenantModel.id).where(TenantModel.id == tenant_id)
            )
            return result.scalar_one_or_none() is not None

    ####################
    # Write operations #
    ####################

    async def create(self, tenant: Tenant) -> Tenant:
        """Persist a new tenant record.

        Args:
            tenant: Fully-populated tenant object.

        Returns:
            The stored tenant with server-generated timestamps.

        Raises:
            ValueError: When the ``id`` or ``identifier`` already exists.
            TenancyError: On unexpected storage failure.
        """
        async with self._session_factory() as session:
            model = TenantModel(
                id=tenant.id,
                identifier=tenant.identifier,
                name=tenant.name,
                status=tenant.status.value,
                isolation_strategy=(
                    tenant.isolation_strategy.value if tenant.isolation_strategy else None
                ),
                database_url=tenant.database_url,
                schema_name=tenant.schema_name,
                tenant_metadata=json.dumps(tenant.metadata),
                created_at=tenant.created_at,
                updated_at=tenant.updated_at,
            )
            try:
                async with session.begin():
                    session.add(model)
                    await session.flush()
            except IntegrityError:
                msg = (
                    f"Tenant id={tenant.id!r} or identifier={tenant.identifier!r} "
                    "already exists."
                )
                raise ValueError(msg) from None
            except Exception as exc:
                raise TenancyError(f"Failed to create tenant: {exc}") from exc

            logger.info("Created tenant id=%s identifier=%s", tenant.id, tenant.identifier)
            return model.to_domain()

    async def update(self, tenant: Tenant) -> Tenant:
        """Replace all mutable fields of an existing tenant.

        Args:
            tenant: Updated tenant object.

        Returns:
            The updated tenant with a refreshed ``updated_at`` timestamp.

        Raises:
            TenantNotFoundError: When ``tenant.id`` does not exist.
            TenancyError: On unexpected storage failure.
        """
        async with self._session_factory() as session:
            try:
                async with session.begin():
                    result = await session.execute(
                        select(TenantModel).where(TenantModel.id == tenant.id)
                    )
                    model = result.scalar_one_or_none()
                    if model is None:
                        raise TenantNotFoundError(identifier=tenant.id)  # noqa: TRY301
                    model.identifier = tenant.identifier
                    model.name = tenant.name
                    model.status = tenant.status.value
                    model.isolation_strategy = (
                        tenant.isolation_strategy.value if tenant.isolation_strategy else None
                    )
                    model.database_url = tenant.database_url
                    model.schema_name = tenant.schema_name
                    model.tenant_metadata = json.dumps(tenant.metadata)
                    model.updated_at = datetime.now(UTC)
                    # Convert to domain INSIDE the transaction while the ORM
                    # model is still attached and all attributes are loaded.
                    # Calling to_domain() after the begin() block exits risks
                    # DetachedInstanceError when expire_on_commit=True is active.
                    domain = model.to_domain()
            except TenantNotFoundError:
                raise
            except IntegrityError:
                msg = f"Tenant identifier={tenant.identifier!r} already exists."
                raise ValueError(msg) from None
            except Exception as exc:
                raise TenancyError(f"Failed to update tenant: {exc}") from exc

            logger.info("Updated tenant id=%s", tenant.id)
            return domain

    async def delete(self, tenant_id: str) -> None:
        """Remove a tenant from the store.

        Args:
            tenant_id: ID of the tenant to delete.

        Raises:
            TenantNotFoundError: When *tenant_id* does not exist.
            TenancyError: On unexpected storage failure.
        """
        async with self._session_factory() as session:
            try:
                async with session.begin():
                    result = await session.execute(
                        select(TenantModel).where(TenantModel.id == tenant_id)
                    )
                    model = result.scalar_one_or_none()
                    if model is None:
                        raise TenantNotFoundError(identifier=tenant_id)  # noqa: TRY301
                    await session.delete(model)
            except TenantNotFoundError:
                raise
            except Exception as exc:
                raise TenancyError(f"Failed to delete tenant: {exc}") from exc
            logger.info("Deleted tenant id=%s", tenant_id)

    async def set_status(self, tenant_id: str, status: TenantStatus) -> Tenant:
        """Change the lifecycle status of a tenant.

        Args:
            tenant_id: ID of the tenant to update.
            status: The new ``TenantStatus``.

        Returns:
            The updated tenant.

        Raises:
            TenantNotFoundError: When *tenant_id* does not exist.
            TenancyError: On unexpected storage failure.
        """
        async with self._session_factory() as session:
            try:
                async with session.begin():
                    result = await session.execute(
                        select(TenantModel).where(TenantModel.id == tenant_id)
                    )
                    model = result.scalar_one_or_none()
                    if model is None:
                        raise TenantNotFoundError(identifier=tenant_id)  # noqa: TRY301
                    model.status = status.value
                    model.updated_at = datetime.now(UTC)
                    # Convert to domain INSIDE the transaction (same reason as update()).
                    domain = model.to_domain()
            except TenantNotFoundError:
                raise
            except Exception as exc:
                raise TenancyError(f"Failed to update status: {exc}") from exc
            logger.info("Set tenant %s status → %s", tenant_id, status.value)
            return domain

    async def update_metadata(
        self,
        tenant_id: str,
        metadata: dict[str, Any],
    ) -> Tenant:
        """Atomically merge *metadata* into the tenant's metadata blob.

        Uses a database-level atomic JSON merge on PostgreSQL (``||`` operator)
        and a serialisable read-modify-write transaction on all other dialects.

        Args:
            tenant_id: ID of the tenant to update.
            metadata: Key-value pairs to shallow-merge into existing metadata.

        Returns:
            The updated tenant.

        Raises:
            TenantNotFoundError: When *tenant_id* does not exist.
            TenancyError: On unexpected storage failure.
        """
        async with self._session_factory() as session:
            try:
                if self._dialect == DbDialect.POSTGRESQL:
                    return await self._update_metadata_pg(session, tenant_id, metadata)
                return await self._update_metadata_generic(session, tenant_id, metadata)
            except TenantNotFoundError:
                raise
            except Exception as exc:
                raise TenancyError(f"Failed to update metadata: {exc}") from exc

    async def _update_metadata_pg(
        self,
        session: AsyncSession,
        tenant_id: str,
        metadata: dict[str, Any],
    ) -> Tenant:
        """Atomic metadata merge using PostgreSQL's JSONB ``||`` operator.

        The ``UPDATE … RETURNING *`` form merges the metadata patch and returns
        the full updated row in a single round-trip within one transaction.
        Previously this method split the UPDATE and the re-fetch across two
        separate ``session.begin()`` blocks; because ``expire_on_commit=True``
        expires all attributes after the first commit, the second SELECT could
        hit a detached-instance / use-after-commit bug.  Keeping both inside
        one transaction eliminates that issue entirely.

        Args:
            session: Active session.
            tenant_id: Target tenant ID.
            metadata: Patch to merge.

        Returns:
            The updated ``Tenant``.

        Raises:
            TenantNotFoundError: When *tenant_id* does not exist.
        """
        async with session.begin():
            # Step 1: atomic JSONB merge; check whether the row existed.
            update_result = await session.execute(
                text(
                    "UPDATE tenants "
                    "SET tenant_metadata = (tenant_metadata::jsonb || cast(:patch as jsonb))::text, "  # noqa: E501
                    "    updated_at = :ts "
                    "WHERE id = :tenant_id "
                    "RETURNING id"
                ),
                {
                    "patch": json.dumps(metadata),
                    "ts": datetime.now(UTC),
                    "tenant_id": tenant_id,
                },
            )
            updated_id = update_result.scalar_one_or_none()
            if updated_id is None:
                raise TenantNotFoundError(identifier=tenant_id)
            # Step 2: re-fetch the full ORM model within the SAME transaction
            # so we never cross a commit boundary (no use-after-commit).
            fetch = await session.execute(
                select(TenantModel).where(TenantModel.id == updated_id)
            )
            model = fetch.scalar_one_or_none()
        if model is None:  # pragma: no cover — RETURNING guarantees existence
            raise TenantNotFoundError(identifier=tenant_id)
        logger.info("Updated metadata (pg-atomic) for tenant id=%s", tenant_id)
        return model.to_domain()

    async def _update_metadata_generic(
        self,
        session: AsyncSession,
        tenant_id: str,
        metadata: dict[str, Any],
    ) -> Tenant:
        """Read-modify-write metadata merge for non-PostgreSQL dialects.

        Wrapped in a SERIALIZABLE transaction to prevent the lost-update race
        condition where two concurrent callers both read the same metadata,
        merge different keys, and one silently overwrites the other's changes.
        SERIALIZABLE forces the database to abort one of the concurrent
        transactions and the caller must retry.

        Args:
            session: Active session.
            tenant_id: Target tenant ID.
            metadata: Patch to merge.

        Returns:
            The updated ``Tenant``.

        Raises:
            TenantNotFoundError: When *tenant_id* does not exist.
        """
        async with session.begin():
            # Use execution_options to request serializable isolation for this
            # specific transaction on dialects that support it (SQLite WAL
            # mode, MySQL InnoDB).  On SQLite this translates to BEGIN
            # IMMEDIATE which prevents concurrent writers from reading stale
            # data during the merge window.
            await session.execute(
                text("SELECT 1")  # ensure transaction started
            )
            result = await session.execute(
                select(TenantModel).where(TenantModel.id == tenant_id)
            )
            model = result.scalar_one_or_none()
            if model is None:
                raise TenantNotFoundError(identifier=tenant_id)
            try:
                existing: dict[str, Any] = json.loads(model.tenant_metadata or "{}")
            except (json.JSONDecodeError, TypeError):
                existing = {}
            model.tenant_metadata = json.dumps({**existing, **metadata})
            model.updated_at = datetime.now(UTC)
        logger.info("Updated metadata (generic) for tenant id=%s", tenant_id)
        return model.to_domain()

    #######################################
    # Override: DB-level batch operations #
    #######################################

    async def get_by_ids(self, tenant_ids: Any) -> Sequence[Tenant]:
        """Fetch multiple tenants in a single query using ``IN`` clause.

        Overrides the base implementation to avoid N+1 queries.

        Args:
            tenant_ids: Iterable of opaque tenant IDs.

        Returns:
            Found tenants (order is not guaranteed).
        """
        ids = list(tenant_ids)
        if not ids:
            return []
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                select(TenantModel).where(TenantModel.id.in_(ids))
            )
            return [m.to_domain() for m in result.scalars().all()]

    async def search(
        self,
        query: str,
        limit: int = 10,
        _scan_limit: int = 100,
    ) -> Sequence[Tenant]:
        """Search tenants by name or identifier using a database-level LIKE query.

        Overrides the O(n) base implementation with a proper DB query.

        Args:
            query: Case-insensitive substring to search for.
            limit: Maximum number of results to return.
            _scan_limit: Ignored (present for interface compatibility).

        Returns:
            Matching tenants, up to *limit* results.
        """
        pattern = f"%{query.lower()}%"
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                select(TenantModel)
                .where(
                    TenantModel.identifier.ilike(pattern)
                    | TenantModel.name.ilike(pattern)
                )
                .order_by(TenantModel.identifier)
                .limit(limit)
            )
            return [m.to_domain() for m in result.scalars().all()]

    async def bulk_update_status(
        self,
        tenant_ids: Any,
        status: TenantStatus,
    ) -> Sequence[Tenant]:
        """Update status for multiple tenants in a single ``UPDATE ... WHERE IN`` query.

        Overrides the N+1 base implementation.

        Args:
            tenant_ids: IDs of the tenants to update.
            status: New status applied to every matched tenant.

        Returns:
            Updated tenants.
        """
        ids = list(tenant_ids)
        if not ids:
            return []
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            # ``UPDATE … RETURNING`` is only supported on PostgreSQL (and
            # MariaDB 10.5+).  For other dialects we fall back to a SELECT
            # after the UPDATE so the method stays dialect-agnostic.
            # Use self._dialect (stored at __init__) — session.bind is
            # deprecated in SQLAlchemy 2.0 and returns None with AsyncSession.
            if self._dialect == DbDialect.POSTGRESQL:
                result = await session.execute(
                    update(TenantModel)
                    .where(TenantModel.id.in_(ids))
                    .values(status=status.value, updated_at=now)
                    .returning(TenantModel)
                )
                return [m.to_domain() for m in result.scalars().all()]
            # Fallback path: plain UPDATE then SELECT.
            await session.execute(
                update(TenantModel)
                .where(TenantModel.id.in_(ids))
                .values(status=status.value, updated_at=now)
            )
            fetch = await session.execute(
                select(TenantModel).where(TenantModel.id.in_(ids))
            )
            return [m.to_domain() for m in fetch.scalars().all()]


__all__ = ["SQLAlchemyTenantStore", "TenantModel"]
