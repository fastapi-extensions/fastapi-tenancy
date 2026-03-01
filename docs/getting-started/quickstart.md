---
title: Quick Start
description: Build a working multi-tenant FastAPI application in under 5 minutes.
---

# Quick Start

This guide walks you through a complete, runnable multi-tenant FastAPI
application from scratch.

## 1. Install

```bash
pip install "fastapi-tenancy[postgres]"
```

## 2. Configure

Create `main.py`:

```python title="main.py"
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from fastapi_tenancy import TenancyConfig, TenancyManager, TenancyMiddleware, Tenant
from fastapi_tenancy.dependencies import get_current_tenant, make_tenant_db_dependency
from fastapi_tenancy.storage.database import SQLAlchemyTenantStore

# ── Database models ──────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass

class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    description: Mapped[str] = mapped_column()

# ── fastapi-tenancy setup ────────────────────────────────────────────────────

config = TenancyConfig(
    database_url="postgresql+asyncpg://postgres:postgres@localhost/myapp",
    resolution_strategy="header",   # (1)
    isolation_strategy="schema",    # (2)
)

store   = SQLAlchemyTenantStore(config.database_url)
manager = TenancyManager(config, store)

# ── FastAPI app ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.initialize()           # (3)
    yield
    await manager.close()

app = FastAPI(title="My SaaS App", lifespan=lifespan)

app.add_middleware(
    TenancyMiddleware,
    manager=manager,
    excluded_paths=["/health"],          # (4)
)

# ── Dependency ───────────────────────────────────────────────────────────────

get_db = make_tenant_db_dependency(manager)  # (5)

# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/orders")
async def list_orders(
    tenant: Annotated[Tenant, Depends(get_current_tenant)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    result = await session.execute(select(Order))
    orders = result.scalars().all()
    return {
        "tenant": tenant.identifier,
        "orders": [{"id": o.id, "description": o.description} for o in orders],
    }

@app.post("/orders")
async def create_order(
    description: str,
    session: Annotated[AsyncSession, Depends(get_db)],
):
    order = Order(description=description)
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return {"id": order.id, "description": order.description}
```

1. Extract the tenant identifier from the `X-Tenant-ID` request header.
2. Each tenant gets a dedicated PostgreSQL schema (e.g. `tenant_acme_corp`).
3. Creates the `tenants` table and warms the connection pool.
4. Exclude `/health` from tenant resolution so load balancers can probe it.
5. Creates a FastAPI dependency that yields a tenant-scoped `AsyncSession`.

## 3. Register your first tenant

Start the server and register a tenant using the manager's API:

```python title="seed.py"
import asyncio
from main import manager, Base

async def seed():
    await manager.initialize()

    # Register a tenant (creates the schema + all Base tables inside it)
    tenant = await manager.register_tenant(
        identifier="acme-corp",
        name="Acme Corporation",
        app_metadata=Base.metadata,  # creates tables in the new schema
    )
    print(f"Created: {tenant.identifier} → schema: tenant_acme_corp")

    await manager.close()

asyncio.run(seed())
```

```bash
python seed.py
# Created: acme-corp → schema: tenant_acme_corp
```

## 4. Make your first request

```bash
# Create an order for acme-corp
curl -X POST "http://localhost:8000/orders?description=First+order" \
     -H "X-Tenant-ID: acme-corp"

# List orders — only acme-corp's data
curl "http://localhost:8000/orders" \
     -H "X-Tenant-ID: acme-corp"
```

```json
{
  "tenant": "acme-corp",
  "orders": [{"id": 1, "description": "First order"}]
}
```

## 5. Error responses

The middleware returns structured JSON errors automatically:

| Situation | HTTP status | Response |
|-----------|-------------|----------|
| Missing `X-Tenant-ID` header | `400` | `{"detail": "Tenant identifier missing"}` |
| Unknown tenant identifier | `404` | `{"detail": "Tenant not found"}` |
| Suspended or deleted tenant | `403` | `{"detail": "Tenant is not active (status: suspended)"}` |
| Rate limit exceeded | `429` | `{"detail": "Rate limit exceeded for tenant ..."}` |

## Environment variables

Instead of hardcoding the database URL, use environment variables:

```bash
export TENANCY_DATABASE_URL="postgresql+asyncpg://user:pass@localhost/myapp"
export TENANCY_RESOLUTION_STRATEGY="header"
export TENANCY_ISOLATION_STRATEGY="schema"
```

```python
config = TenancyConfig()  # reads from environment automatically
```

Every `TenancyConfig` field has a corresponding `TENANCY_<FIELD>` environment
variable. See [Configuration](configuration.md) for the full list.

## Next steps

<div class="ft-grid">
  <a href="../guides/isolation/" class="ft-card">
    <span class="ft-card-icon">🗄️</span>
    <h3>Isolation strategies</h3>
    <p>Choose the right isolation model for your data security requirements.</p>
  </a>
  <a href="../guides/resolution/" class="ft-card">
    <span class="ft-card-icon">🔍</span>
    <h3>Resolution strategies</h3>
    <p>Identify tenants by header, subdomain, path, JWT, or custom logic.</p>
  </a>
  <a href="../guides/storage/" class="ft-card">
    <span class="ft-card-icon">💾</span>
    <h3>Tenant storage</h3>
    <p>Choose between SQLAlchemy, Redis, in-memory, or a custom store.</p>
  </a>
  <a href="../guides/testing/" class="ft-card">
    <span class="ft-card-icon">🧪</span>
    <h3>Testing</h3>
    <p>Patterns for writing fast, isolated unit and integration tests.</p>
  </a>
</div>
