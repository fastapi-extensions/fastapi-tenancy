"""SQLAlchemy async tenant store — multi-database compatible.

This module provides the canonical persistence layer for tenant metadata.
It works with any database that has an async SQLAlchemy driver:

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

The ORM model (:class:`TenantModel`) uses only portable SQLAlchemy 2.0
``Mapped[T]`` column declarations and standard SQL types — no
PostgreSQL-specific types — so the same schema definition works across all
supported dialects.

Connection-pool behaviour
-------------------------
* **SQLite** — ``StaticPool`` with ``check_same_thread=False``.  All
  in-memory SQLite operations share a single connection, which is mandatory
  for databases that must persist across multiple async tasks.
* **All other dialects** — ``QueuePool`` with configurable ``pool_size``,
  ``max_overflow``, and ``pool_recycle``.  ``pool_pre_ping=True`` is always
  on so stale connections are detected and replaced transparently.

Timezone handling
-----------------
``created_at`` and ``updated_at`` columns use ``DateTime(timezone=True)``
on all dialects.  On SQLite this does *not* store a timezone — the driver
returns naive datetimes.  :meth:`TenantModel.to_domain` detects naive
datetimes and coerces them to UTC-aware so the domain model is always
consistent.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, String, Text, func, select
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
from fastapi_tenancy.utils.db_compat import detect_dialect, requires_static_pool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ORM layer
# ---------------------------------------------------------------------------


class _Base(DeclarativeBase):
    """Private declarative base scoped to this module."""


class TenantModel(_Base):
    """SQLAlchemy ORM model for the ``tenants`` table.

    Column notes
    ------------
    * ``metadata`` is stored as JSON-encoded ``TEXT`` for maximum portability.
      PostgreSQL users who need JSON indexing may migrate the column to
      ``JSONB`` post-deployment.
    * ``created_at`` / ``updated_at`` use ``server_default=func.now()`` so
      the database clock is authoritative, not the application clock.
    * ``onupdate=func.now()`` on ``updated_at`` is honoured by PostgreSQL,
      MySQL, and MSSQL; SQLite requires an explicit assignment in application
      code (which the store performs).
    """

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
        index=True,
        comment="Opaque unique tenant ID.",
    )
    identifier: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
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
        index=True,
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
    metadata_json: Mapped[str] = mapped_column(
        "metadata",
        Text,
        nullable=False,
        default="{}",
        server_default="{}",
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
        onupdate=func.now(),
        comment="Last-modification timestamp (UTC).",
    )

    def to_domain(self) -> Tenant:
        """Convert this ORM row to an immutable :class:`~fastapi_tenancy.core.types.Tenant`.

        Handles edge cases:

        * ``metadata_json`` is ``NULL`` in legacy rows — falls back to ``{}``.
        * ``created_at`` / ``updated_at`` may be timezone-naive on SQLite —
          coerced to UTC-aware.

        Returns:
            Fully-populated, frozen :class:`~fastapi_tenancy.core.types.Tenant`.
        """
        try:
            meta: dict[str, Any] = json.loads(self.metadata_json or "{}")
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


# ---------------------------------------------------------------------------
# Store implementation
# ---------------------------------------------------------------------------


class SQLAlchemyTenantStore(TenantStore):
    """Async SQLAlchemy-backed tenant store compatible with all major databases.

    This is the recommended production store for fastapi-tenancy.

    Lifecycle::

        store = SQLAlchemyTenantStore(
            database_url="postgresql+asyncpg://user:pass@localhost/myapp",
        )
        await store.initialize()      # create table if not exists

        # ... serve requests ...

        await store.close()           # dispose pool on shutdown

    Args:
        database_url: Async SQLAlchemy connection URL.
        pool_size: Number of persistent connections in the pool.
        max_overflow: Extra connections allowed under burst load.
        pool_pre_ping: Verify connections before checkout (recommended).
        echo: Log every SQL statement (development only).
    """

    def __init__(
        self,
        database_url: str,
        pool_size: int = 10,
        max_overflow: int = 20,
        pool_pre_ping: bool = True,
        echo: bool = False,
    ) -> None:
        dialect = detect_dialect(database_url)
        kw: dict[str, Any] = {"echo": echo}

        if requires_static_pool(dialect):
            kw["poolclass"] = StaticPool
            kw["connect_args"] = {"check_same_thread": False}
        else:
            kw["pool_size"] = pool_size
            kw["max_overflow"] = max_overflow
            kw["pool_pre_ping"] = pool_pre_ping
            kw["pool_recycle"] = 3600

        self._engine: AsyncEngine = create_async_engine(database_url, **kw)
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            autocommit=False,
        )
        logger.info(
            "SQLAlchemyTenantStore ready dialect=%s pool_size=%d",
            dialect.value,
            pool_size,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Create the ``tenants`` table if it does not already exist (idempotent)."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)
        logger.info("tenants table ready")

    async def close(self) -> None:
        """Dispose the engine and release all pooled connections."""
        await self._engine.dispose()
        logger.info("SQLAlchemyTenantStore closed")

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def get_by_id(self, tenant_id: str) -> Tenant:
        async with self._session_factory() as session:
            row = await session.execute(
                select(TenantModel).where(TenantModel.id == tenant_id)
            )
            model = row.scalar_one_or_none()
            if model is None:
                raise TenantNotFoundError(identifier=tenant_id)
            return model.to_domain()

    async def get_by_identifier(self, identifier: str) -> Tenant:
        async with self._session_factory() as session:
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
        async with self._session_factory() as session:
            query = select(TenantModel)
            if status is not None:
                query = query.where(TenantModel.status == status.value)
            query = (
                query
                .order_by(TenantModel.created_at.desc())
                .offset(skip)
                .limit(limit)
            )
            result = await session.execute(query)
            return [m.to_domain() for m in result.scalars().all()]

    async def count(self, status: TenantStatus | None = None) -> int:
        async with self._session_factory() as session:
            query = select(func.count(TenantModel.id))
            if status is not None:
                query = query.where(TenantModel.status == status.value)
            result = await session.execute(query)
            return result.scalar() or 0

    async def exists(self, tenant_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TenantModel.id).where(TenantModel.id == tenant_id)
            )
            return result.scalar_one_or_none() is not None

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def create(self, tenant: Tenant) -> Tenant:
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
                metadata_json=json.dumps(tenant.metadata),
                created_at=tenant.created_at,
                updated_at=tenant.updated_at,
            )
            session.add(model)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise ValueError(  # noqa: B904
                    f"Tenant id={tenant.id!r} or identifier={tenant.identifier!r} "
                    "already exists."
                )
            except Exception as exc:
                await session.rollback()
                raise TenancyError(f"Failed to create tenant: {exc}") from exc
            await session.refresh(model)
            logger.info("Created tenant id=%s identifier=%s", tenant.id, tenant.identifier)
            return model.to_domain()

    async def update(self, tenant: Tenant) -> Tenant:
        async with self._session_factory() as session:
            try:
                result = await session.execute(
                    select(TenantModel).where(TenantModel.id == tenant.id)
                )
                model = result.scalar_one_or_none()
                if model is None:
                    raise TenantNotFoundError(identifier=tenant.id)
                model.identifier = tenant.identifier
                model.name = tenant.name
                model.status = tenant.status.value
                model.isolation_strategy = (
                    tenant.isolation_strategy.value if tenant.isolation_strategy else None
                )
                model.database_url = tenant.database_url
                model.schema_name = tenant.schema_name
                model.metadata_json = json.dumps(tenant.metadata)
                model.updated_at = datetime.now(UTC)
                await session.commit()
                await session.refresh(model)
                logger.info("Updated tenant id=%s", tenant.id)
                return model.to_domain()
            except TenantNotFoundError:
                raise
            except Exception as exc:
                await session.rollback()
                raise TenancyError(f"Failed to update tenant: {exc}") from exc

    async def delete(self, tenant_id: str) -> None:
        async with self._session_factory() as session:
            try:
                result = await session.execute(
                    select(TenantModel).where(TenantModel.id == tenant_id)
                )
                model = result.scalar_one_or_none()
                if model is None:
                    raise TenantNotFoundError(identifier=tenant_id)
                await session.delete(model)
                await session.commit()
                logger.info("Deleted tenant id=%s", tenant_id)
            except TenantNotFoundError:
                raise
            except Exception as exc:
                await session.rollback()
                raise TenancyError(f"Failed to delete tenant: {exc}") from exc

    async def set_status(self, tenant_id: str, status: TenantStatus) -> Tenant:
        async with self._session_factory() as session:
            try:
                result = await session.execute(
                    select(TenantModel).where(TenantModel.id == tenant_id)
                )
                model = result.scalar_one_or_none()
                if model is None:
                    raise TenantNotFoundError(identifier=tenant_id)
                model.status = status.value
                model.updated_at = datetime.now(UTC)
                await session.commit()
                await session.refresh(model)
                logger.info("Set tenant %s status → %s", tenant_id, status.value)
                return model.to_domain()
            except TenantNotFoundError:
                raise
            except Exception as exc:
                await session.rollback()
                raise TenancyError(f"Failed to update status: {exc}") from exc

    async def update_metadata(
        self,
        tenant_id: str,
        metadata: dict[str, Any],
    ) -> Tenant:
        async with self._session_factory() as session:
            try:
                result = await session.execute(
                    select(TenantModel).where(TenantModel.id == tenant_id)
                )
                model = result.scalar_one_or_none()
                if model is None:
                    raise TenantNotFoundError(identifier=tenant_id)
                existing: dict[str, Any] = {}
                try:
                    existing = json.loads(model.metadata_json or "{}")
                except (json.JSONDecodeError, TypeError):
                    pass
                model.metadata_json = json.dumps({**existing, **metadata})
                model.updated_at = datetime.now(UTC)
                await session.commit()
                await session.refresh(model)
                logger.info("Updated metadata for tenant id=%s", tenant_id)
                return model.to_domain()
            except TenantNotFoundError:
                raise
            except Exception as exc:
                await session.rollback()
                raise TenancyError(f"Failed to update metadata: {exc}") from exc


__all__ = ["SQLAlchemyTenantStore", "TenantModel"]
