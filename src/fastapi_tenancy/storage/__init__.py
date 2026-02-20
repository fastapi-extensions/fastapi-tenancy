"""Tenant storage backends for fastapi-tenancy.

All backends implement :class:`~fastapi_tenancy.storage.tenant_store.TenantStore`
and are fully interchangeable.

Backends
--------
:class:`~fastapi_tenancy.storage.database.SQLAlchemyTenantStore`
    Recommended production backend.  Async SQLAlchemy 2.0 supporting
    PostgreSQL (asyncpg), SQLite (aiosqlite), MySQL (aiomysql), and MSSQL.

:class:`~fastapi_tenancy.storage.memory.InMemoryTenantStore`
    In-memory store for tests and local development.  No I/O; data is
    lost when the process exits.

:class:`~fastapi_tenancy.storage.redis.RedisTenantStore`
    Redis write-through cache layer wrapping any primary store.
    Requires the ``redis`` extra.

Example — production with Redis cache::

    from fastapi_tenancy.storage import SQLAlchemyTenantStore, RedisTenantStore

    primary = SQLAlchemyTenantStore(
        database_url="postgresql+asyncpg://user:pass@localhost/myapp"
    )
    await primary.initialize()

    store = RedisTenantStore(
        redis_url="redis://localhost:6379/0",
        primary_store=primary,
        ttl=3600,
    )

Example — testing::

    from fastapi_tenancy.storage import InMemoryTenantStore

    store = InMemoryTenantStore()
    await store.create(Tenant(id="t1", identifier="acme", name="Acme Corp"))
"""

from fastapi_tenancy.storage.database import SQLAlchemyTenantStore, TenantModel
from fastapi_tenancy.storage.memory import InMemoryTenantStore
from fastapi_tenancy.storage.tenant_store import TenantStore

# Redis is optional — requires: pip install fastapi-tenancy[redis]
try:
    from fastapi_tenancy.storage.redis import RedisTenantStore
except ImportError:
    RedisTenantStore = None  # type: ignore[assignment, misc]

__all__ = [
    "InMemoryTenantStore",
    "RedisTenantStore",
    "SQLAlchemyTenantStore",
    "TenantModel",
    "TenantStore",
]
