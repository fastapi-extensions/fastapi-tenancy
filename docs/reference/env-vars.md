---
title: Environment Variables
description: Complete listing of all TENANCY_* environment variables.
---

# Environment Variables

All `TenancyConfig` fields can be set via environment variables with the `TENANCY_` prefix. Variable names are case-insensitive.

```bash
# Set in .env, shell, Docker, Kubernetes, etc.
TENANCY_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/myapp
TENANCY_RESOLUTION_STRATEGY=header
TENANCY_ISOLATION_STRATEGY=schema
```

## Core

| Variable | Type | Default |
|----------|------|---------|
| `TENANCY_DATABASE_URL` | `str` | **required** |
| `TENANCY_RESOLUTION_STRATEGY` | `str` | `header` |
| `TENANCY_ISOLATION_STRATEGY` | `str` | `schema` |

## Database pool

| Variable | Type | Default |
|----------|------|---------|
| `TENANCY_DATABASE_POOL_SIZE` | `int` | `20` |
| `TENANCY_DATABASE_MAX_OVERFLOW` | `int` | `40` |
| `TENANCY_DATABASE_POOL_TIMEOUT` | `int` | `30` |
| `TENANCY_DATABASE_POOL_RECYCLE` | `int` | `3600` |
| `TENANCY_DATABASE_POOL_PRE_PING` | `bool` | `true` |
| `TENANCY_DATABASE_ECHO` | `bool` | `false` |
| `TENANCY_DATABASE_URL_TEMPLATE` | `str` | — |
| `TENANCY_MAX_CACHED_ENGINES` | `int` | `100` |

## Resolution parameters

| Variable | Type | Default |
|----------|------|---------|
| `TENANCY_TENANT_HEADER_NAME` | `str` | `X-Tenant-ID` |
| `TENANCY_DOMAIN_SUFFIX` | `str` | — |
| `TENANCY_PATH_PREFIX` | `str` | `/tenants` |
| `TENANCY_JWT_SECRET` | `str` | — |
| `TENANCY_JWT_ALGORITHM` | `str` | `HS256` |
| `TENANCY_JWT_TENANT_CLAIM` | `str` | `tenant_id` |

## Isolation parameters

| Variable | Type | Default |
|----------|------|---------|
| `TENANCY_SCHEMA_PREFIX` | `str` | `tenant_` |
| `TENANCY_PUBLIC_SCHEMA` | `str` | `public` |

## Hybrid strategy

| Variable | Type | Default |
|----------|------|---------|
| `TENANCY_PREMIUM_TENANTS` | `list[str]` (comma-separated) | `[]` |
| `TENANCY_PREMIUM_ISOLATION_STRATEGY` | `str` | `schema` |
| `TENANCY_STANDARD_ISOLATION_STRATEGY` | `str` | `rls` |

## Cache

| Variable | Type | Default |
|----------|------|---------|
| `TENANCY_REDIS_URL` | `str` | — |
| `TENANCY_CACHE_ENABLED` | `bool` | `false` |
| `TENANCY_CACHE_TTL` | `int` | `3600` |

## Rate limiting

| Variable | Type | Default |
|----------|------|---------|
| `TENANCY_ENABLE_RATE_LIMITING` | `bool` | `false` |
| `TENANCY_RATE_LIMIT_PER_MINUTE` | `int` | `100` |
| `TENANCY_RATE_LIMIT_WINDOW_SECONDS` | `int` | `60` |

## Security

| Variable | Type | Default |
|----------|------|---------|
| `TENANCY_ENABLE_AUDIT_LOGGING` | `bool` | `true` |
| `TENANCY_ENABLE_ENCRYPTION` | `bool` | `false` |
| `TENANCY_ENCRYPTION_KEY` | `str` | — |

## Tenant management

| Variable | Type | Default |
|----------|------|---------|
| `TENANCY_ALLOW_TENANT_REGISTRATION` | `bool` | `false` |
| `TENANCY_MAX_TENANTS` | `int` | — |
| `TENANCY_DEFAULT_TENANT_STATUS` | `str` | `active` |
| `TENANCY_ENABLE_SOFT_DELETE` | `bool` | `true` |

## Observability

| Variable | Type | Default |
|----------|------|---------|
| `TENANCY_ENABLE_QUERY_LOGGING` | `bool` | `false` |
| `TENANCY_SLOW_QUERY_THRESHOLD_MS` | `int` | `1000` |
| `TENANCY_ENABLE_METRICS` | `bool` | `true` |
