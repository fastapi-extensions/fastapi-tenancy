---
title: Rate Limiting
description: Per-tenant sliding-window rate limiting backed by Redis.
---

# Rate Limiting

fastapi-tenancy provides per-tenant sliding-window rate limiting backed by Redis sorted sets. When a tenant exceeds its limit, the middleware returns `HTTP 429 Too Many Requests`. WebSocket connections receive a `1008 Policy Violation` close frame.

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
    rate_limit_window_seconds=60,          # default — window duration in seconds
    rate_limit_fail_closed=False,          # default — see Failure modes below
)
```

Both `redis_url` and `enable_rate_limiting=True` are required together — the config validator raises `ValidationError` if `enable_rate_limiting=True` without a `redis_url`.

## Sliding-window algorithm

The rate limiter uses a single Redis sorted set per tenant and a server-side **Lua script** that executes atomically — no TOCTOU race conditions. Each request is represented by a unique member string (`"{timestamp}:{uuid4}"`) so two concurrent requests arriving within the same microsecond each produce a distinct sorted-set entry rather than overwriting each other.

Script steps:

1. **Remove** all members with scores older than `now - window_seconds` (`ZREMRANGEBYSCORE`)
2. **Count** remaining members (`ZCARD`)
3. If count ≥ limit → **deny** immediately without adding (`return count`)
4. Otherwise **add** this request (`ZADD`) and refresh the key TTL (`EXPIRE`)
5. **Return** the new count (≤ limit = allowed)

```
Redis key: tenancy:ratelimit:<tenant_id>
```

!!! note "Atomicity"
    The entire check-and-increment runs inside a single Lua script evaluated by
    Redis. This replaces the earlier two-pipeline approach that had an off-by-one
    race where concurrent requests could all read `count = limit - 1`, all pass,
    and then all add — silently breaching the limit.

## HTTP 429 response

```http
HTTP/1.1 429 Too Many Requests
Content-Type: application/json
Retry-After: 60

{"detail": "Rate limit exceeded. Please slow down."}
```

## WebSocket connections

For WebSocket upgrade requests that exceed the rate limit, the middleware sends a `websocket.close` frame with code `1008` (Policy Violation) instead of an HTTP response — emitting `http.response.start` on a WebSocket scope violates the ASGI specification.

## Failure modes — fail-open vs fail-closed

When Redis is temporarily unavailable (network partition, restart), the rate-limit check raises an exception. The `rate_limit_fail_closed` field controls what happens next:

| `rate_limit_fail_closed` | Redis down | Effect |
|--------------------------|-----------|--------|
| `False` *(default)*      | Logs `ERROR`, request proceeds | **Fail-open** — service stays up, rate limiting disabled until Redis recovers |
| `True`                   | Raises `RateLimitExceededError` → `HTTP 429` | **Fail-closed** — strict enforcement, requests blocked during Redis outage |

```python
# Strict enforcement — block all requests when Redis is down
config = TenancyConfig(
    ...,
    enable_rate_limiting=True,
    redis_url="redis://localhost:6379/0",
    rate_limit_fail_closed=True,
)
```

!!! warning "Redis failures are always logged at ERROR level"
    Regardless of `rate_limit_fail_closed`, every Redis failure during a
    rate-limit check is logged at `ERROR` level so it surfaces in your
    alerting dashboard.

Choose `fail_closed=True` in high-security environments where bypassing rate
limits during an outage is unacceptable. Use the default `fail_closed=False`
when service availability must be maintained even if Redis is down.

## Per-tenant limits

The default limit applies to all tenants. To set per-tenant limits, store the limit in `tenant.metadata`:

```python
await manager.store.update_metadata("t-vip", {"rate_limit_per_minute": 1000})
```

Read the per-tenant value from `TenantConfig` in a custom dependency:

```python
from fastapi_tenancy.dependencies import make_tenant_config_dependency

get_config = make_tenant_config_dependency(manager)

async def check_per_tenant_limit(
    tenant: TenantDep,
    config: Annotated[TenantConfig, Depends(get_config)],
) -> None:
    # config.rate_limit_per_minute reads from tenant.metadata
    # Subclass TenancyManager and override check_rate_limit() to enforce
    # the per-tenant value instead of the global config setting.
    ...
```

## Bypassing rate limits for specific paths

Add paths to `excluded_paths` in the middleware:

```python
app.add_middleware(
    TenancyMiddleware,
    manager=manager,
    excluded_paths=["/health", "/metrics", "/batch"],
)
```

## Monitoring

```bash
# Count requests for a tenant in the current window
ZCARD tenancy:ratelimit:t-abc123

# See all request timestamps and unique member strings
ZRANGE tenancy:ratelimit:t-abc123 0 -1 WITHSCORES

# TTL remaining on the key
TTL tenancy:ratelimit:t-abc123
```
