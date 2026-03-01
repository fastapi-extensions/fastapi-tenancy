# fastapi-tenancy Examples

A progressive series of real-world examples for the `fastapi-tenancy` library.
Each example ships with Docker and can be started with a single `docker compose up`.

---

## Quick reference

| Level | Example | Isolation | Resolution | Key concepts |
|-------|---------|-----------|------------|--------------|
| Basic | [01 Hello Tenant](#basic-01--hello-tenant) | — | Header | In-memory store, seed tenants |
| Basic | [02 Schema Isolation](#basic-02--schema-isolation-postgresql) | SCHEMA | Header | PostgreSQL schemas, SQLAlchemy models |
| Basic | [03 Subdomain](#basic-03--subdomain-resolution) | — | Subdomain | domain_suffix, Host header |
| Intermediate | [01 SaaS Starter](#intermediate-01--saas-starter) | RLS | JWT | Redis cache, rate limiting, audit logs |
| Intermediate | [02 Hybrid Isolation](#intermediate-02--hybrid-isolation) | HYBRID | Header | Premium vs standard tiers |
| Intermediate | [03 Custom Resolver](#intermediate-03--custom-resolver-api-key) | — | Custom | API key auth, resolver protocol |
| Advanced | [01 E-Commerce Platform](#advanced-01--e-commerce-platform) | DATABASE | Path | Per-tenant DBs, background tasks, feature gates |
| Advanced | [02 Analytics Platform](#advanced-02--analytics-platform) | SCHEMA | Header | WebSockets, SSE streaming, feature flags |

---

## Basic Examples

### Basic 01 — Hello Tenant

**The simplest possible multi-tenant app.**  No database required.

```bash
cd basic/01_hello_tenant
pip install "fastapi-tenancy" "fastapi[standard]"
uvicorn main:app --reload

# Test
curl http://localhost:8000/hello -H "X-Tenant-ID: acme-corp"
curl http://localhost:8000/hello -H "X-Tenant-ID: globex"
```

**What you learn:** `TenancyConfig`, `TenancyMiddleware`, `InMemoryTenantStore`,
`get_current_tenant` dependency, excluded paths, tenant seeding at startup.

---

### Basic 02 — Schema Isolation (PostgreSQL)

**A real TODO app.** Each tenant gets a dedicated PostgreSQL schema.

```bash
cd basic/02_schema_isolation
docker compose up            # starts postgres + app

# Register tenant (creates schema + tables)
curl -X POST http://localhost:8000/admin/tenants \
     -H "Content-Type: application/json" \
     -d '{"identifier": "acme-corp", "name": "Acme Corp"}'

# Create a TODO
curl -X POST "http://localhost:8000/todos" \
     -H "X-Tenant-ID: acme-corp" \
     -H "Content-Type: application/json" \
     -d '{"title": "Deploy to production"}'

# List TODOs (isolated to acme-corp only)
curl http://localhost:8000/todos -H "X-Tenant-ID: acme-corp"
```

**What you learn:** `SQLAlchemyTenantStore`, `make_tenant_db_dependency`,
`manager.register_tenant()`, schema provisioning with `app_metadata`, tenant lifecycle.

---

### Basic 03 — Subdomain Resolution

**Identify tenants from the subdomain** (`acme-corp.myapp.com → acme-corp`).

```bash
cd basic/03_subdomain_resolution
pip install "fastapi-tenancy" "fastapi[standard]"
uvicorn main:app --reload

# Simulate subdomain via Host header
curl http://localhost:8000/ -H "Host: acme-corp.localhost.local"
curl http://localhost:8000/ -H "Host: globex.localhost.local"
```

**What you learn:** `resolution_strategy="subdomain"`, `domain_suffix`,
`X-Forwarded-Host` trust, subdomain extraction logic.

---

## Intermediate Examples

### Intermediate 01 — SaaS Starter

**Production-pattern SaaS API** with JWT auth, Redis cache, rate limiting, and audit logs.

```bash
cd intermediate/01_saas_starter
docker compose up

# Generate a JWT for testing
pip install PyJWT
python generate_token.py acme-corp

# Use the token
curl http://localhost:8000/posts \
     -H "Authorization: Bearer <token>"

curl -X POST http://localhost:8000/posts \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{"title": "Hello SaaS!", "body": "It works."}'
```

**What you learn:** `resolution_strategy="jwt"`, `isolation_strategy="rls"`,
`cache_enabled`, `enable_rate_limiting`, `make_audit_log_dependency`, tenant_id
column stamping, RLS architecture.

---

### Intermediate 02 — Hybrid Isolation

**Different isolation for different tiers.** Premium tenants get dedicated schemas;
standard tenants share tables via RLS.

```bash
cd intermediate/02_hybrid_isolation
docker compose up

# Premium tenant (schema isolation)
curl http://localhost:8000/invoices -H "X-Tenant-ID: acme-corp"
curl http://localhost:8000/me       -H "X-Tenant-ID: acme-corp"
# → "tier": "premium", "isolation_strategy": "schema"

# Standard tenant (RLS isolation)
curl http://localhost:8000/invoices -H "X-Tenant-ID: startup-a"
curl http://localhost:8000/me       -H "X-Tenant-ID: startup-a"
# → "tier": "standard", "isolation_strategy": "rls"
```

**What you learn:** `isolation_strategy="hybrid"`, `premium_tenants` list,
`premium_isolation_strategy`, `standard_isolation_strategy`,
`config.is_premium_tenant()`, `config.get_isolation_strategy_for_tenant()`.

---

### Intermediate 03 — Custom Resolver (API Key)

**Identify tenants from a pre-shared API key** (`X-API-Key` header).

```bash
cd intermediate/03_custom_resolver
pip install "fastapi-tenancy" "fastapi[standard]"
uvicorn main:app --reload

# Pre-registered keys
curl http://localhost:8000/data -H "X-API-Key: key-acme-ABCDEF"
curl http://localhost:8000/data -H "X-API-Key: key-globex-123456"

# Invalid key → 400
curl http://localhost:8000/data -H "X-API-Key: wrong-key"
```

**What you learn:** `TenantResolver` protocol (duck typing), `resolution_strategy="custom"`,
`custom_resolver=` parameter, `TenantResolutionError`, hashed key lookup pattern.

---

## Advanced Examples

### Advanced 01 — E-Commerce Platform

**Complete multi-tenant e-commerce** with per-tenant PostgreSQL databases,
path-based resolution, persistent audit logs, background onboarding, and
plan-based feature gating.

```bash
cd advanced/01_ecommerce_platform
docker compose up --build

# Provision a tenant (creates a new PostgreSQL database automatically)
curl -X POST http://localhost:8000/admin/tenants \
     -H "Content-Type: application/json" \
     -d '{"identifier": "acme-corp", "name": "Acme Corp", "plan": "enterprise"}'

# Create a product
curl -X POST http://localhost:8000/t/acme-corp/products \
     -H "Content-Type: application/json" \
     -d '{"name": "Widget Pro", "price": 29.99, "sku": "WGT-001", "stock": 50}'

# Place an order
curl -X POST http://localhost:8000/t/acme-corp/orders \
     -H "Content-Type: application/json" \
     -d '{"customer_email": "alice@example.com", "product_ids": [1]}'

# List orders
curl http://localhost:8000/t/acme-corp/orders

# Suspend a tenant (returns 403 on all subsequent requests)
curl -X POST http://localhost:8000/admin/tenants/acme-corp/suspend
curl http://localhost:8000/t/acme-corp/orders   # → 403
```

**What you learn:** `isolation_strategy="database"`, `database_url_template`,
`resolution_strategy="path"`, `path_prefix`, custom `TenancyManager` subclass,
`write_audit_log()` override, `BackgroundTasks` for async onboarding,
`make_tenant_config_dependency`, `enable_soft_delete`, tenant lifecycle states.

---

### Advanced 02 — Analytics Platform

**High-scale analytics SaaS** with WebSocket real-time events, SSE streaming,
feature-flag gating, and bulk tenant operations.

```bash
cd advanced/02_analytics_platform
docker compose up --build

# Register a tenant
curl -X POST http://localhost:8000/admin/tenants \
     -H "Content-Type: application/json" \
     -d '{"identifier": "acme-corp", "name": "Acme Corp", "plan": "pro"}'

# Ingest an event
curl -X POST http://localhost:8000/events \
     -H "X-Tenant-ID: acme-corp" \
     -H "Content-Type: application/json" \
     -d '{"event_type": "page_view", "user_id": "u-123", "properties": {"page": "/home"}}'

# Stream live events via SSE
curl -N "http://localhost:8000/events/stream" \
     -H "X-Tenant-ID: acme-corp" \
     -H "Accept: text/event-stream"

# Advanced metrics (pro+ only — feature gated)
curl http://localhost:8000/metrics/summary -H "X-Tenant-ID: acme-corp"

# Starter plan → 403 on feature-gated endpoint
curl -X POST http://localhost:8000/admin/tenants \
     -d '{"identifier": "free-user", "name": "Free", "plan": "starter"}'
curl http://localhost:8000/metrics/summary -H "X-Tenant-ID: free-user"
# → 403 Feature 'advanced_analytics' is not available on your plan

# Bulk suspend (e.g. payment delinquency)
curl -X POST http://localhost:8000/admin/tenants/bulk-suspend \
     -H "Content-Type: application/json" \
     -d '["free-user"]'

# WebSocket (use wscat or websocat)
# websocat "ws://localhost:8000/ws" --header "X-Tenant-ID: acme-corp"
```

**What you learn:** `TenantConfig.features_enabled`, Depends factory for feature gates,
`StreamingResponse` with SSE, `WebSocket` with tenant isolation, bulk operations,
`ws.broadcast()` pattern, rate limiting for high-throughput analytics.

---

## Common patterns

### Tenant resolution strategies

```python
# Header (default)
config = TenancyConfig(resolution_strategy="header", tenant_header_name="X-Tenant-ID")

# Subdomain
config = TenancyConfig(resolution_strategy="subdomain", domain_suffix=".myapp.com")

# Path prefix  /t/{tenant}/...
config = TenancyConfig(resolution_strategy="path", path_prefix="/t")

# JWT bearer token
config = TenancyConfig(resolution_strategy="jwt", jwt_secret="...", jwt_tenant_claim="tenant_id")

# Custom (API key, cookie, etc.)
config   = TenancyConfig(resolution_strategy="custom")
manager  = TenancyManager(config, store, custom_resolver=MyResolver(store))
```

### Isolation strategies

```python
# Schema per tenant (PostgreSQL/MSSQL)
config = TenancyConfig(isolation_strategy="schema")

# Database per tenant
config = TenancyConfig(
    isolation_strategy="database",
    database_url_template="postgresql+asyncpg://user:pass@host/{database_name}",
)

# Row-Level Security (PostgreSQL only)
config = TenancyConfig(isolation_strategy="rls")

# Hybrid: premium → schema, standard → rls
config = TenancyConfig(
    isolation_strategy="hybrid",
    premium_tenants=["acme-corp", "globex"],
    premium_isolation_strategy="schema",
    standard_isolation_strategy="rls",
)
```

### Injecting dependencies

```python
from fastapi_tenancy.dependencies import (
    TenantDep,                          # Annotated[Tenant, Depends(get_current_tenant)]
    TenantOptionalDep,                  # Annotated[Tenant | None, ...]
    make_tenant_db_dependency,          # yields tenant-scoped AsyncSession
    make_tenant_config_dependency,      # yields TenantConfig from metadata
    make_audit_log_dependency,          # yields audit log writer
)

get_db     = make_tenant_db_dependency(manager)
get_config = make_tenant_config_dependency(manager)
get_audit  = make_audit_log_dependency(manager)

@app.get("/orders")
async def list_orders(
    tenant:  TenantDep,
    session: Annotated[AsyncSession, Depends(get_db)],
    config:  Annotated[TenantConfig, Depends(get_config)],
    audit:   Annotated[Any, Depends(get_audit)],
):
    await audit(action="list", resource="order")
    ...
```

### Lifespan (recommended pattern)

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.initialize()   # creates tables, warms cache
    yield
    await manager.close()        # disposes engines, closes Redis

app = FastAPI(lifespan=lifespan)
```

Or use the helper:

```python
app = FastAPI(lifespan=manager.create_lifespan())
```

---

## Installation quick reference

```bash
# Core (SQLite, in-memory)
pip install fastapi-tenancy

# PostgreSQL (asyncpg driver)
pip install "fastapi-tenancy[postgres]"

# PostgreSQL + Redis (cache + rate limiting)
pip install "fastapi-tenancy[postgres,redis]"

# PostgreSQL + Redis + JWT resolution
pip install "fastapi-tenancy[postgres,redis,jwt]"

# Everything
pip install "fastapi-tenancy[postgres,redis,jwt,migrations]"
```

Environment variables (all optional, each corresponds to a `TenancyConfig` field):

```bash
TENANCY_DATABASE_URL="postgresql+asyncpg://user:pass@localhost/myapp"
TENANCY_RESOLUTION_STRATEGY="header"
TENANCY_ISOLATION_STRATEGY="schema"
TENANCY_REDIS_URL="redis://localhost:6379/0"
TENANCY_CACHE_ENABLED="true"
TENANCY_ENABLE_RATE_LIMITING="true"
TENANCY_RATE_LIMIT_PER_MINUTE="100"
TENANCY_JWT_SECRET="your-secret-here"
```
