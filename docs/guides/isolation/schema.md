---
title: Schema Isolation
description: Dedicated PostgreSQL schema per tenant with automatic search_path management.
---

# Schema Isolation

`isolation_strategy="schema"` gives each tenant a dedicated schema within a shared database. The `search_path` (PostgreSQL) is set per-connection using `SET LOCAL`, so unqualified table names resolve to the correct schema automatically — no `WHERE tenant_id =` clauses needed in application code.

## How it works

```mermaid
sequenceDiagram
    participant C as Client
    participant M as Middleware
    participant I as SchemaIsolationProvider
    participant DB as PostgreSQL

    C->>M: GET /orders  X-Tenant-ID: acme
    M->>M: resolve tenant → Tenant(identifier="acme")
    M->>I: get_session(tenant)
    I->>DB: BEGIN
    I->>DB: SET LOCAL search_path TO "tenant_acme", public
    I-->>M: AsyncSession (scoped to tenant_acme)
    M->>C: route handler runs with session
    Note over DB: All queries use tenant_acme schema
    C->>DB: COMMIT / session close
    Note over DB: search_path resets at transaction end
```

## Configuration

```python
from fastapi_tenancy import TenancyConfig

config = TenancyConfig(
    database_url="postgresql+asyncpg://user:pass@localhost/myapp",
    isolation_strategy="schema",
    schema_prefix="tenant_",   # default — tenant "acme-corp" → schema "tenant_acme_corp"
    public_schema="public",    # default — included in search_path after tenant schema
)
```

### Schema naming

The schema name is computed from the tenant identifier:

```python
config.get_schema_name("acme-corp")   # → "tenant_acme_corp"
config.get_schema_name("hello.world") # → "tenant_hello_world"
```

Hyphens and dots are replaced with underscores. The `schema_prefix` must match `^[a-z][a-z0-9_]*$`.

## Provisioning a new tenant

When you call `manager.register_tenant(...)`, the provider:

1. Calls `CREATE SCHEMA IF NOT EXISTS "tenant_<identifier>"`
2. Calls `metadata.create_all()` inside the new schema (if `app_metadata` is passed)

```python
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass

class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    description: Mapped[str]

# Register and provision
tenant = await manager.register_tenant(
    identifier="acme-corp",
    name="Acme Corporation",
    app_metadata=Base.metadata,  # creates all tables in the new schema
)
```

## Security: SQL injection prevention

Schema names are validated by `assert_safe_schema_name()` before being
interpolated into any DDL statement. A schema name is valid only if it:

- Consists of lowercase letters, digits, underscores, and hyphens
- Is between 1 and 512 characters
- Does not start with `pg_` (reserved by PostgreSQL)

Any invalid name raises `IsolationError` immediately.

```python
from fastapi_tenancy.utils.validation import assert_safe_schema_name

# Safe — raises nothing
assert_safe_schema_name("tenant_acme_corp", context="SET search_path")

# Unsafe — raises IsolationError
assert_safe_schema_name("tenant'; DROP SCHEMA public; --", context="SET search_path")
```

## The `SET LOCAL` pattern

PostgreSQL's `SET` command does **not** accept bound parameters. The correct
approach is to validate the name first, then interpolate as a literal in an
f-string:

```python
# ✅ Correct (used internally)
assert_safe_schema_name(schema, context="get_session")
await conn.execute(text(f'SET LOCAL search_path TO "{schema}", public'))

# ❌ Wrong — asyncpg raises ProgrammingError
await conn.execute(text("SET LOCAL search_path TO :s").bindparams(s=schema))
```

`SET LOCAL` limits the change to the current transaction. When the transaction
closes (commit or rollback), the `search_path` reverts automatically, so no
tenant context leaks between requests sharing the same physical connection.

## Dialect behaviour

| Dialect | Mechanism |
|---------|-----------|
| PostgreSQL | `CREATE SCHEMA` + `SET LOCAL search_path` |
| MSSQL | `CREATE SCHEMA` + default schema switch |
| MySQL / MariaDB | Delegates to `DatabaseIsolationProvider` (separate databases) |
| SQLite | Table-name prefix (e.g. `tenant_acme_orders`) |

## Destroying a tenant

```python
await manager.delete_tenant(
    tenant_id="t-123",
    destroy_data=True,          # drops the schema
    app_metadata=Base.metadata, # optional: tables to drop first
)
```

When `destroy_data=True` and `enable_soft_delete=False`, the provider executes:

```sql
DROP SCHEMA "tenant_acme_corp" CASCADE;
```

!!! danger "This is irreversible"
    `DROP SCHEMA ... CASCADE` permanently deletes all tables and data in the
    schema. Set `enable_soft_delete=True` (the default) if you want to keep
    tenant records for audit purposes.

## Example: multi-tenant blog

```python
from fastapi import FastAPI, Depends
from typing import Annotated
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from fastapi_tenancy import TenancyConfig, TenancyManager, TenancyMiddleware, Tenant
from fastapi_tenancy.dependencies import make_tenant_db_dependency, get_current_tenant
from fastapi_tenancy.storage.database import SQLAlchemyTenantStore

# Models
class Base(DeclarativeBase): pass

class Post(Base):
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    body: Mapped[str]

# Config
config = TenancyConfig(
    database_url="postgresql+asyncpg://user:pass@localhost/blog",
    isolation_strategy="schema",
)
store   = SQLAlchemyTenantStore(config.database_url)
manager = TenancyManager(config, store)

# App
app = FastAPI()
app.add_middleware(TenancyMiddleware, manager=manager, excluded_paths=["/health"])

get_db = make_tenant_db_dependency(manager)

@app.get("/posts")
async def list_posts(
    tenant: Annotated[Tenant, Depends(get_current_tenant)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    result = await session.execute(select(Post))
    return result.scalars().all()
    # SQL issued: SELECT * FROM "tenant_<identifier>".posts
```
