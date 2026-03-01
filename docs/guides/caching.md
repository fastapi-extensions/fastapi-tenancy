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

# Set
cache.set(tenant)

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

### LRU eviction

When the cache reaches `max_size`, the least-recently-used entry is evicted on the next `set()`. This keeps memory usage bounded regardless of tenant count.

### TTL strategy

Entries expire after `ttl` seconds regardless of access pattern. Short TTLs (30–120 s) limit the staleness window after a tenant update. Long TTLs (1–24 h) reduce database load but mean updates take longer to propagate.

## Redis write-through cache

For applications with many app instances (multiple workers, Kubernetes pods), the in-process cache is not shared. Use `RedisTenantStore` as a shared cache layer:

```python
config = TenancyConfig(
    database_url="postgresql+asyncpg://...",
    redis_url="redis://localhost:6379/0",
    cache_enabled=True,
    cache_ttl=3600,  # seconds
)
```

When `cache_enabled=True`, the manager automatically:
1. Wraps the SQLAlchemy store in a `RedisTenantStore`
2. Calls `warm_cache()` during `initialize()` to pre-warm with all active tenants

## Cache warming

On startup, you can pre-load all active tenants into the cache to eliminate cold-start latency:

```python
await manager.initialize()
# → calls store.warm_cache() when cache_enabled=True
```

Or manually:

```python
tenants = await store.list(status=TenantStatus.ACTIVE)
for tenant in tenants:
    cache.set(tenant)
```

## Cache invalidation

All write operations (`update`, `set_status`, `update_metadata`, `delete`) automatically invalidate both the `id` and `identifier` cache keys. No manual invalidation is needed in normal operation.

For cross-process invalidation (multiple app instances without Redis), you have two options:

1. **Use Redis** — `RedisTenantStore` handles cross-process invalidation automatically
2. **Short TTL** — Set a short TTL (e.g. 30 s) so stale entries expire quickly

## Tuning

| Scenario | Recommended settings |
|----------|---------------------|
| Single-process, low traffic | `cache_enabled=False` (default) |
| Single-process, high traffic | `TenantCache(max_size=500, ttl=300)` |
| Multi-process, medium traffic | `cache_enabled=True`, `cache_ttl=300` |
| Multi-process, high traffic | `cache_enabled=True`, `cache_ttl=60`, `TenantCache(ttl=30)` |
| Strict consistency required | `cache_enabled=False` or `cache_ttl=0` |
