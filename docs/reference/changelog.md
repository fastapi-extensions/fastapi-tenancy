---
title: Changelog
description: Version history for fastapi-tenancy.
---

# Changelog

All notable changes to fastapi-tenancy are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

---

## [0.2.0] — 2024-01-15

### Added

**Isolation**

- `HybridIsolationProvider` — route tenants between schema and RLS strategies by tier
- `DatabaseIsolationProvider` — separate database per tenant with async LRU engine cache
- `RLSIsolationProvider` — PostgreSQL Row-Level Security with `SET LOCAL` GUC

**Resolution**

- `SubdomainTenantResolver` — extract tenant from leftmost subdomain of `Host` header
- `JWTTenantResolver` — decode Bearer JWT and read configurable claim
- `PathTenantResolver` — extract tenant from URL path prefix

**Storage**

- `RedisTenantStore` — write-through Redis cache wrapper for any `TenantStore`
- Atomic `update_metadata` — PostgreSQL JSONB merge / serialisable transaction on other dialects
- `search()` — full-text tenant search (substring match on name/identifier)
- `bulk_update_status()` — batch status update

**Operations**

- `TenantMigrationManager` — bounded-concurrent Alembic migrations for all tenants
- `TenantCache` — in-process LRU+TTL cache with `stats()` and `purge_expired()`
- Redis sliding-window rate limiter in `TenancyManager.check_rate_limit()`
- Audit logging via `TenancyManager.write_audit_log()` and `make_audit_log_dependency`

**Types**

- `TenantMetrics` — snapshot of tenant usage metrics
- `TenantDataLeakageError`, `TenantQuotaExceededError`, `DatabaseConnectionError`
- `TenantConfig` — quota and feature-flag model built from `Tenant.metadata`

**Quality**

- 753 tests across unit, integration, and end-to-end suites
- 95%+ branch coverage enforced by CI
- Python 3.11, 3.12, and 3.13 tested in CI matrix

### Fixed

- **Critical**: `BaseHTTPMiddleware` replaced with raw ASGI implementation — eliminates response buffering and `contextvars` propagation bug in background tasks
- `SET search_path` now uses validated f-string interpolation instead of bind params (asyncpg `ProgrammingError` fix)
- `make_tenant_db_dependency` uses closure instead of `app.state` lookup (`RuntimeError` fix)
- `DatabaseIsolationProvider` LRU cache is now protected by `asyncio.Lock`

---

## [0.1.0] — 2024-07-01

### Added

- `TenancyManager` — central orchestrator with lifespan management
- `TenancyMiddleware` — raw ASGI middleware with excluded paths and error mapping
- `TenancyConfig` — pydantic-settings model with `TENANCY_` prefix
- `SchemaIsolationProvider` — schema-per-tenant with `SET LOCAL search_path`
- `HeaderTenantResolver` — read tenant from configurable HTTP header
- `SQLAlchemyTenantStore` — async SQLAlchemy 2.0 backend (PostgreSQL, MySQL, SQLite, MSSQL)
- `InMemoryTenantStore` — in-process store for tests
- `TenantContext` / `tenant_scope` — async-safe per-request context management
- `make_tenant_db_dependency` — closure-based FastAPI session dependency
- `Tenant`, `TenantStatus`, `IsolationStrategy`, `ResolutionStrategy` domain types
- `TenancyError` exception hierarchy (5 types)

[Unreleased]: https://github.com/fastapi-extensions/fastapi-tenancy/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/fastapi-extensions/fastapi-tenancy/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/fastapi-extensions/fastapi-tenancy/releases/tag/v0.1.0
