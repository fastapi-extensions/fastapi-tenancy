"""Shared fixtures for the isolation test package."""

from __future__ import annotations

from datetime import UTC, datetime
import os
import socket
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.types import IsolationStrategy, Tenant, TenantStatus
from fastapi_tenancy.isolation.database import DatabaseIsolationProvider
from fastapi_tenancy.isolation.hybrid import HybridIsolationProvider
from fastapi_tenancy.isolation.schema import SchemaIsolationProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable


_PG_URL = os.getenv(
    "POSTGRES_URL",
    "postgresql+asyncpg://testing:Testing123!@localhost:5432/test_db",
)

_SQLITE_MEM = "sqlite+aiosqlite:///:memory:"


def _tcp_ok(host: str, port: int, *, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _pg_up() -> bool:
    return _tcp_ok("localhost", 5432)


@pytest.fixture
def make_tenant() -> Callable[..., Tenant]:
    """Return a counter-based Tenant factory with all fields overridable."""
    counter: list[int] = [0]

    def _factory(
        *,
        tenant_id: str | None = None,
        identifier: str | None = None,
        name: str | None = None,
        status: TenantStatus = TenantStatus.ACTIVE,
        isolation_strategy: IsolationStrategy | None = None,
        metadata: dict[str, Any] | None = None,
        schema_name: str | None = None,
        database_url: str | None = None,
    ) -> Tenant:
        counter[0] += 1
        n = counter[0]
        now = datetime.now(UTC)
        return Tenant(
            id=tenant_id or f"t-iso-{n:05d}",
            identifier=identifier or f"iso-tenant-{n:05d}",
            name=name or f"Isolation Tenant {n}",
            status=status,
            isolation_strategy=isolation_strategy,
            metadata=metadata or {},
            schema_name=schema_name,
            database_url=database_url,
            created_at=now,
            updated_at=now,
        )

    return _factory


@pytest.fixture
def simple_metadata() -> sa.MetaData:
    """Return a minimal SQLAlchemy MetaData with a tenant-scoped ``items`` table."""
    meta = sa.MetaData()
    sa.Table(
        "items",
        meta,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(255), nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
    )
    return meta


@pytest.fixture
def fk_metadata() -> sa.MetaData:
    """MetaData with FK relationship between two tables — used in prefix FK remap tests."""
    meta = sa.MetaData()
    sa.Table(
        "categories",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("label", sa.String(100)),
    )
    sa.Table(
        "products",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("category_id", sa.Integer, sa.ForeignKey("categories.id")),
    )
    return meta


def _schema_config(db_url: str, **extra: Any) -> TenancyConfig:
    return TenancyConfig(
        database_url=db_url,
        isolation_strategy=IsolationStrategy.SCHEMA,
        schema_prefix="t_",
        database_echo=False,
        database_pool_size=2,
        database_max_overflow=2,
        database_pool_timeout=10,
        database_pool_recycle=300,
        database_pool_pre_ping=False,
        **extra,
    )


def _database_config(db_url: str, template: str, **extra: Any) -> TenancyConfig:
    return TenancyConfig(
        database_url=db_url,
        isolation_strategy=IsolationStrategy.DATABASE,
        database_url_template=template,
        schema_prefix="t_",
        database_echo=False,
        database_pool_size=2,
        database_max_overflow=2,
        database_pool_timeout=10,
        database_pool_recycle=300,
        database_pool_pre_ping=False,
        **extra,
    )


def _rls_config(db_url: str, **extra: Any) -> TenancyConfig:
    return TenancyConfig(
        database_url=db_url,
        isolation_strategy=IsolationStrategy.RLS,
        schema_prefix="t_",
        database_echo=False,
        database_pool_size=2,
        database_max_overflow=2,
        database_pool_timeout=10,
        database_pool_recycle=300,
        database_pool_pre_ping=False,
        **extra,
    )


def _hybrid_config(
    db_url: str,
    premium_tenants: list[str],
    premium_strategy: IsolationStrategy,
    standard_strategy: IsolationStrategy,
    **extra: Any,
) -> TenancyConfig:
    return TenancyConfig(
        database_url=db_url,
        isolation_strategy=IsolationStrategy.HYBRID,
        premium_isolation_strategy=premium_strategy,
        standard_isolation_strategy=standard_strategy,
        premium_tenants=premium_tenants,
        schema_prefix="t_",
        database_echo=False,
        database_pool_size=2,
        database_max_overflow=2,
        database_pool_timeout=10,
        database_pool_recycle=300,
        database_pool_pre_ping=False,
        **extra,
    )


@pytest.fixture
def sqlite_schema_config() -> TenancyConfig:
    """SCHEMA isolation config backed by in-memory SQLite."""
    return _schema_config(_SQLITE_MEM)


@pytest.fixture
def sqlite_db_config() -> TenancyConfig:
    """DATABASE isolation config backed by SQLite (uses file-path per tenant)."""
    return _database_config(
        _SQLITE_MEM,
        template="sqlite+aiosqlite:///./test_{database_name}.db",
    )


@pytest.fixture
def sqlite_hybrid_schema_rls_config() -> TenancyConfig:
    """HYBRID config: premium=SCHEMA, standard=DATABASE (SQLite-safe)."""
    return _hybrid_config(
        _SQLITE_MEM,
        premium_tenants=["t-iso-00001"],
        premium_strategy=IsolationStrategy.SCHEMA,
        standard_strategy=IsolationStrategy.DATABASE,
        database_url_template="sqlite+aiosqlite:///./std_{database_name}.db",
    )


@pytest_asyncio.fixture
async def schema_provider(sqlite_schema_config: TenancyConfig) -> AsyncIterator[Any]:
    """Yield a :class:`SchemaIsolationProvider` over SQLite (no external deps)."""

    p = SchemaIsolationProvider(sqlite_schema_config)
    try:
        yield p
    finally:
        await p.close()


@pytest_asyncio.fixture
async def database_provider(sqlite_db_config: TenancyConfig) -> AsyncIterator[Any]:
    """Yield a :class:`DatabaseIsolationProvider` over SQLite (no external deps)."""
    from fastapi_tenancy.isolation.database import DatabaseIsolationProvider  # noqa: PLC0415

    p = DatabaseIsolationProvider(sqlite_db_config)
    try:
        yield p
    finally:
        await p.close()


@pytest_asyncio.fixture
async def hybrid_provider(
    sqlite_hybrid_schema_rls_config: TenancyConfig,
) -> AsyncIterator[Any]:
    """Yield a :class:`HybridIsolationProvider` over SQLite-safe strategies."""
    from fastapi_tenancy.isolation.hybrid import HybridIsolationProvider  # noqa: PLC0415

    p = HybridIsolationProvider(sqlite_hybrid_schema_rls_config)
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
def pg_schema_config() -> TenancyConfig:
    if not _pg_up():
        pytest.skip("PostgreSQL not reachable on localhost:5432")
    return _schema_config(_PG_URL)


@pytest.fixture
def pg_rls_config() -> TenancyConfig:
    if not _pg_up():
        pytest.skip("PostgreSQL not reachable on localhost:5432")
    return _rls_config(_PG_URL)


@pytest.fixture
def pg_database_config() -> TenancyConfig:
    if not _pg_up():
        pytest.skip("PostgreSQL not reachable on localhost:5432")
    return _database_config(
        _PG_URL,
        template="postgresql+asyncpg://testing:Testing123!@localhost:5432/{database_name}",
    )


@pytest.fixture
def pg_hybrid_config() -> TenancyConfig:
    if not _pg_up():
        pytest.skip("PostgreSQL not reachable on localhost:5432")
    return _hybrid_config(
        _PG_URL,
        premium_tenants=["pg-premium-001"],
        premium_strategy=IsolationStrategy.SCHEMA,
        standard_strategy=IsolationStrategy.RLS,
    )


@pytest_asyncio.fixture
async def pg_schema_provider(pg_schema_config: TenancyConfig) -> AsyncIterator[Any]:
    """Yield a PostgreSQL SchemaIsolationProvider; skip when PG is down."""

    p = SchemaIsolationProvider(pg_schema_config)
    try:
        yield p
    finally:
        await p.close()


@pytest_asyncio.fixture
async def pg_rls_provider(pg_rls_config: TenancyConfig) -> AsyncIterator[Any]:
    """Yield a PostgreSQL RLSIsolationProvider; skip when PG is down."""
    from fastapi_tenancy.isolation.rls import RLSIsolationProvider  # noqa: PLC0415

    p = RLSIsolationProvider(pg_rls_config)
    try:
        yield p
    finally:
        await p.close()


@pytest_asyncio.fixture
async def pg_database_provider(pg_database_config: TenancyConfig) -> AsyncIterator[Any]:
    """Yield a PostgreSQL DatabaseIsolationProvider; skip when PG is down."""

    p = DatabaseIsolationProvider(pg_database_config)
    try:
        yield p
    finally:
        await p.close()


@pytest_asyncio.fixture
async def pg_hybrid_provider(pg_hybrid_config: TenancyConfig) -> AsyncIterator[Any]:
    """Yield a PostgreSQL HybridIsolationProvider; skip when PG is down."""

    p = HybridIsolationProvider(pg_hybrid_config)
    try:
        yield p
    finally:
        await p.close()


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncIterator[AsyncEngine]:
    """Yield a shared in-memory SQLite AsyncEngine for injection tests."""
    engine = create_async_engine(_SQLITE_MEM)
    try:
        yield engine
    finally:
        await engine.dispose()
