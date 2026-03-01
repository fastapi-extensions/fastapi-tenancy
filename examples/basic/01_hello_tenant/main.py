"""
Basic Example 1 — Hello Tenant
================================
The simplest possible multi-tenant FastAPI application.

What you'll learn
-----------------
- Install fastapi-tenancy
- Configure TenancyConfig with in-memory storage (no database needed)
- Add TenancyMiddleware to your FastAPI app
- Inject the current Tenant into a route
- Bootstrap seed tenants at startup

Run
---
    pip install "fastapi-tenancy"
    pip install "fastapi[standard]"
    uvicorn main:app --reload

Test
----
    # Health check (no tenant needed)
    curl http://localhost:8001/health

    # Greet acme-corp
    curl http://localhost:8001/hello -H "X-Tenant-ID: acme-corp"

    # Greet globex
    curl http://localhost:8001/hello -H "X-Tenant-ID: globex"

    # Missing header → 400
    curl http://localhost:8001/hello

    # Unknown tenant → 404
    curl http://localhost:8001/hello -H "X-Tenant-ID: no-such-tenant"
"""
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI

from fastapi_tenancy import TenancyConfig, TenancyManager, TenancyMiddleware, Tenant
from fastapi_tenancy.dependencies import get_current_tenant
from fastapi_tenancy.storage.memory import InMemoryTenantStore

# ── 1. Configuration ──────────────────────────────────────────────────────────
#
# database_url is required by TenancyConfig even when using InMemoryTenantStore
# because the isolation provider still needs a DB URL.  For pure in-memory
# demos you can use SQLite in-memory.
#
config = TenancyConfig(
    database_url="sqlite+aiosqlite:///:memory:",
    resolution_strategy="header",   # read X-Tenant-ID header
    isolation_strategy="schema",    # not actually used — no real DB here
)

# ── 2. Store & Manager ────────────────────────────────────────────────────────
#
# InMemoryTenantStore keeps everything in Python dicts — perfect for demos,
# tests, and local development.  Data is lost when the process restarts.
#
store   = InMemoryTenantStore()
manager = TenancyManager(config, store)

# ── 3. App + lifespan ─────────────────────────────────────────────────────────
#
# Use the manager's create_lifespan() helper — it calls initialize() on enter
# and close() on exit automatically.

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Seed two demo tenants so you can test immediately.
    await store.create(Tenant(
        id="tid-acme",
        identifier="acme-corp",
        name="Acme Corporation",
        metadata={"plan": "enterprise", "emoji": "🏭"},
    ))
    await store.create(Tenant(
        id="tid-globex",
        identifier="globex",
        name="Globex Inc.",
        metadata={"plan": "starter", "emoji": "🌐"},
    ))
    print("✅ Seeded 2 tenants — ready to receive requests")
    yield
    # In-memory store: nothing to close; manager.close() is a no-op here.

app = FastAPI(
    title="Hello Tenant — Basic Example",
    description="Simplest possible multi-tenant FastAPI app.",
    lifespan=lifespan,
)

# ── 4. Middleware ─────────────────────────────────────────────────────────────
#
# TenancyMiddleware intercepts every request, reads X-Tenant-ID, validates
# the tenant exists and is ACTIVE, then stores the Tenant in a ContextVar.
# Routes/dependencies downstream can read it via get_current_tenant().
#
app.add_middleware(
    TenancyMiddleware,
    manager=manager,
    excluded_paths=[
        "/health", "/docs", "/openapi.json", "/favicon.ico"
    ],  # these bypass tenant resolution
)

# ── 5. Routes ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """No tenant needed — used by load-balancers and CI checks."""
    return {"status": "ok"}


@app.get("/hello")
async def hello(
    tenant: Annotated[Tenant, Depends(get_current_tenant)],
):
    """Return a greeting personalised to the current tenant.

    The Tenant object is injected automatically — no header parsing in the
    route handler at all.
    """
    emoji = tenant.metadata.get("emoji", "👋")
    plan  = tenant.metadata.get("plan", "unknown")
    return {
        "message": f"Hello, {tenant.name}! {emoji}",
        "tenant_id": tenant.identifier,
        "plan": plan,
    }


@app.get("/me")
async def me(tenant: Annotated[Tenant, Depends(get_current_tenant)]):
    """Return the full tenant record (safe version — no DB URLs exposed)."""
    return tenant.model_dump_safe()
