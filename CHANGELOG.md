# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased] — Security & reliability fixes

> Eleven targeted fixes addressing concurrency safety, WebSocket error handling,
> JWT security hardening, connection-pool lifecycle, and observability gaps.

### Added

**`TenancyConfig.rate_limit_fail_closed` (`core/config.py`)**

New boolean field (`default=False`) that controls what happens when Redis is
unavailable during a rate-limit check:

- `False` *(default, fail-open)* — the check is skipped and the request
  proceeds. Suitable when service availability matters more than strict
  rate enforcement during outages.
- `True` *(fail-closed)* — raises `RateLimitExceededError` → HTTP 429.
  Suitable for high-security environments where bypassing rate limits
  during a Redis outage is unacceptable.

Configurable via `TENANCY_RATE_LIMIT_FAIL_CLOSED` environment variable.

**`TenantCache.aset()` — async-safe cache write (`cache/tenant_cache.py`)**

New coroutine method that acquires the internal `asyncio.Lock` before
writing to the cache. Use `await cache.aset(tenant)` in all async contexts
(middleware, resolvers, cache proxies) to prevent concurrent tasks from
interleaving writes and corrupting the `identifier → id` mapping.

The synchronous `set()` method is preserved for backwards compatibility and
synchronous setup code that runs before any concurrent tasks start.

**`JWTTenantResolver.audience` parameter (`resolution/jwt.py`)**

New optional `audience: str | None` parameter on `JWTTenantResolver`. When
set, PyJWT validates the `aud` claim in every decoded token. Tokens with a
missing or mismatched `aud` claim raise `TenantResolutionError` with reason
`"JWT audience claim does not match expected audience"`.

When `audience=None` (the default), a `WARNING` is emitted at resolver
construction time to alert operators of the cross-service token replay risk.

**`_ws_close()` middleware helper (`middleware/tenancy.py`)**

New internal coroutine `_ws_close(send, code)` that sends a
`websocket.close` frame. Used by all error paths in `_handle()` when
`scope["type"] == "websocket"` to comply with the ASGI specification.

### Fixed

**FIX 1 — `TenantCache` not safe under concurrent async tasks**

The `OrderedDict` backing the cache had no mutual-exclusion guard. Two
concurrent tasks resolving different tenants on a cache miss could interleave
their `set()` calls, causing the `identifier → id` mapping to point to the
wrong tenant — a silent cross-tenant data access bug.

Fix: added a lazily-created `asyncio.Lock` (`_get_lock()`). All writes in
async contexts now go through `aset()` which holds the lock. `_CachingStoreProxy`
was updated to call `await l1.aset(tenant)` on cache-miss population.

**FIX 2 — Redis failure during rate-limit check logged at WARNING, silently allowed**

A Redis unavailability error (network partition, restart) was caught and
logged at `WARNING` level only, silently allowing all requests through without
any observable signal in alerting systems.

Fix: Redis failures are now logged at `ERROR` level. When
`rate_limit_fail_closed=True`, a `RateLimitExceededError` is raised instead
of allowing the request through.

**FIX 3 — WebSocket error paths emitted `http.response.start` (ASGI violation)**

When tenant resolution failed, the tenant was inactive, or the rate limit was
exceeded on a WebSocket scope, the middleware tried to send an
`http.response.start` ASGI event. This violates the ASGI specification and
corrupts the connection at the ASGI server level.

Fix: `_handle()` now detects `scope["type"] == "websocket"` and calls
`_ws_close(send, code)` instead. Policy errors use code `1008`; internal
errors use `1011`. The resolver correctly builds a `WebSocket` object (not
`Request`) for WebSocket scopes, since `starlette.requests.Request` asserts
`scope["type"] == "http"`.

**FIX 4 — `_CachingStoreProxy.update()` did not evict old identifier on rename**

When a tenant's `identifier` (slug) was changed via `update()`, the proxy
invalidated the entry by ID but did not evict the old identifier key from the
L1 cache. The old slug remained warm, and subsequent lookups by the old slug
would resolve to a stale or wrong entry.

Fix: `update()` now looks up the cached entry before the store write. If the
identifier has changed, `invalidate_by_identifier(old_identifier)` is called
before proceeding.

**FIX 5 — JWT audience claim not validated (cross-service token replay)**

`JWTTenantResolver._decode_token()` did not pass `audience` to `jwt.decode()`.
A JWT issued for any service that shared the same secret was accepted by
every other service using that secret — a cross-service token replay vector.

Fix: when `audience` is configured on the resolver, it is passed directly to
`jwt.decode()`. `jwt.InvalidAudienceError` is caught and mapped to
`TenantResolutionError` with a descriptive reason and `details` dict.

**FIX 6 — `_json_response` was a sync function returning a coroutine object**

The function returned an inner `async def _send()` coroutine. Any caller that
forgot `await` would silently discard the coroutine without sending any
response — a hard-to-detect bug.

Fix: `_json_response` is now declared as `async def` and sends both ASGI
messages directly. Forgetting `await` is now a type error, not a silent no-op.

**FIX 7 — MySQL delegate not closed in `SchemaIsolationProvider.close()`**

`SchemaIsolationProvider.__init__()` creates a `DatabaseIsolationProvider`
delegate when the dialect is MySQL. The `close()` method disposed the main
engine but never called `await self._mysql_delegate.close()`, leaking the
delegate's connection pool on application shutdown.

Fix: `close()` now calls `await self._mysql_delegate.close()` before
disposing the engine.

**FIX 8 — Corrupt `tenant_metadata` JSON silently fell back to `{}`**

`TenantModel.to_domain()` caught `JSONDecodeError` and returned an empty
metadata dict without any log output. A corrupt row would silently strip all
tenant configuration — quotas, feature flags, rate limits — with no trace in
the application logs.

Fix: the exception handler now logs at `ERROR` level with the tenant ID
before falling back to `{}`.

**FIX 9 — `ip_address` and `user_agent` always `None` in audit log entries**

The `make_audit_log_dependency` closure created `AuditLog` entries with
`ip_address=None` and `user_agent=None` because the inner function had no
access to the HTTP request object.

Fix: the dependency function signature now accepts `request: Request` as a
FastAPI dependency. `request.client.host` and
`request.headers.get("user-agent")` are captured once and injected into every
`AuditLog` entry produced by the returned `log()` callable.

## [0.4.0] — 2026-04-02

> Concurrency hardening, PostgreSQL schema isolation correctness under multi-transaction
> sessions, serializable metadata merges with automatic retry, L1 cache lifecycle
> management, context-variable restoration safety, and 46 new regression tests.
> All 5 failures found by running the full live-database test suite against v0.3.0
> are fixed in this release.

### Security

**CRITICAL — Cross-tenant schema bleed under concurrent load (`isolation/schema.py`)**

Three successive implementations of the `_schema_session` search-path mechanism
were analysed and the root defects fixed:

- **v0.3.0 (engine-level `begin` listener)** — `event.listen(sync_engine, "begin",
  _on_begin)` attached a single listener to the *global engine*, shared by every
  concurrent request. Under load, Request A's and Request B's listeners both fired on
  every transaction begin, causing each session to silently receive the other tenant's
  `search_path`. Additionally, the `event.listen` call preceded the `try` block, so
  if `AsyncSession()` construction raised (e.g. pool exhausted), the listener remained
  permanently on the engine, corrupting all subsequent connections.

- **Pool `checkout`/`checkin` approach (interim)** — The asyncpg dialect wraps the raw
  DBAPI connection in `AdaptedConnection`, which does **not** implement the SQLAlchemy
  event interface. Attaching a `begin` listener to it raised
  `InvalidRequestError: No such event 'begin'` on every PostgreSQL connection checkout.

- **`Connection.begin` on `conn.sync_connection` (second interim)** — Correct for
  single-transaction sessions, but broken for multi-transaction ones. With
  `autobegin=False`, SQLAlchemy releases the physical connection back to the pool on
  every `commit()`. The next transaction uses a *new* `Connection` object, making
  the listener on the old object useless. The test
  `test_search_path_reapplied_after_commit` confirmed data was invisible after commit.

**Final fix — `Session.after_begin`:** The `Session.after_begin(session, transaction,
connection)` event fires for **every** transaction the session begins, including those
that start after `commit()` releases and re-acquires the connection. It receives the
current `Connection` as an argument, so `SET LOCAL search_path` is always issued on
the correct physical connection. The listener is scoped to the `session.sync_session`
object — invisible to other sessions — and is removed in `finally`.

**CRITICAL — RLS GUC listener not removed after session close (`isolation/rls.py`)**

The `@event.listens_for(sync_conn, "begin")` decorator inside `get_session()` is a
call-site decoration that **never removes the listener**. When the physical connection
was returned to the pool and reused by a future request for a different tenant, the
stale listener fired at the start of the new tenant's first transaction, silently
setting `app.current_tenant` to the previous tenant's ID — a silent cross-tenant data
read breach.

**Fix:** The listener function is defined as a named local variable, registered with
`event.listen()`, and removed with `event.remove()` in a `finally` block that wraps
the entire session lifetime — including the `AsyncSession()` constructor call.
`sync_conn` is initialised to `None` outside the block so the guard is safe even if
the session never opens.

### Added

**`TenancyConfig` — L1 cache fields now first-class (`core/config.py`)**
- `l1_cache_max_size: int` (default `1000`, range `10–100 000`) — maximum entries in
  the in-process LRU cache. Configurable via `TENANCY_L1_CACHE_MAX_SIZE` env var.
- `l1_cache_ttl_seconds: int` (default `60`, min `1`) — TTL for in-process cache
  entries. Configurable via `TENANCY_L1_CACHE_TTL_SECONDS` env var. Previously
  `TenancyManager` read these via `getattr(config, "l1_cache_...", fallback)` — they
  did not exist on `TenancyConfig` and could not be set by users.

**`TenancyManager` — periodic L1 cache purge task (`manager.py`)**
- `_run_cache_purge_loop()` — background `asyncio.Task` that calls
  `TenantCache.purge_expired()` every `max(1, l1_cache_ttl_seconds // 2)` seconds.
  Previously `purge_expired()` existed but was never called automatically; in
  low-traffic deployments expired entries accumulated indefinitely.
- `initialize()` creates the task (idempotent — a second call while the task is
  running is a no-op). The task is named `"fastapi-tenancy:l1-cache-purge"` for
  observability in async debuggers.
- `close()` cancels and awaits the task before disposing the store and isolation
  provider, preventing use-after-free on the cache reference.

**`TenantContext.reset_all()` (`core/context.py`)**
- New static method `reset_all(tenant_token, metadata_token)` that calls
  `_tenant_ctx.reset(tenant_token)` and `_metadata_ctx.reset(meta_token)` atomically.
  Counterpart to the updated `clear()`, enabling safe nested-scope context management
  at any call depth.

### Fixed

**FIX-1 — `DatabaseIsolationProvider._creation_locks` grows without bound
(`isolation/database.py`)**
- Replaced `dict[str, asyncio.Lock]` with `weakref.WeakValueDictionary[str,
  asyncio.Lock]`. Entries are garbage-collected automatically when no coroutine holds
  a live reference, bounding the dict to the number of *actively contested* tenants
  at any moment. The local variable `tenant_lock` inside `_get_engine` keeps the lock
  strongly referenced for the critical section, preventing premature GC between the
  `WeakValueDictionary` lookup and acquiring the lock. All manual `pop` cleanup calls
  removed.

**FIX-2 — Metadata merge loses updates under PostgreSQL concurrent writes
(`storage/database.py`)**
- `_update_metadata_pg()` — the SERIALIZABLE transaction correctly aborts one of N
  concurrent writers with `SerializationError` (pgcode `40001`). The previous
  implementation propagated this error as `TenancyError`, making `update_metadata`
  non-functional under any realistic write concurrency.
- **Fix:** Retry loop — up to 5 attempts with 5 ms base exponential back-off. The
  competing transaction has already committed by the time the error is received, so
  retries succeed immediately in practice. Detection is class-name-based to avoid
  a hard `asyncpg` import.
- The three-transaction corruption-recovery pattern (optimistic attempt → reset →
  re-merge) is replaced by a single SERIALIZABLE transaction with an inline
  `CASE … WHEN tenant_metadata IS NULL OR … THEN '{}'::jsonb ELSE … END` guard
  that handles NULL, empty-string, and non-JSON values server-side with no round-trip.

**FIX-3 — `TenantContext.clear()` and `clear_metadata()` discard tokens
(`core/context.py`)**
- Both methods previously called `set(None)` and discarded the returned `Token`,
  making it impossible to restore the previous state. In nested scopes — a test
  fixture inside a `tenant_scope`, background tasks, or middleware wrapping an
  outer tenant scope — the outer tenant was permanently erased.
- **Fix:** `clear()` now returns `(tenant_token, metadata_token)`. `clear_metadata()`
  returns its `Token`. Existing callers that ignore return values are unaffected.

**FIX-4 — `TenancyManager` reads L1 cache config via fragile `getattr` fallback
(`manager.py`)**
- `TenancyManager.__init__` previously used `getattr(config, "l1_cache_max_size",
  1000)` and `getattr(config, "l1_cache_ttl_seconds", 60)`. These fields did not
  exist on `TenancyConfig`. Users could not configure them via environment variables
  or programmatic construction, and any typo in the field name silently fell through
  to the hardcoded default.
- **Fix:** Both fields added to `TenancyConfig` (see Added above). `TenancyManager`
  now reads `config.l1_cache_max_size` and `config.l1_cache_ttl_seconds` directly.

**FIX-5 — MSSQL `destroy_tenant` dynamic SQL uses raw string concatenation
(`isolation/schema.py`)**
- The T-SQL block that drops all tables in a schema before dropping the schema itself
  previously used `'DROP TABLE [' + :schema + '].[' + TABLE_NAME + '];'` — raw string
  concatenation for `TABLE_NAME` with no quoting.
- **Fix:** Replaced with `QUOTENAME(TABLE_SCHEMA) + N'.' + QUOTENAME(TABLE_NAME)`.
  Both identifiers are now bracket-quoted by SQL Server's built-in `QUOTENAME()`
  function. `:schema` remains a bound parameter for the `WHERE TABLE_SCHEMA = :schema`
  predicate. `AND TABLE_TYPE = N'BASE TABLE'` guard added to exclude views.

**FIX-6 — Rate-limit Lua sorted-set member collision (`manager.py`)**
- `ZADD key now now` used the float timestamp as both score and member. Two requests
  arriving within the same microsecond produce an identical float value; the second
  `ZADD` overwrote the first entry rather than adding a new one, under-counting the
  window and allowing an extra request past the limit.
- **Fix:** Each call to `check_rate_limit()` generates
  `member = f"{now}:{uuid.uuid4().hex}"`. Score remains `now` for time-based
  eviction; the UUID suffix guarantees per-request uniqueness. The Lua script
  receives `member` as `ARGV[5]`.

### Changed

- **`TenancyConfig.cache_ttl`** — description updated to clarify this is the
  **Redis write-through cache TTL** (SETEX expiry), distinct from
  `l1_cache_ttl_seconds` (in-process LRU TTL). Both fields were previously conflated.
- **`TenancyManager.initialize()`** — now starts the L1 purge background task and
  logs its interval. Docstring updated to document all four startup steps.
- **`TenancyManager.close()`** — cancels and awaits the purge task before disposing
  the isolation provider and store.
- **`TenantContext.clear()`** — return type changed from `None` to
  `tuple[Token[Tenant | None], Token[dict[str, Any] | None]]`. Fully backward
  compatible — existing callers that ignore the return value continue to work.
- **`TenantContext.clear_metadata()`** — return type changed from `None` to
  `Token[dict[str, Any] | None]`. Fully backward compatible.

### Tests

- **46 new regression tests** added in `tests/test_fixes.py`, each named after the
  issue it covers and documenting the before/after behaviour.
- **`TestSchemaSessionListenerLifecycle`** — verifies `after_begin` listener cleanup
  on normal close, exception paths, and concurrent session independence.
- **`TestRLSListenerCleanup`** — verifies RLS GUC listener removal and correct GUC
  value after pool connection reuse across different tenants (PostgreSQL live-DB).
- **`TestL1CacheConfigFields`** — validates new `TenancyConfig` field declarations,
  bounds enforcement, and env-var naming.
- **`TestManagerUsesConfigFields`** — confirms `TenancyManager` uses direct field
  access, not `getattr` fallback.
- **`TestCreationLocksWeakValueDictionary`** — confirms `_creation_locks` is a
  `WeakValueDictionary`, entries are GC'd after success and failure, concurrent
  callers produce exactly one engine, and retries after failure succeed.
- **`TestUpdateMetadataSingleTransaction`** — validates normal merge and sequential
  consistency on SQLite.
- **`TestMSSQLDestroyTenantQuotename`** — source-level assertion that `QUOTENAME`
  is present and old raw-concatenation pattern is absent.
- **`TestContextClearReturnsTokens`** — covers all `clear()`/`reset_all()`/
  `clear_metadata()` token-restoration scenarios including nested scopes.
- **`TestCachePurgeTask`** — verifies task creation, cancellation, idempotent double-
  initialise, interval derivation, and safe `close()` without `initialize()`.
- **`TestRateLimitLuaUniqueMember`** — verifies ARGV[5] usage, 8-arg eval call
  signature, and per-call member uniqueness.
- **`tests/isolation/test_database.py`** — `TestCreationLocksLeak` updated to call
  `gc.collect()` before asserting `WeakValueDictionary` entry absence.

---

## [0.3.0] — 2026-03-20

> Security hardening, field-level encryption, L1 cache wired into every request,
> MSSQL schema isolation fix, anti-enumeration resolver, and a complete CI rewrite
> with MSSQL in the integration tier.

### Added

**Field-level encryption (`utils/encryption.py`)**
- `TenancyEncryption` — Fernet/HKDF-SHA256 implementation. Encrypts `database_url`
  and any metadata key prefixed `_enc_` at rest. Ciphertext is prefixed `enc::` for
  rolling-migration compatibility (plain values pass through unchanged on read).
- Key material is derived via HKDF-SHA256 — callers supply any 32+ char passphrase;
  the library derives a proper 32-byte Fernet key internally, preventing weak-key attacks.
- `TenancyManager` encrypts on `register_tenant()` write and decrypts transparently
  via `decrypt_tenant()`.

**L1 cache wired into every request (`manager.py`)**
- `_CachingStoreProxy` — transparent proxy that wraps any `TenantStore` with the
  in-process `TenantCache`. Intercepts `get_by_identifier()` (the hot path on every
  request) and serves from L1 on warm hits. Automatically invalidates on `create`,
  `update`, `set_status`, and `delete`. Previously `TenantCache` existed but was
  never connected to the request path.

**Observability**
- `TenancyManager.get_metrics()` — runtime snapshot: L1 cache hit rate / size,
  engine cache size (DATABASE isolation). Designed for wiring to a `/metrics` endpoint.

**CI / workflows**
- MSSQL added to the **integration** job tier using the custom image from
  `compose/mssql/` (built via `docker build` + `docker run` steps since GitHub
  Actions `services:` does not support `build:` context). This is the same image
  used by `make test-all` locally, ensuring parity between local and CI runs.
- `ci.yml` — path-filtered job graph. Docs-only PRs skip all test/lint jobs.
  Integration enforces `--cov-fail-under=85` which is achievable with MSSQL
  included. E2E (PostgreSQL × 2 versions, MySQL) runs only on `main` push or
  PRs labelled `run-e2e`.
- `docs.yml` — separate workflow for MkDocs build + GitHub Pages deploy,
  triggered only when docs paths change.
- `release.yml` — version/CHANGELOG validation before build, pre-release detection
  (→ TestPyPI), PyPI Trusted Publishing (OIDC — no stored API token).
- `codeql.yml` — triggered only on `src/**` changes plus weekly schedule.
- `dependency-review.yml` — blocks PRs introducing CVEs ≥ moderate or GPL/AGPL deps.
- `ci-pass` gate job — single required status for branch protection.

### Fixed

**FIX-1 — `_creation_locks` leak on engine creation failure (`isolation/database.py`)**
- `DatabaseIsolationProvider._get_engine()` — wrapped `create_async_engine()` in
  `try/except`. On failure the per-tenant lock is removed from `_creation_locks`
  before re-raising. Previously the lock leaked permanently, blocking all retries
  for that tenant until process restart.

**FIX-2 — MSSQL schema isolation (`isolation/schema.py`)**
- `_mssql_schema_session()` — replaced `ALTER USER CURRENT_USER WITH DEFAULT_SCHEMA`
  (permanently forbidden for the `dbo` principal, error 15150) with SQLAlchemy's
  `schema_translate_map` execution option. Every unqualified ORM table reference is
  rewritten to `[schema].[table]` at SQL-generation time with no DDL required.
- `_initialize_mssql_schema()` — uses `MetaData(schema=schema)` + `create_all` to
  generate `CREATE TABLE [schema].[table]` without touching any database user.
- `get_session()` now has an explicit `elif self.dialect == DbDialect.MSSQL` branch.

**FIX-3 — Tenant enumeration via header resolver (`resolution/header.py`)**
- All failure modes — missing header, invalid identifier format, unknown tenant —
  raise `TenantResolutionError` with the same generic reason `"Tenant not found"`.
- Unknown tenant now produces a 400 response (not 404) to prevent status-code-based
  enumeration of valid tenant identifiers.

**FIX-4 — `search()` ILIKE not portable to MSSQL (`storage/database.py`)**
- `SQLAlchemyTenantStore.search()` branches on `self._dialect`: MSSQL uses `.like()`
  (case-insensitive by default with CI collation); all other dialects keep `.ilike()`.

**FIX-5 — `_prefix_session` docstring gap (`isolation/schema.py`)**
- Added `.. warning::` block documenting that `session.info["table_prefix"]` persists
  on the session object across transactions but is not automatically set on new
  `AsyncSession` instances created manually within the same request.

**FIX-6 — `SET LOCAL search_path` implicit-transaction collision (`isolation/schema.py`)**
- `_schema_session()` — replaced `session.connection()` (which starts an implicit
  transaction, causing `InvalidRequestError: A transaction is already begun` when
  callers open `async with session.begin()`) with an engine-level `begin` event
  listener on `engine.sync_engine`. The listener fires before any transaction starts
  and is removed in `finally` to prevent cross-session leakage.

**FIX-7 — Flaky `test_multiple_requests_each_get_fresh_session` (`tests/test_dependencies.py`)**
- Replaced `id(session)` comparison (unreliable: CPython reuses memory addresses for
  non-overlapping objects) with `sessions_seen[0] is not sessions_seen[1]` while
  keeping both references alive simultaneously.

### Changed

- **`AuditLogWriter` promoted to runtime `Protocol`** — moved out of `TYPE_CHECKING`,
  decorated with `@runtime_checkable`. `isinstance(writer, AuditLogWriter)` now works
  correctly at runtime.
- **`enable_metrics` wired** — `TenancyManager.get_metrics()` exposes runtime metrics.
  Previously the config field was declared but nothing consumed it.
- **README** — complete rewrite with live CI and Codecov badges, feature table, all
  four isolation strategies with code examples, encryption and L1 cache usage,
  observability section, DB compatibility matrix.

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

[0.4.0]: https://github.com/fastapi-extensions/fastapi-tenancy/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/fastapi-extensions/fastapi-tenancy/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/fastapi-extensions/fastapi-tenancy/releases/tag/v0.2.0
[0.1.0]: https://github.com/fastapi-extensions/fastapi-tenancy/releases/tag/v0.1.0
