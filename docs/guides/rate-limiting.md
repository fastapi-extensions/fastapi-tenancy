---
title: Rate Limiting
description: Per-tenant sliding-window rate limiting backed by Redis.
---

# Rate Limiting

fastapi-tenancy provides per-tenant sliding-window rate limiting backed by Redis sorted sets. When a tenant exceeds its limit, the middleware returns `HTTP 429 Too Many Requests`.

## Requirements

```bash
pip install "fastapi-tenancy[redis]"
```

## Configuration

```python
config = TenancyConfig(
    database_url="...",
    redis_url="redis://localhost:6379/0",   # required
    enable_rate_limiting=True,
    rate_limit_per_minute=100,             # default — requests per window
    rate_limit_window_seconds=60,          # default — window duration
)
```

Both `redis_url` and `enable_rate_limiting=True` are required together — the config validator will raise `ValidationError` if `enable_rate_limiting=True` without a `redis_url`.

## Sliding-window algorithm

The rate limiter uses a Redis sorted set per tenant. Each request adds the current timestamp as a score. To check the limit:

1. **Remove** all scores older than `now - window_seconds` (`ZREMRANGEBYSCORE`)
2. **Count** remaining scores (`ZCARD`)
3. If count ≥ limit → raise `RateLimitExceededError` → HTTP 429
4. **Add** current timestamp (`ZADD`)
5. **Expire** the key after `window_seconds` (`EXPIRE`)

Steps 1–2 run in a single pipeline for atomicity.

```
Redis key: tenancy:ratelimit:<tenant_id>
```

## HTTP 429 response

```http
HTTP/1.1 429 Too Many Requests
Content-Type: application/json
Retry-After: 60

{"detail": "Rate limit exceeded. Please slow down."}
```

## Per-tenant limits

The default limit applies to all tenants. To set per-tenant limits, store the limit in `tenant.metadata`:

```python
# Store per-tenant limit in metadata
tenant = await manager.store.update_metadata("t-vip", {"rate_limit_per_minute": 1000})
```

Read the per-tenant value from `TenantConfig` in a custom route dependency and call `check_rate_limit` after adjusting `manager.config.rate_limit_per_minute`, or implement a custom pre-request hook using `TenantConfig`:

```python
from fastapi_tenancy.dependencies import make_tenant_config_dependency
from fastapi_tenancy.core.types import TenantConfig

get_config = make_tenant_config_dependency(manager)

# Example: enforce per-tenant limit stored in metadata
async def check_per_tenant_limit(
    tenant: TenantDep,
    config: Annotated[TenantConfig, Depends(get_config)],
) -> None:
    if manager.config.enable_rate_limiting:
        # config.rate_limit_per_minute is read from tenant.metadata
        limit = config.rate_limit_per_minute
        # Enforce via your own Redis logic or by calling manager.check_rate_limit(tenant)
        # after temporarily setting the global limit — or subclass TenancyManager
        # and override check_rate_limit() to read from tenant metadata directly.
        await manager.check_rate_limit(tenant)
```

## Bypassing rate limits

Add specific paths to `excluded_paths` in the middleware, or check the tenant programmatically:

```python
@app.get("/admin/batch-job")
async def batch_job(
    tenant: TenantDep,
    session: SessionDep,
):
    # This route is excluded from rate limiting because it's in excluded_paths
    # OR because it runs as an admin without going through the middleware
    ...
```

## Monitoring

Use Redis to inspect the current rate-limit state:

```bash
# Count requests for a tenant in the current window
ZCARD tenancy:ratelimit:t-abc123

# See all request timestamps (Unix)
ZRANGE tenancy:ratelimit:t-abc123 0 -1 WITHSCORES

# TTL remaining on the key
TTL tenancy:ratelimit:t-abc123
```
