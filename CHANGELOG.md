# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.2.0] — 2026-02-06

> First functional release. Version 0.1.0 was a scaffolding placeholder with no source code; all implementation work ships in this release.

### Added

**Core**
- `TenancyConfig` — Pydantic Settings model with full environment-variable override support (`TENANCY_*` prefix)
- `TenantContext` — `contextvars`-based async-safe current-tenant propagation (`get_current_tenant`, `tenant_scope`)
- `Tenant`, `TenantConfig`, `TenantMetrics`, `AuditLog` — fully-typed domain models (Pydantic v2)
- `TenantStatus` enum — `active`, `suspended`, `deleted`, `provisioning`
- `IsolationStrategy` and `ResolutionStrategy` enums
- Full exception hierarchy: `TenancyError`, `TenantNotFoundError`, `TenantInactiveError`, `IsolationError`, `TenantResolutionError`, `ConfigurationError`, `DatabaseConnectionError`, `MigrationError`, `TenantDataLeakageError`, `RateLimitExceededError`, `TenantQuotaExceededError`

**Isolation Providers**
- `SchemaIsolationProvider` — PostgreSQL schema-per-tenant isolation with automatic DDL provisioning and FK remapping
- `DatabaseIsolationProvider` — separate database / connection pool per tenant
- `RLSIsolationProvider` — PostgreSQL row-level security via `SET LOCAL` GUC
- `HybridIsolationProvider` — route tenants to different strategies based on tier
- `BaseIsolationProvider` — abstract base class for custom providers

**Tenant Resolution**
- `HeaderTenantResolver` — resolves from a configurable HTTP header (default `X-Tenant-ID`)
- `SubdomainTenantResolver` — parses `<tenant>.example.com`
- `PathTenantResolver` — extracts identifier from URL path (e.g. `/tenants/<id>/...`)
- `JWTTenantResolver` — decodes a Bearer token and reads a configurable claim (requires `jwt` extra)
- `BaseTenantResolver` — abstract base for custom resolvers

**Storage Backends**
- `SQLAlchemyTenantStore` — async SQLAlchemy store supporting PostgreSQL, MySQL, SQLite, MSSQL; includes `get_metadata`, `update_metadata`, `list_tenants`, `create_tenant`, `delete_tenant`
- `InMemoryTenantStore` — thread-safe in-memory store (testing / development)
- `RedisTenantStore` — Redis-backed store with configurable TTL (requires `redis` extra)
- `TenantStore` — abstract base class / protocol

**Middleware & Dependencies**
- `TenancyMiddleware` — ASGI middleware that resolves the tenant from the request and sets `TenantContext`
- `make_tenant_db_dependency` — factory that returns a FastAPI `Depends`-compatible async generator yielding a per-tenant `AsyncSession`

**Manager**
- `TenancyManager` — top-level orchestrator; wires config, store, isolation provider, and resolver; exposes `create_lifespan()` for FastAPI lifespan integration

**Caching**
- `TenantCache` — in-process LRU cache with configurable TTL to reduce store round-trips

**Migrations**
- `TenantMigrationManager` — Alembic wrapper for running, upgrading, and rolling back per-tenant migrations (requires `migrations` extra)

**Utilities**
- `db_compat` — helpers for cross-dialect SQL compatibility
- `security` — HMAC-based signing utilities
- `validation` — tenant identifier sanitisation
- `_sanitize` — internal string sanitisation

**Packaging**
- `py.typed` PEP 561 marker
- Optional extras: `postgres`, `sqlite`, `mysql`, `mssql`, `redis`, `jwt`, `migrations`, `full`
- Python 3.11, 3.12, 3.13 support

**CI / Tooling**
- GitHub Actions CI workflow (`ci.yml`) — lint, type-check, test matrix across Python 3.11–3.13
- `ruff` for linting and formatting
- `mypy` in strict mode with SQLAlchemy and Pydantic plugins
- `pytest-asyncio` in auto mode; `pytest-cov` coverage reporting
- `uv` for dependency management

---

## [0.1.0] — 2026-02-20

### Added
- Initial project scaffold: `pyproject.toml`, `LICENSE`, directory structure, CI skeleton.
- No source code shipped in this release.

---

[0.2.0]: https://github.com/fastapi-extensions/fastapi-tenancy/releases/tag/v0.2.0
[0.1.0]: https://github.com/fastapi-extensions/fastapi-tenancy/releases/tag/v0.1.0
