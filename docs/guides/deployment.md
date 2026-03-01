---
title: Deployment
description: Production checklist and Docker setup for fastapi-tenancy applications.
---

# Deployment

## Production checklist

### Security

- [ ] `jwt_secret` is at least 32 characters and stored in a secret manager (not in code)
- [ ] `database_url` uses SSL (`?ssl=require` for PostgreSQL, `?ssl=true` for MySQL)
- [ ] `database_pool_pre_ping=True` (default) to detect stale connections
- [ ] `enable_soft_delete=True` (default) for auditability
- [ ] `enable_audit_logging=True` (default)
- [ ] `DATABASE` isolation tenants have separate database credentials

### Performance

- [ ] `database_pool_size` tuned to your concurrency requirements
- [ ] `cache_enabled=True` with `redis_url` set for multi-instance deployments
- [ ] `max_cached_engines` ≥ peak concurrent tenant count (for `DATABASE` isolation)
- [ ] `database_pool_recycle` ≤ your database's `wait_timeout` (MySQL) or `tcp_keepalives_idle`

### Observability

- [ ] `enable_metrics=True` (default)
- [ ] Structured logging enabled (set `LOG_LEVEL=INFO` in production, `DEBUG` in staging only)
- [ ] `database_echo=False` in production (default)
- [ ] `slow_query_threshold_ms` tuned to your SLA

## Environment variables

```bash
# Core
TENANCY_DATABASE_URL=postgresql+asyncpg://user:pass@db.internal/myapp?ssl=require
TENANCY_RESOLUTION_STRATEGY=header
TENANCY_ISOLATION_STRATEGY=schema

# Cache + rate limiting
TENANCY_REDIS_URL=redis://redis.internal:6379/0
TENANCY_CACHE_ENABLED=true
TENANCY_CACHE_TTL=3600
TENANCY_ENABLE_RATE_LIMITING=true
TENANCY_RATE_LIMIT_PER_MINUTE=200

# Security
TENANCY_JWT_SECRET=change-me-use-a-secure-secret-from-vault
TENANCY_ENABLE_AUDIT_LOGGING=true

# Pool tuning
TENANCY_DATABASE_POOL_SIZE=20
TENANCY_DATABASE_MAX_OVERFLOW=40
TENANCY_DATABASE_POOL_TIMEOUT=30
TENANCY_DATABASE_POOL_RECYCLE=3600
```

## Docker Compose example

```yaml title="docker-compose.yml"
version: "3.9"
services:
  app:
    build: .
    ports:
      - "8000:8000"
    environment:
      TENANCY_DATABASE_URL: postgresql+asyncpg://app:secret@db/myapp
      TENANCY_REDIS_URL: redis://redis:6379/0
      TENANCY_CACHE_ENABLED: "true"
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: secret
      POSTGRES_DB: myapp
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U app"]
      interval: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 5
```

## Kubernetes

```yaml title="deployment.yaml"
apiVersion: apps/v1
kind: Deployment
metadata:
  name: myapp
spec:
  replicas: 3
  template:
    spec:
      containers:
        - name: app
          image: myapp:latest
          env:
            - name: TENANCY_DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: db-credentials
                  key: url
            - name: TENANCY_REDIS_URL
              value: redis://redis-svc:6379/0
            - name: TENANCY_CACHE_ENABLED
              value: "true"
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
```

!!! tip "Exclude `/health` from middleware"
    Always include `/health` in `excluded_paths` so Kubernetes probes work
    without a valid tenant header.

## Gunicorn + Uvicorn

```bash
gunicorn main:app \
  --workers 4 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --timeout 30
```

With multiple workers, use `cache_enabled=True` (Redis) so all workers share the tenant cache — the in-process `TenantCache` is per-worker.

## Connection pool sizing

For `N` Gunicorn workers with `pool_size=P` and `max_overflow=O`:

```
max PostgreSQL connections used = N × (P + O)
```

Ensure this is below your database's `max_connections`:

```python
# 4 workers × (20 pool + 40 overflow) = 240 connections max
config = TenancyConfig(
    ...
    database_pool_size=20,
    database_max_overflow=40,
)
```

For PostgreSQL, typical `max_connections` is 100–1000. With PgBouncer in transaction-pooling mode you can scale much further.
