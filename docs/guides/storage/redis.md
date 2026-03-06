---
title: Redis Tenant Store
description: Write-through Redis caching wrapper around any TenantStore.
---

# Redis Tenant Store

`RedisTenantStore` is a **transparent caching wrapper** — it sits in front of any other store (typically `SQLAlchemyTenantStore`) and caches tenant objects in Redis with a TTL.

## Installation

```bash
pip install "fastapi-tenancy[redis]"
```

## How it works

```
get_by_identifier("acme")
    │
    ├── Redis HIT  ──→ deserialise → return Tenant   (< 1 ms)
    │
    └── Redis MISS → SQLAlchemyTenantStore.get_by_identifier()
                         │
                         └── cache_set(tenant)   → setex id + setex slug
                             return Tenant
```

Both `id` and `identifier` cache keys are written on every cache miss, so subsequent lookups by either key are served from Redis.

## Setup

```python
from fastapi_tenancy.storage.database import SQLAlchemyTenantStore
from fastapi_tenancy.storage.redis import RedisTenantStore

primary = SQLAlchemyTenantStore("postgresql+asyncpg://user:pass@localhost/myapp")
store   = RedisTenantStore(
    primary=primary,
    redis_url="redis://localhost:6379/0",
    ttl=3600,     # seconds — default: 3600
    prefix="ft",  # key prefix — default: "ft"
)

manager = TenancyManager(config, store)
```

## Cache keys

| Lookup | Redis key |
|--------|-----------|
| By ID | `ft:id:<tenant_id>` |
| By identifier | `ft:identifier:<identifier>` |

## Invalidation

Cache entries are **automatically invalidated** on every write operation (`update`, `delete`, `set_status`, `update_metadata`). No manual invalidation is needed.

## Cache warming

On startup, you can pre-warm the cache for all active tenants:

```python
await store.warm_cache()  # fetches all ACTIVE tenants and caches them
```

This is called automatically by `TenancyManager.initialize()` when `config.cache_enabled=True`.
