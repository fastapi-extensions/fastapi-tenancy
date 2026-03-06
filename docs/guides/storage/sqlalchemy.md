---
title: SQLAlchemy Tenant Store
description: The production-grade SQLAlchemy async backend for tenant metadata storage.
---

# SQLAlchemy Tenant Store

`SQLAlchemyTenantStore` is the recommended store for production. It persists tenant metadata in any database supported by SQLAlchemy 2.0 async.

## Installation

=== "PostgreSQL"
    ```bash
    pip install "fastapi-tenancy[postgres]"
    ```

=== "MySQL"
    ```bash
    pip install "fastapi-tenancy[mysql]"
    ```

=== "SQLite"
    ```bash
    pip install "fastapi-tenancy[sqlite]"
    ```

## Basic usage

```python
from fastapi_tenancy.storage.database import SQLAlchemyTenantStore

store = SQLAlchemyTenantStore("postgresql+asyncpg://user:pass@localhost/myapp")

# Initialise — creates the tenants table if it doesn't exist
await store.initialize()
```

## Supported databases

| Database | URL prefix |
|----------|-----------|
| PostgreSQL | `postgresql+asyncpg://` |
| MySQL / MariaDB | `mysql+aiomysql://` |
| SQLite | `sqlite+aiosqlite:///` |
| MSSQL | `mssql+aioodbc://` |

## The `tenants` table

The store creates a single `tenants` table:

```sql
CREATE TABLE tenants (
    id          VARCHAR(255)  PRIMARY KEY,
    identifier  VARCHAR(63)   UNIQUE NOT NULL,
    name        VARCHAR(255)  NOT NULL,
    status      VARCHAR(20)   NOT NULL DEFAULT 'active',
    metadata    TEXT,          -- JSON (JSONB on PostgreSQL)
    schema_name VARCHAR(255),
    database_url TEXT,
    isolation_strategy VARCHAR(20),
    created_at  TIMESTAMPTZ   NOT NULL,
    updated_at  TIMESTAMPTZ   NOT NULL
);

-- Indexes
CREATE INDEX ix_tenants_identifier           ON tenants (identifier);
CREATE INDEX ix_tenants_status               ON tenants (status);
CREATE INDEX ix_tenants_created_at           ON tenants (created_at);
-- Covering composite index: satisfies WHERE status = ? ORDER BY created_at DESC
CREATE INDEX ix_tenants_status_created_at    ON tenants (status, created_at);
```

On PostgreSQL, `metadata` uses `JSONB` for indexed JSON queries. On other databases it stores `TEXT` JSON.

## CRUD operations

```python
from fastapi_tenancy.core.types import Tenant, TenantStatus

# Create
tenant = await store.create(Tenant(
    id="t-abc123",
    identifier="acme-corp",
    name="Acme Corporation",
))

# Read
tenant = await store.get_by_id("t-abc123")
tenant = await store.get_by_identifier("acme-corp")

# List with pagination
tenants = await store.list(skip=0, limit=20, status=TenantStatus.ACTIVE)
total   = await store.count(status=TenantStatus.ACTIVE)

# Update
updated = await store.update(tenant.model_copy(update={"name": "Acme Corp"}))

# Status transitions
suspended = await store.set_status("t-abc123", TenantStatus.SUSPENDED)
activated = await store.set_status("t-abc123", TenantStatus.ACTIVE)

# Metadata merge (atomic on PostgreSQL, serialisable tx on others)
patched = await store.update_metadata("t-abc123", {"plan": "pro", "max_users": 100})

# Delete
await store.delete("t-abc123")  # hard delete
```

## Searching

```python
results = await store.search("acme", limit=10)
# → tenants whose name or identifier contains "acme"
```

!!! tip "Override for PostgreSQL full-text search"
    The default `search()` implementation loads records and filters in Python.
    For large tenant counts, override it with a `ILIKE` or full-text query.

## Batch operations

```python
# Fetch multiple tenants in one query
tenants = await store.get_by_ids(["t-abc", "t-def", "t-ghi"])

# Bulk status update
updated = await store.bulk_update_status(["t-abc", "t-def"], TenantStatus.SUSPENDED)
```

## Atomic metadata updates

The `update_metadata` method uses a database-level atomic merge to prevent the lost-update race condition:

```python
# PostgreSQL — uses JSONB || operator (single atomic UPDATE)
await store.update_metadata("t-abc", {"plan": "enterprise"})

# Other databases — uses serialisable transaction (BEGIN; SELECT FOR UPDATE; UPDATE; COMMIT)
await store.update_metadata("t-abc", {"plan": "enterprise"})
```

## Connection pool tuning

Pass connection pool settings directly to `SQLAlchemyTenantStore`:

```python
store = SQLAlchemyTenantStore(
    database_url="postgresql+asyncpg://user:pass@localhost/myapp",
    pool_size=20,        # persistent connections (default: 10)
    max_overflow=40,     # burst connections (default: 20)
    pool_pre_ping=True,  # verify before checkout (default: True)
    pool_recycle=3600,   # recycle after 1 hour (default: 3600)
    echo=False,          # log SQL statements (default: False)
)
```

## Using with migrations

If you manage the `tenants` table yourself with Alembic instead of letting the store auto-create it, skip calling `store.initialize()` in your lifespan. The `initialize()` method is the only place `CREATE TABLE` is issued — simply omit it:

```python
# Without auto-creation — manage the tenants table yourself via Alembic
store = SQLAlchemyTenantStore(database_url)
# Do NOT call await store.initialize() if Alembic owns the schema
```
