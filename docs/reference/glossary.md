---
title: Glossary
description: Terms and definitions used throughout the fastapi-tenancy documentation.
---

# Glossary

*[tenant]: A single customer or user group whose data is isolated from all other tenants.
*[isolation strategy]: The database mechanism that separates one tenant's data from another's — schema, database, RLS, or hybrid.
*[resolution strategy]: The method used to extract the tenant identifier from an HTTP request.
*[tenant store]: The storage backend that persists tenant metadata (name, status, config, etc.).
*[tenant identifier]: A human-readable slug (e.g. `"acme-corp"`) used in URLs and headers.
*[tenant ID]: An opaque internal primary key (e.g. `"t-abc123"`) used in databases and logs.
*[schema isolation]: A PostgreSQL isolation strategy where each tenant gets a dedicated schema and `search_path` is set per-connection.
*[database isolation]: An isolation strategy where each tenant has a separate database.
*[RLS]: Row-Level Security — a PostgreSQL feature where every query is automatically filtered by tenant via server-side policies.
*[hybrid isolation]: An isolation strategy that routes tenants to one of two configurable inner strategies (e.g. schema for premium, RLS for standard) based on tier membership.
*[contextvars]: Python's `contextvars` module — provides per-async-task isolated context variables, used to propagate tenant identity across a request.
*[TenantContext]: The namespace of static methods managing the per-request tenant `ContextVar`.
*[tenant_scope]: An async context manager (`async with tenant_scope(tenant)`) that sets a tenant on `TenantContext` for a block of code and resets it on exit — used in background tasks and tests.
*[soft delete]: Marking a tenant as `DELETED` in the store rather than removing its row — preserves audit history.
*[LRU]: Least-Recently-Used — eviction policy that removes the item accessed furthest in the past when the cache is full.
*[TTL]: Time-To-Live — the number of seconds before a cache entry is considered stale and must be refreshed.
*[GUC]: Grand Unified Configuration — PostgreSQL session-level settings (e.g. `app.current_tenant`) set with `SET LOCAL`.

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
