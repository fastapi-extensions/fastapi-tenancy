<!-- Glossary abbreviations — auto-appended to every page via pymdownx.snippets -->
<!-- The full glossary page is rendered at reference/glossary-page.md          -->

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
