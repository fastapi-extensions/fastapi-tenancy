---
title: Database Isolation
description: Dedicated database per tenant with async LRU engine cache.
---

# Database Isolation

`isolation_strategy="database"` provisions a separate database for each tenant. Each tenant's SQLAlchemy engine is created lazily on first use and cached in an async LRU pool.

## How it works

```mermaid
sequenceDiagram
    participant R as Request
    participant P as DatabaseIsolationProvider
    participant LRU as LRU Engine Cache
    participant DB1 as tenant_acme_db
    participant DB2 as tenant_beta_db

    R->>P: get_session(acme)
    P->>LRU: lookup(acme)
    LRU-->>P: miss
    P->>DB1: create_async_engine(url)
    P->>LRU: store(acme → engine)
    P-->>R: AsyncSession(DB1)

    R->>P: get_session(beta)
    P->>LRU: lookup(beta)
    LRU-->>P: miss
    P->>DB2: create_async_engine(url)
    P->>LRU: store(beta → engine)
    P-->>R: AsyncSession(DB2)

    R->>P: get_session(acme)
    P->>LRU: lookup(acme)
    LRU-->>P: hit → engine
    P-->>R: AsyncSession(DB1)
```

## Configuration

```python
config = TenancyConfig(
    database_url="postgresql+asyncpg://user:pass@localhost/master",  # (1)
    isolation_strategy="database",
    database_url_template="postgresql+asyncpg://user:pass@localhost/{database_name}",  # (2)
    max_cached_engines=200,  # (3)
)
```

1. Used for the tenant metadata store and as fallback.
2. `{database_name}` is replaced with `tenant_<sanitised_tenant_id>_db`.
   `{tenant_id}` is also available as a placeholder.
3. When the LRU cache reaches this size, least-recently-used engines are
   disposed. Set this ≥ your peak concurrent tenant count.

### URL template placeholders

| Placeholder | Value |
|------------|-------|
| `{tenant_id}` | The tenant's opaque ID (e.g. `t-abc123`) |
| `{database_name}` | `tenant_<sanitised_id>_db` (e.g. `tenant_t_abc123_db`) |

```python
# Examples
config.get_database_url_for_tenant("t-abc-123")
# → "postgresql+asyncpg://user:pass@localhost/tenant_t_abc_123_db"
```

## Provisioning a new tenant

On `manager.register_tenant(...)`, the provider:

1. Creates the target database with `CREATE DATABASE tenant_<id>_db`
2. Connects to the new database
3. Runs `metadata.create_all()` inside it

```python
tenant = await manager.register_tenant(
    identifier="acme-corp",
    name="Acme Corporation",
    app_metadata=Base.metadata,
)
```

### PostgreSQL DDL

```sql
CREATE DATABASE tenant_t_abc_123_db
  ENCODING 'UTF8'
  TEMPLATE template0;
```

### MySQL DDL

```sql
CREATE DATABASE IF NOT EXISTS `tenant_t_abc_123_db`
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
```

## LRU engine cache

The provider maintains an internal `LRUEngineCache` to avoid recreating
`AsyncEngine` instances on every request:

- Cache capacity controlled by `max_cached_engines` (default: 100)
- LRU eviction disposes the engine (`engine.dispose()`) before removal
- All operations are protected by `asyncio.Lock` for task safety
- Engines are created with the same pool settings as the master engine

```python
# Tuning for a large tenant fleet
config = TenancyConfig(
    ...
    max_cached_engines=500,       # hold engines for 500 tenants in memory
    database_pool_size=5,         # smaller per-tenant pool (many × 5 = total connections)
    database_max_overflow=10,
    database_pool_pre_ping=True,  # detect stale connections early
)
```

## Destroying a tenant

```python
await manager.delete_tenant(
    tenant_id="t-abc-123",
    destroy_data=True,
)
```

The provider:

1. Disposes the tenant's engine (removes from LRU cache)
2. Issues `DROP DATABASE tenant_t_abc_123_db`

!!! danger "Irreversible"
    `DROP DATABASE` permanently destroys all data. Combine with
    `enable_soft_delete=True` (the default) if you need an audit trail.

## Supported databases

| Database | Supported | Notes |
|----------|-----------|-------|
| PostgreSQL | ✅ | Full support |
| MySQL / MariaDB | ✅ | Full support |
| MSSQL | ⚠️ | Raises `IsolationError` — create databases manually |
| SQLite | ❌ | Not supported (use Schema strategy) |
