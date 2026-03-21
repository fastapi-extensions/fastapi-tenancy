<div align="center">

<img src="docs/assets/fastapi-tenancy-logo-256.svg" alt="fastapi-tenancy" width="120" />

# fastapi-tenancy

**Production-ready multi-tenancy for FastAPI**

Schema · Database · RLS · Hybrid isolation — fully async, fully typed

[![PyPI version](https://img.shields.io/pypi/v/fastapi-tenancy?color=0A7AE2&logo=pypi&logoColor=white)](https://pypi.org/project/fastapi-tenancy/)
[![Python](https://img.shields.io/pypi/pyversions/fastapi-tenancy?logo=python&logoColor=white)](https://pypi.org/project/fastapi-tenancy/)
[![CI](https://github.com/fastapi-extensions/fastapi-tenancy/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/fastapi-extensions/fastapi-tenancy/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/fastapi-extensions/fastapi-tenancy/graph/badge.svg?token=a4f8805e-fb44-41f7-aa4c-df9b54ecc36f)](https://codecov.io/gh/fastapi-extensions/fastapi-tenancy)
[![Docs](https://img.shields.io/badge/docs-mkdocs-blue?logo=readthedocs&logoColor=white)](https://fastapi-tenancy.readthedocs.io)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)

</div>

---

## Why fastapi-tenancy?

Every SaaS product built with FastAPI eventually hits the same walls:

- Where does tenant context live without leaking between requests or background tasks?
- How do you enforce schema or database isolation without boilerplate in every route?
- How do you support different isolation tiers for different customer plans?
- How do you run per-tenant Alembic migrations without a custom script every time?

**fastapi-tenancy** solves all of these — one library, four isolation strategies, zero per-route boilerplate.

---

## Features

| Category | What's included |
|---|---|
| **Isolation** | Schema-per-tenant, database-per-tenant, PostgreSQL RLS, Hybrid (mix by tier) |
| **Resolution** | Header, subdomain, URL path, JWT claim — or bring your own |
| **Storage** | SQLAlchemy async (PostgreSQL, MySQL, SQLite, MSSQL), Redis, in-memory |
| **Context** | `contextvars`-based — propagates through background tasks and streaming |
| **Middleware** | Raw ASGI (not `BaseHTTPMiddleware`) — zero buffering, correct ContextVar propagation |
| **Caching** | In-process LRU + TTL L1 cache wired into every request; Redis L2 |
| **Encryption** | Fernet/HKDF field-level encryption for `database_url` and `_enc_*` metadata |
| **Security** | JWT algorithm-confusion prevention, SQL-injection-safe DDL, anti-enumeration errors |
| **Migrations** | Per-tenant Alembic runner with concurrency control |
| **Observability** | `get_metrics()` — L1 cache hit rate, engine pool size |
| **Typing** | `py.typed`, strict mypy, Pydantic v2 frozen models throughout |

---

## Installation

```bash
# Minimal
pip install fastapi-tenancy

# PostgreSQL
pip install "fastapi-tenancy[postgres]"

# Full stack — PostgreSQL + Redis + JWT + Alembic
pip install "fastapi-tenancy[full]"
```

<details>
<summary>All available extras</summary>

| Extra | Driver installed |
|---|---|
| `postgres` | `asyncpg` |
| `sqlite` | `aiosqlite` |
| `mysql` | `aiomysql` |
| `mssql` | `aioodbc` |
| `redis` | `redis[asyncio]` |
| `jwt` | `PyJWT` |
| `migrations` | `alembic` |
| `full` | all of the above |

</details>

---

## Quick Start

```python
from fastapi import FastAPI, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi_tenancy import TenancyManager, TenancyConfig
from fastapi_tenancy.storage.database import SQLAlchemyTenantStore
from fastapi_tenancy.middleware.tenancy import TenancyMiddleware
from fastapi_tenancy.dependencies import make_tenant_db_dependency

# 1. Configure — all fields overridable via TENANCY_* env vars
config = TenancyConfig(
    database_url="postgresql+asyncpg://user:pass@localhost/myapp",
    resolution_strategy="header",   # read tenant from X-Tenant-ID header
    isolation_strategy="schema",    # one PostgreSQL schema per tenant
)

# 2. Wire
store   = SQLAlchemyTenantStore(config.database_url)
manager = TenancyManager(config, store)

# 3. Mount
app = FastAPI(lifespan=manager.create_lifespan())
app.add_middleware(TenancyMiddleware, manager=manager)

# 4. Use — zero per-route boilerplate
get_tenant_db = make_tenant_db_dependency(manager)

@app.get("/orders")
async def list_orders(db: AsyncSession = Depends(get_tenant_db)):
    # db is already scoped to the current tenant's schema
    result = await db.execute(select(Order))
    return result.scalars().all()
```

---

## Isolation Strategies

### Schema isolation — PostgreSQL / MSSQL

```python
config = TenancyConfig(
    database_url="postgresql+asyncpg://...",
    isolation_strategy="schema",
    schema_prefix="t_",
)
```

### Database isolation — all dialects

```python
config = TenancyConfig(
    database_url="postgresql+asyncpg://.../master",
    isolation_strategy="database",
    database_url_template="postgresql+asyncpg://.../{database_name}",
)
```

### Row-Level Security — PostgreSQL

```python
config = TenancyConfig(
    database_url="postgresql+asyncpg://...",
    isolation_strategy="rls",
)
```

### Hybrid — mix strategies by tier

```python
config = TenancyConfig(
    database_url="postgresql+asyncpg://...",
    isolation_strategy="hybrid",
    premium_isolation_strategy="schema",
    standard_isolation_strategy="rls",
    premium_tenants=["enterprise-co", "whale-customer"],
)
```

---

## Tenant Resolution

```python
# HTTP header (default: X-Tenant-ID)
TenancyConfig(resolution_strategy="header", tenant_header_name="X-Tenant-ID")

# Subdomain: acme.example.com -> "acme"
TenancyConfig(resolution_strategy="subdomain", domain_suffix=".example.com")

# URL path: /t/acme/orders -> "acme"
TenancyConfig(resolution_strategy="path", path_prefix="/t/")

# JWT claim from Bearer token
TenancyConfig(resolution_strategy="jwt", jwt_secret="...", jwt_tenant_claim="tenant_id")
```

All failure modes — missing header, invalid format, unknown tenant — return the same generic
`"Tenant not found"` to prevent identifier enumeration.

---

## Tenant Lifecycle

```python
# Provision — creates schema/database + tables + stores metadata
tenant = await manager.register_tenant(
    identifier="acme-corp",
    name="Acme Corporation",
    metadata={"plan": "enterprise", "max_users": 500},
    app_metadata=Base.metadata,
)

await manager.suspend_tenant(tenant.id)
await manager.activate_tenant(tenant.id)
await manager.delete_tenant(tenant.id)  # soft delete by default
```

---

## Field-level Encryption

```python
config = TenancyConfig(
    ...,
    enable_encryption=True,
    encryption_key="your-secret-key-at-least-32-chars!",
)

tenant = await manager.register_tenant(
    identifier="acme-corp",
    metadata={"_enc_api_key": "sk-live-abc123", "plan": "pro"},
)
# Stored as: {"_enc_api_key": "enc::gAAAAA...", "plan": "pro"}

plain = manager.decrypt_tenant(tenant)
plain.metadata["_enc_api_key"]  # "sk-live-abc123"
```

---

## L1 Cache

```python
config = TenancyConfig(
    ...,
    cache_enabled=True,
    redis_url="redis://localhost:6379/0",
    l1_cache_max_size=1000,
    l1_cache_ttl_seconds=60,
)
# Every warm request hits the in-process LRU cache — no Redis round-trip
# Cache auto-invalidates on create / update / set_status / delete
```

---

## Observability

```python
@app.get("/metrics")
async def metrics():
    return manager.get_metrics()

# {
#   "metrics_enabled": True,
#   "l1_cache": {"size": 42, "hit_rate_pct": 94.3, "hits": 1847, "misses": 112},
#   "engine_cache_size": 7,
# }
```

---

## Configuration

All fields overridable via `TENANCY_*` environment variables:

```bash
TENANCY_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/myapp
TENANCY_ISOLATION_STRATEGY=schema
TENANCY_RESOLUTION_STRATEGY=header
TENANCY_CACHE_ENABLED=true
TENANCY_REDIS_URL=redis://localhost:6379/0
TENANCY_ENABLE_ENCRYPTION=true
TENANCY_ENCRYPTION_KEY=your-secret-key
TENANCY_ENABLE_RATE_LIMITING=true
TENANCY_RATE_LIMIT_MAX_REQUESTS=1000
TENANCY_MAX_TENANTS=500
TENANCY_ENABLE_METRICS=true
```

---

## Database Compatibility

| Feature | PostgreSQL | MySQL | SQLite | MSSQL |
| --- | :---: | :---: | :---: | :---: |
| Schema isolation | ✓ (native) | ✓ (via DATABASE) | ✓ (table prefix) | ✓ (translate_map) |
| Database isolation | ✓ | ✓ | ✓ (file-based) | ! (manual) |
| RLS isolation | ✓ | ✗ | ✗ | ✗ |
| Hybrid isolation | ✓ | ✓ (partial) | ✓ (partial) | ! (limited) |
| Async driver | asyncpg | aiomysql | aiosqlite | aioodbc |

---

## Project Layout

```
src/fastapi_tenancy/
├── core/            # Config, context, types, exceptions
├── isolation/       # Schema, database, RLS, hybrid providers
├── resolution/      # Header, subdomain, path, JWT resolvers
├── storage/         # SQLAlchemy, in-memory, Redis stores
├── middleware/       # TenancyMiddleware (raw ASGI)
├── migrations/      # TenantMigrationManager (Alembic wrapper)
├── cache/           # TenantCache (LRU + TTL)
├── utils/           # Encryption, validation, DB compat, security
├── dependencies.py  # FastAPI dependency factories
└── manager.py       # TenancyManager — top-level orchestrator
```

---

## Development

```bash
git clone https://github.com/fastapi-extensions/fastapi-tenancy
cd fastapi-tenancy
uv sync --all-extras

# Unit + integration (no Docker needed)
uv run pytest -m "not e2e"

# Full suite with real databases
docker compose -f docker-compose.test.yml up -d
uv run pytest

# Code quality
uv run ruff check src tests && uv run ruff format src tests
uv run mypy src
uv run bandit -r src -ll -ii
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE) © fastapi-tenancy contributors
