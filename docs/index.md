---
title: fastapi-tenancy
description: Production-ready multi-tenancy for FastAPI — schema, database, RLS, and hybrid isolation with full async support.
hide:
  - navigation
  - toc
---

<div class="ft-hero">
  <div class="ft-hero-inner">
    <h1>fastapi-tenancy</h1>
    <p>
      Production-ready multi-tenancy for FastAPI.<br>
      Four isolation strategies. Five resolution strategies. Full async support. 95%+ test coverage.
    </p>
    <div class="ft-hero-buttons">
      <a href="getting-started/quickstart/" class="ft-btn ft-btn--primary">
        ⚡ Quick Start
      </a>
      <a href="guides/" class="ft-btn ft-btn--secondary">
        📖 Read the Guides
      </a>
      <a href="https://github.com/fastapi-extensions/fastapi-tenancy" class="ft-btn ft-btn--secondary">
        ⭐ GitHub
      </a>
    </div>
  </div>
</div>

<div class="ft-grid">
  <a href="guides/isolation/" class="ft-card">
    <span class="ft-card-icon">🗄️</span>
    <h3>Four isolation strategies</h3>
    <p>Schema, Database, Row-Level Security, and Hybrid — all with automatic DDL and per-connection setup.</p>
  </a>
  <a href="guides/resolution/" class="ft-card">
    <span class="ft-card-icon">🔍</span>
    <h3>Five resolution strategies</h3>
    <p>Header, Subdomain, Path, JWT, and fully custom resolvers via a simple protocol.</p>
  </a>
  <a href="guides/dependencies/" class="ft-card">
    <span class="ft-card-icon">⚡</span>
    <h3>Async-first</h3>
    <p>Built on SQLAlchemy 2.0 async, asyncio-safe LRU engine cache, and non-blocking Redis throughout.</p>
  </a>
  <a href="guides/middleware/" class="ft-card">
    <span class="ft-card-icon">🔒</span>
    <h3>SQL-injection safe</h3>
    <p>Every schema name, database name, and identifier is validated against a strict allowlist before any DDL runs.</p>
  </a>
  <a href="guides/storage/custom/" class="ft-card">
    <span class="ft-card-icon">🦆</span>
    <h3>Protocol-driven</h3>
    <p><code>TenantStore</code>, <code>BaseTenantResolver</code>, and <code>BaseIsolationProvider</code> are runtime-checkable protocols — swap any component freely.</p>
  </a>
  <a href="guides/caching/" class="ft-card">
    <span class="ft-card-icon">🏎️</span>
    <h3>Two-level cache</h3>
    <p>In-process LRU cache with optional Redis write-through layer. Hot tenant lookups cost microseconds, not milliseconds.</p>
  </a>
  <a href="guides/rate-limiting/" class="ft-card">
    <span class="ft-card-icon">🚦</span>
    <h3>Rate limiting</h3>
    <p>Per-tenant sliding-window rate limiting backed by Redis atomic Lua scripts. Zero additional dependencies beyond Redis.</p>
  </a>
  <a href="guides/migrations/" class="ft-card">
    <span class="ft-card-icon">🔄</span>
    <h3>Migrations</h3>
    <p>Bounded-concurrent Alembic migrations across all tenants. Migrate 1 000 tenants in minutes, not hours.</p>
  </a>
  <a href="contributing/" class="ft-card">
    <span class="ft-card-icon">🧪</span>
    <h3>Fully tested</h3>
    <p>753 tests. 95%+ branch coverage. Unit, integration, and end-to-end test suites targeting Python 3.11–3.13.</p>
  </a>
</div>

---

## At a glance

```python
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi_tenancy import TenancyConfig, TenancyManager, TenancyMiddleware
from fastapi_tenancy.dependencies import get_current_tenant, make_tenant_db_dependency
from fastapi_tenancy.storage.database import SQLAlchemyTenantStore

# 1. Configure
config = TenancyConfig(
    database_url="postgresql+asyncpg://user:pass@localhost/myapp",
    resolution_strategy="header",   # read X-Tenant-ID header
    isolation_strategy="schema",    # schema-per-tenant
)

# 2. Wire
store   = SQLAlchemyTenantStore(config.database_url)
manager = TenancyManager(config, store)

# 3. App
@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.initialize()
    yield
    await manager.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(TenancyMiddleware, manager=manager, excluded_paths=["/health"])

# 4. Tenant-scoped session per request
get_db = make_tenant_db_dependency(manager)

@app.get("/orders")
async def list_orders(session: Annotated[AsyncSession, Depends(get_db)]):
    # session is already pointing at the current tenant's schema
    result = await session.execute(select(Order))
    return result.scalars().all()
```

---

## Isolation strategies

<table class="ft-strategy-table">
  <thead>
    <tr>
      <th>Strategy</th>
      <th>Mechanism</th>
      <th>Databases</th>
      <th>Best for</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><code>schema</code></td>
      <td>Dedicated schema per tenant, <code>search_path</code> set per-connection</td>
      <td>PostgreSQL, MSSQL, SQLite*</td>
      <td>Most SaaS apps</td>
    </tr>
    <tr>
      <td><code>database</code></td>
      <td>Separate database per tenant, LRU engine cache</td>
      <td>PostgreSQL, MySQL</td>
      <td>Strict regulatory isolation</td>
    </tr>
    <tr>
      <td><code>rls</code></td>
      <td>Shared tables, PostgreSQL RLS policies, <code>SET LOCAL</code> GUC</td>
      <td>PostgreSQL only</td>
      <td>Large tenant counts</td>
    </tr>
    <tr>
      <td><code>hybrid</code></td>
      <td>Route by tenant tier — premium → schema, standard → rls</td>
      <td>PostgreSQL</td>
      <td>Tiered SaaS products</td>
    </tr>
  </tbody>
</table>

*SQLite falls back to table-name prefixing.

---

## Installation

=== "Core"

    ```bash
    pip install fastapi-tenancy
    ```

=== "PostgreSQL"

    ```bash
    pip install "fastapi-tenancy[postgres]"
    ```

=== "MySQL"

    ```bash
    pip install "fastapi-tenancy[mysql]"
    ```

=== "Full stack"

    ```bash
    pip install "fastapi-tenancy[full]"
    ```

    Installs: `asyncpg` · `redis[hiredis]` · `python-jose[cryptography]` · `alembic`

---

## Links

<div class="ft-grid">
  <a href="getting-started/installation/" class="ft-card">
    <span class="ft-card-icon">📦</span>
    <h3>Installation</h3>
    <p>Extras, async drivers, and optional services explained.</p>
  </a>
  <a href="getting-started/quickstart/" class="ft-card">
    <span class="ft-card-icon">🚀</span>
    <h3>Quick Start</h3>
    <p>Full working application in under 5 minutes.</p>
  </a>
  <a href="api/" class="ft-card">
    <span class="ft-card-icon">📚</span>
    <h3>API Reference</h3>
    <p>Complete auto-generated reference for every class and function.</p>
  </a>
  <a href="https://github.com/fastapi-extensions/fastapi-tenancy" class="ft-card">
    <span class="ft-card-icon">🐙</span>
    <h3>GitHub</h3>
    <p>Source code, issues, and pull requests.</p>
  </a>
</div>
