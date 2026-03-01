---
title: Configuration Fields
description: Every TenancyConfig field with type, default, validation, and env var name.
---

# Configuration Fields

See [Configuration](../getting-started/configuration.md) for usage examples.

For the corresponding environment variables, see [Environment Variables](env-vars.md).

## Complete field listing

| Field | Type | Default | Env var | Notes |
|-------|------|---------|---------|-------|
| `database_url` | `str` | **required** | `TENANCY_DATABASE_URL` | Must use async driver |
| `resolution_strategy` | `str` | `header` | `TENANCY_RESOLUTION_STRATEGY` | header/subdomain/path/jwt/custom |
| `isolation_strategy` | `str` | `schema` | `TENANCY_ISOLATION_STRATEGY` | schema/database/rls/hybrid |
| `database_pool_size` | `int` | `20` | `TENANCY_DATABASE_POOL_SIZE` | 1–100 |
| `database_max_overflow` | `int` | `40` | `TENANCY_DATABASE_MAX_OVERFLOW` | 0–200 |
| `database_pool_timeout` | `int` | `30` | `TENANCY_DATABASE_POOL_TIMEOUT` | seconds |
| `database_pool_recycle` | `int` | `3600` | `TENANCY_DATABASE_POOL_RECYCLE` | seconds; min 60 |
| `database_pool_pre_ping` | `bool` | `True` | `TENANCY_DATABASE_POOL_PRE_PING` | recommended for production |
| `database_echo` | `bool` | `False` | `TENANCY_DATABASE_ECHO` | development only |
| `database_url_template` | `str\|None` | `None` | `TENANCY_DATABASE_URL_TEMPLATE` | required for DATABASE isolation; must contain `{tenant_id}` or `{database_name}` |
| `max_cached_engines` | `int` | `100` | `TENANCY_MAX_CACHED_ENGINES` | 10–10000 |
| `redis_url` | `str\|None` | `None` | `TENANCY_REDIS_URL` | required for cache and rate limiting |
| `cache_ttl` | `int` | `3600` | `TENANCY_CACHE_TTL` | seconds |
| `cache_enabled` | `bool` | `False` | `TENANCY_CACHE_ENABLED` | requires redis_url |
| `enable_rate_limiting` | `bool` | `False` | `TENANCY_ENABLE_RATE_LIMITING` | requires redis_url |
| `rate_limit_per_minute` | `int` | `100` | `TENANCY_RATE_LIMIT_PER_MINUTE` | 1–10000 |
| `rate_limit_window_seconds` | `int` | `60` | `TENANCY_RATE_LIMIT_WINDOW_SECONDS` | min 1 |
| `tenant_header_name` | `str` | `X-Tenant-ID` | `TENANCY_TENANT_HEADER_NAME` | header strategy |
| `domain_suffix` | `str\|None` | `None` | `TENANCY_DOMAIN_SUFFIX` | required for subdomain |
| `path_prefix` | `str` | `/tenants` | `TENANCY_PATH_PREFIX` | path strategy |
| `jwt_secret` | `str\|None` | `None` | `TENANCY_JWT_SECRET` | required for jwt; min 32 chars |
| `jwt_algorithm` | `str` | `HS256` | `TENANCY_JWT_ALGORITHM` | |
| `jwt_tenant_claim` | `str` | `tenant_id` | `TENANCY_JWT_TENANT_CLAIM` | |
| `enable_audit_logging` | `bool` | `True` | `TENANCY_ENABLE_AUDIT_LOGGING` | |
| `enable_encryption` | `bool` | `False` | `TENANCY_ENABLE_ENCRYPTION` | |
| `encryption_key` | `str\|None` | `None` | `TENANCY_ENCRYPTION_KEY` | required if encryption on; min 32 chars |
| `allow_tenant_registration` | `bool` | `False` | `TENANCY_ALLOW_TENANT_REGISTRATION` | |
| `max_tenants` | `int\|None` | `None` | `TENANCY_MAX_TENANTS` | unlimited when None |
| `default_tenant_status` | `str` | `active` | `TENANCY_DEFAULT_TENANT_STATUS` | active/suspended/provisioning |
| `enable_soft_delete` | `bool` | `True` | `TENANCY_ENABLE_SOFT_DELETE` | |
| `enable_query_logging` | `bool` | `False` | `TENANCY_ENABLE_QUERY_LOGGING` | |
| `slow_query_threshold_ms` | `int` | `1000` | `TENANCY_SLOW_QUERY_THRESHOLD_MS` | |
| `enable_metrics` | `bool` | `True` | `TENANCY_ENABLE_METRICS` | |
| `premium_tenants` | `list[str]` | `[]` | `TENANCY_PREMIUM_TENANTS` | hybrid mode |
| `premium_isolation_strategy` | `str` | `schema` | `TENANCY_PREMIUM_ISOLATION_STRATEGY` | hybrid mode |
| `standard_isolation_strategy` | `str` | `rls` | `TENANCY_STANDARD_ISOLATION_STRATEGY` | hybrid mode |
| `schema_prefix` | `str` | `tenant_` | `TENANCY_SCHEMA_PREFIX` | must match `^[a-z][a-z0-9_]*$` |
| `public_schema` | `str` | `public` | `TENANCY_PUBLIC_SCHEMA` | |
