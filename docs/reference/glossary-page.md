---
title: Glossary
description: Terms and definitions used throughout the fastapi-tenancy documentation.
---

# Glossary

Terms and definitions used throughout the fastapi-tenancy documentation.

---

**identifier**
: The human-readable slug for a tenant (e.g. `"acme-corp"`). Must be 3–63 lowercase letters, digits, and hyphens. Used in URLs, HTTP headers, subdomains, and audit logs.

**tenant ID**
: The opaque internal primary key for a tenant (e.g. `"t-abc-xyz"`). Cryptographically generated; should be treated as an implementation detail and not exposed in user-facing APIs.

**isolation namespace**
: The database structure that physically separates a tenant's data — a PostgreSQL schema, a separate database, or a set of RLS session variables.

**resolver**
: An object satisfying the `TenantResolver` protocol. Given an HTTP request, it returns the current `Tenant` or raises `TenantResolutionError` / `TenantNotFoundError`.

**provider**
: An object satisfying the `IsolationProvider` protocol. Given a `Tenant`, it yields an `AsyncSession` scoped to that tenant's namespace.

**write-through cache**
: A cache where writes immediately update both the cache and the underlying store, keeping them in sync. `RedisTenantStore` implements write-through caching.

**defence-in-depth**
: Multiple independent security controls protecting against the same threat. In fastapi-tenancy: RLS policies at the database level + `WHERE tenant_id = :id` in `apply_filters()`.

**bounded concurrency**
: Running at most N operations simultaneously to prevent resource exhaustion. `TenantMigrationManager.upgrade_all()` uses an `asyncio.Semaphore` for bounded migration concurrency.

---

## Abbreviations

The following terms are highlighted with tooltips throughout the documentation:

| Term | Meaning |
|------|---------|
| `tenant` | A single customer or user group whose data is isolated from all others |
| `isolation strategy` | Database mechanism (schema, database, RLS, or hybrid) separating tenant data |
| `resolution strategy` | Method to extract the tenant identifier from an HTTP request |
| `tenant store` | Storage backend persisting tenant metadata |
| `tenant identifier` | Human-readable slug used in URLs and headers |
| `tenant ID` | Opaque internal primary key used in databases and logs |
| `schema isolation` | PostgreSQL isolation using dedicated schemas per tenant |
| `database isolation` | Isolation strategy using separate databases per tenant |
| `RLS` | Row-Level Security — PostgreSQL query filtering by tenant via server-side policies |
| `hybrid isolation` | Routes tenants to different isolation strategies based on tier |
| `contextvars` | Python module providing per-async-task context variables |
| `TenantContext` | Static-method namespace managing the per-request tenant `ContextVar` |
| `tenant_scope` | Async context manager setting/resetting tenant on `TenantContext` |
| `soft delete` | Marking a tenant `DELETED` without removing the row |
| `LRU` | Least-Recently-Used — cache eviction policy |
| `TTL` | Time-To-Live — seconds before a cache entry goes stale |
| `GUC` | Grand Unified Configuration — PostgreSQL session-level settings |
