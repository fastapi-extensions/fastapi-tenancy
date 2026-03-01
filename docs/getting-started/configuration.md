---
title: Configuration
description: Complete reference for every TenancyConfig field with environment variable names and validation rules.
---

# Configuration

`TenancyConfig` is a `pydantic-settings` model. It reads values from:

1. Environment variables (prefix `TENANCY_`)
2. A `.env` file in the current working directory
3. Keyword arguments passed directly to the constructor

Validation runs at **construction time** — misconfigured values raise
`ValidationError` immediately so problems surface during startup, not at the
first request.

## Minimal example

```python
from fastapi_tenancy import TenancyConfig

config = TenancyConfig(
    database_url="postgresql+asyncpg://user:pass@localhost/myapp",
)
```

## Environment variable example

```bash
# .env
TENANCY_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/myapp
TENANCY_RESOLUTION_STRATEGY=subdomain
TENANCY_DOMAIN_SUFFIX=.example.com
TENANCY_ISOLATION_STRATEGY=schema
TENANCY_REDIS_URL=redis://localhost:6379/0
TENANCY_CACHE_ENABLED=true
TENANCY_ENABLE_RATE_LIMITING=true
```

```python
config = TenancyConfig()  # reads from .env or environment
```

!!! tip "Safe logging"
    `str(config)` and `repr(config)` mask all passwords in connection URLs
    and secret fields. Always use these when logging configuration objects.

---

## Fields reference

### Core

| Field | Type | Default | Env var |
|-------|------|---------|---------|
| `database_url` | `str` | **required** | `TENANCY_DATABASE_URL` |
| `resolution_strategy` | `str` | `"header"` | `TENANCY_RESOLUTION_STRATEGY` |
| `isolation_strategy` | `str` | `"schema"` | `TENANCY_ISOLATION_STRATEGY` |

### Database pool

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `database_pool_size` | `int` | `20` | Persistent connections in pool |
| `database_max_overflow` | `int` | `40` | Extra connections under burst |
| `database_pool_timeout` | `int` | `30` | Seconds to wait for connection |
| `database_pool_recycle` | `int` | `3600` | Seconds before proactive replacement |
| `database_pool_pre_ping` | `bool` | `True` | Verify connections before checkout |
| `database_echo` | `bool` | `False` | Log every SQL statement |
| `database_url_template` | `str \| None` | `None` | Required for `DATABASE` isolation |
| `max_cached_engines` | `int` | `100` | LRU size for per-tenant engine cache |

### Resolution parameters

| Field | Type | Default | Used by |
|-------|------|---------|---------|
| `tenant_header_name` | `str` | `"X-Tenant-ID"` | `header` strategy |
| `domain_suffix` | `str \| None` | `None` | `subdomain` strategy (required) |
| `path_prefix` | `str` | `"/tenants"` | `path` strategy |
| `jwt_secret` | `str \| None` | `None` | `jwt` strategy (required, min 32 chars) |
| `jwt_algorithm` | `str` | `"HS256"` | `jwt` strategy |
| `jwt_tenant_claim` | `str` | `"tenant_id"` | `jwt` strategy |

### Isolation parameters

| Field | Type | Default | Used by |
|-------|------|---------|---------|
| `schema_prefix` | `str` | `"tenant_"` | `schema` strategy |
| `public_schema` | `str` | `"public"` | `schema` strategy (PostgreSQL) |

### Hybrid strategy

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `premium_tenants` | `list[str]` | `[]` | Tenant IDs that get premium treatment |
| `premium_isolation_strategy` | `str` | `"schema"` | Strategy for premium tenants |
| `standard_isolation_strategy` | `str` | `"rls"` | Strategy for standard tenants |

### Cache

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `redis_url` | `str \| None` | `None` | Required when `cache_enabled=True` |
| `cache_enabled` | `bool` | `False` | Enable Redis write-through cache |
| `cache_ttl` | `int` | `3600` | Seconds before cache entry expires |

### Rate limiting

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enable_rate_limiting` | `bool` | `False` | Enable per-tenant sliding-window limiting |
| `rate_limit_per_minute` | `int` | `100` | Default requests per window |
| `rate_limit_window_seconds` | `int` | `60` | Window duration in seconds |

### Security

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enable_audit_logging` | `bool` | `True` | Record tenant operations |
| `enable_encryption` | `bool` | `False` | Encrypt sensitive fields at rest |
| `encryption_key` | `str \| None` | `None` | Base64 32-byte key (required when encryption on) |

### Tenant management

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `allow_tenant_registration` | `bool` | `False` | Allow self-service registration |
| `max_tenants` | `int \| None` | `None` | Hard cap on tenant count |
| `default_tenant_status` | `str` | `"active"` | Initial status for new tenants |
| `enable_soft_delete` | `bool` | `True` | Mark deleted instead of removing rows |

### Observability

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enable_query_logging` | `bool` | `False` | Log slow database queries |
| `slow_query_threshold_ms` | `int` | `1000` | Threshold for slow-query logging |
| `enable_metrics` | `bool` | `True` | Expose tenant usage metrics |

---

## Validation rules

`TenancyConfig` enforces these cross-field rules at construction time:

- `cache_enabled=True` requires `redis_url` to be set
- `enable_rate_limiting=True` requires `redis_url` to be set
- `isolation_strategy="database"` requires `database_url_template` to be set
- `resolution_strategy="jwt"` requires `jwt_secret` (min 32 characters)
- `resolution_strategy="subdomain"` requires `domain_suffix`
- `isolation_strategy="hybrid"` requires `premium_isolation_strategy ≠ standard_isolation_strategy`
- `schema_prefix` must match `^[a-z][a-z0-9_]*$`
- `jwt_secret`, if set, must be at least 32 characters
- `encryption_key`, if set, must be at least 32 characters

---

## Helper methods

### `get_schema_name(identifier)`

Compute the PostgreSQL schema name for a tenant identifier:

```python
config = TenancyConfig(database_url="...", schema_prefix="tenant_")
config.get_schema_name("acme-corp")  # → "tenant_acme_corp"
```

Hyphens and dots in the identifier are replaced with underscores.

### `get_database_url_for_tenant(tenant_id)`

Build the database URL for a tenant in `DATABASE` isolation mode:

```python
config = TenancyConfig(
    database_url="...",
    isolation_strategy="database",
    database_url_template="postgresql+asyncpg://user:pass@localhost/{database_name}",
)
config.get_database_url_for_tenant("tenant-abc-123")
# → "postgresql+asyncpg://user:pass@localhost/tenant_tenant_abc_123_db"
```

### `is_premium_tenant(tenant_id)`

Check whether a tenant ID appears in the `premium_tenants` list:

```python
config = TenancyConfig(
    database_url="...",
    premium_tenants=["t-enterprise-1", "t-enterprise-2"],
)
config.is_premium_tenant("t-enterprise-1")  # → True
```

### `get_isolation_strategy_for_tenant(tenant_id)`

Return the effective isolation strategy for a given tenant ID (respects `HYBRID` routing):

```python
strategy = config.get_isolation_strategy_for_tenant("t-enterprise-1")
# → IsolationStrategy.SCHEMA  (if it's in premium_tenants)
```
