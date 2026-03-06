# fastapi-tenancy

Production-ready multi-tenancy for FastAPI — schema, database, RLS, and hybrid isolation with full async support.

[![PyPI version](https://img.shields.io/pypi/v/fastapi-tenancy)](https://pypi.org/project/fastapi-tenancy/)
[![Python](https://img.shields.io/pypi/pyversions/fastapi-tenancy)](https://pypi.org/project/fastapi-tenancy/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/fastapi-extensions/fastapi-tenancy/actions/workflows/ci.yml/badge.svg)](https://github.com/fastapi-extensions/fastapi-tenancy/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-90%25-green)](https://github.com/fastapi-extensions/fastapi-tenancy)

---

## Features

- **Four isolation strategies** — schema-per-tenant, database-per-tenant, row-level security (RLS), and hybrid
- **Pluggable tenant resolution** — header, subdomain, URL path, JWT claim, or custom
- **Async-safe context** — `contextvars`-based current-tenant propagation, no thread-locals
- **Multiple storage backends** — SQLAlchemy (PostgreSQL, MySQL, SQLite, MSSQL) and Redis
- **Middleware + dependency injection** — drop-in FastAPI integration
- **Migration support** — per-tenant Alembic migrations via `TenantMigrationManager`
- **Fully typed** — `py.typed` marker, strict mypy, Pydantic v2 models throughout

---

## Installation

```bash
# Core (no async driver)
pip install fastapi-tenancy

# With PostgreSQL
pip install "fastapi-tenancy[postgres]"

# Full stack (PostgreSQL + Redis + JWT + Alembic)
pip install "fastapi-tenancy[full]"
```

Available extras: `postgres`, `sqlite`, `mysql`, `mssql`, `redis`, `jwt`, `migrations`, `full`.

---

## Quick Start

```python
from fastapi import FastAPI, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi_tenancy import TenancyManager, TenancyConfig
from fastapi_tenancy.storage.database import SQLAlchemyTenantStore
from fastapi_tenancy.middleware.tenancy import TenancyMiddleware
from fastapi_tenancy.dependencies import make_tenant_db_dependency

config = TenancyConfig(
    database_url="postgresql+asyncpg://user:pass@localhost/myapp",
    resolution_strategy="header",   # resolve tenant from X-Tenant-ID header
    isolation_strategy="schema",    # one PostgreSQL schema per tenant
)
store   = SQLAlchemyTenantStore(config.database_url)
manager = TenancyManager(config, store)

app = FastAPI(lifespan=manager.create_lifespan())
app.add_middleware(TenancyMiddleware, manager=manager)

get_tenant_db = make_tenant_db_dependency(manager)

@app.get("/items")
async def list_items(db: AsyncSession = Depends(get_tenant_db)):
    # `db` is scoped to the current tenant's schema automatically
    ...
```

---

## Isolation Strategies

| Strategy | Class | Best for |
|---|---|---|
| Schema | `SchemaIsolationProvider` | PostgreSQL, strong isolation, shared database |
| Database | `DatabaseIsolationProvider` | Maximum isolation, separate connection pools |
| RLS | `RLSIsolationProvider` | PostgreSQL row-level security policies |
| Hybrid | `HybridIsolationProvider` | Mix of strategies per tenant tier |

## Resolution Strategies

| Strategy | Class | Resolves from |
|---|---|---|
| Header | `HeaderTenantResolver` | `X-Tenant-ID` request header |
| Subdomain | `SubdomainTenantResolver` | `tenant.example.com` |
| Path | `PathTenantResolver` | `/tenants/{identifier}/...` |
| JWT | `JWTTenantResolver` | Claim inside a Bearer token (requires `jwt` extra) |

---

## Configuration

`TenancyConfig` is a Pydantic Settings model — all fields can be overridden via environment variables prefixed `TENANCY_`.

```python
from fastapi_tenancy import TenancyConfig

config = TenancyConfig(
    database_url="postgresql+asyncpg://...",
    isolation_strategy="schema",   # schema | database | rls | hybrid
    resolution_strategy="header",  # header | subdomain | path | jwt
    tenant_header_name="X-Tenant-ID",
    cache_ttl=300,
    max_tenants=1000,
)
```

---

## Development

### Prerequisites

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/)

### Setup

```bash
git clone https://github.com/fastapi-extensions/fastapi-tenancy
cd fastapi-tenancy
uv sync --all-extras
```

### Running Tests

```bash
# All tests with coverage
uv run pytest --cov -v

# Unit tests only (no database required)
uv run pytest -m unit

# Integration tests (SQLite / mocks)
uv run pytest -m integration
```

### Linting & Type Checking

```bash
uv run ruff check src tests
uv run ruff format src tests
uv run mypy src
```

---

## Project Layout

```
src/fastapi_tenancy/
├── core/           # Config, context, types, exceptions
├── isolation/      # Schema, database, RLS, hybrid providers
├── resolution/     # Header, subdomain, path, JWT resolvers
├── storage/        # SQLAlchemy, in-memory, Redis stores
├── middleware/     # TenancyMiddleware
├── migrations/     # TenantMigrationManager (Alembic wrapper)
├── cache/          # TenantCache
├── dependencies.py # FastAPI dependency factories
└── manager.py      # TenancyManager (top-level orchestrator)
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE)
