---
title: Caching
description: In-process LRU cache and Redis write-through layer for tenant lookups.
---

# Caching

fastapi-tenancy uses a two-level cache hierarchy to minimise database round-trips for tenant lookups:

```
Request
  │
  ▼
TenantCache (in-process LRU, μs)
  │ miss
  ▼
RedisTenantStore (network, ~1 ms)
  │ miss
  ▼
SQLAlchemyTenantStore (database, ~5–20 ms)
```

## In-process LRU cache (`TenantCache`)

The `TenantCache` is an in-process LRU dictionary with per-entry TTL. It is the fastest level of the hierarchy because it requires no network round-trip.

```python
from fastapi_tenancy.cache.tenant_cache import TenantCache

cache = TenantCache(
    max_size=1000,  # LRU eviction when full
    ttl=60,         # seconds before an entry is considered stale
)

# Synchronous write (safe before concurrent tasks start)
cache.set(tenant)

# Async write — acquires the internal asyncio.Lock for task safety
await cache.aset(tenant)

# Get (returns None on miss or expired entry)
tenant = cache.get("t-abc123")
tenant = cache.get_by_identifier("acme-corp")

# Invalidate
cache.invalidate("t-abc123")
cache.invalidate_by_identifier("acme-corp")

# Stats
stats = cache.stats()
# → {
#      "size": 42, "max_size": 1000, "ttl": 60,
#      "hits": 1830, "misses": 14, "hit_rate_pct": 99,
#    }
```

### Async safety

`TenantCache` protects all write operations with an internal `asyncio.Lock`.
Use `aset()` — not `set()` — from any async context (middleware, resolvers,
background tasks) so concurrent tasks cannot interleave their writes and
corrupt the `identifier → id` mapping.

`set()` remains available for synchronous setup code (e.g. test fixtures,
module-level initialisation) that runs before any concurrent tasks start.

### LRU eviction

When the cache reaches `max_size`, the least-recently-used entry is evicted on the next `aset()` / `set()`. This keeps memory usage bounded regardless of tenant count.

### TTL strategy

Entries expire after `ttl` seconds regardless of access pattern. Short TTLs (30–120 s) limit the staleness window after a tenant update. Long TTLs reduce database load but mean updates take longer to propagate.

### Configuration via `TenancyConfig`

The in-process cache TTL and max size are first-class config fields:

```python
config = TenancyConfig(
    database_url="...",
    l1_cache_max_size=2000,     # TENANCY_L1_CACHE_MAX_SIZE env var
    l1_cache_ttl_seconds=120,   # TENANCY_L1_CACHE_TTL_SECONDS env var
)
```

`TenancyManager` reads these fields during `initialize()` to configure the
`TenantCache` instance automatically.

## Redis write-through cache

For applications with multiple workers or Kubernetes pods, the in-process cache is not shared across processes. Use `RedisTenantStore` as a shared cache layer:

```python
config = TenancyConfig(
    database_url="postgresql+asyncpg://...",
    redis_url="redis://localhost:6379/0",
    cache_enabled=True,
    cache_ttl=3600,  # seconds — TENANCY_CACHE_TTL env var
)
```

When `cache_enabled=True`, the manager automatically wraps the SQLAlchemy store in a `RedisTenantStore` and calls `warm_cache()` during `initialize()`.

## Cache warming

On startup, all active tenants are pre-loaded to eliminate cold-start latency:

```python
await manager.initialize()
# → calls store.warm_cache() when cache_enabled=True
```

Or manually:

```python
tenants = await store.list(status=TenantStatus.ACTIVE)
for tenant in tenants:
    await cache.aset(tenant)
```

## Cache invalidation

All write operations (`create`, `update`, `set_status`, `update_metadata`, `delete`) automatically invalidate both the `id` and `identifier` cache keys. No manual invalidation is needed in normal operation.

!!! note "Identifier rename invalidation"
    When a tenant's `identifier` (slug) is changed via `update()`, the cache
    proxy evicts the **old** identifier key before writing the new one. This
    prevents the old slug from remaining warm and resolving to a stale entry.

For cross-process invalidation without Redis, use a short TTL (e.g. 30 s).

## Periodic expired-entry purge

`TenancyManager` runs a background `asyncio.Task` that calls
`TenantCache.purge_expired()` every `max(1, l1_cache_ttl_seconds // 2)` seconds,
so expired entries are collected proactively rather than only on access. The
task is started in `initialize()` and cancelled in `close()`.

## Tuning

| Scenario | Recommended settings |
|----------|---------------------|
| Single-process, low traffic | `cache_enabled=False` (default) |
| Single-process, high traffic | `l1_cache_max_size=500`, `l1_cache_ttl_seconds=300` |
| Multi-process, medium traffic | `cache_enabled=True`, `cache_ttl=300` |
| Multi-process, high traffic | `cache_enabled=True`, `cache_ttl=60`, `l1_cache_ttl_seconds=30` |
| Strict consistency required | `cache_enabled=False` or `l1_cache_ttl_seconds=1` |
