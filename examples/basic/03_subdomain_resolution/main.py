"""
Basic Example 3 — Subdomain Resolution
========================================
Identify tenants from the subdomain: acme-corp.myapp.com → tenant "acme-corp".

What you'll learn
-----------------
- SubdomainTenantResolver configuration
- domain_suffix handling
- How to test subdomain resolution locally using the Host header

Run
---
    pip install "fastapi-tenancy"
    pip install "fastapi[standard]"
    uvicorn main:app --reload

Test (simulating subdomains via Host header)
----
    # Simulate acme-corp.localhost.local
    curl http://localhost:8000/ \\
         -H "Host: acme-corp.localhost.local"

    # Simulate globex.localhost.local
    curl http://localhost:8000/ \\
         -H "Host: globex.localhost.local"

    # No subdomain → 400
    curl http://localhost:8000/ -H "Host: localhost.local"

    # Unknown subdomain → 404
    curl http://localhost:8000/ -H "Host: unknown.localhost.local"
"""
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI

from fastapi_tenancy import TenancyConfig, TenancyManager, TenancyMiddleware, Tenant
from fastapi_tenancy.dependencies import get_current_tenant
from fastapi_tenancy.storage.memory import InMemoryTenantStore

config = TenancyConfig(
    database_url="sqlite+aiosqlite:///:memory:",
    resolution_strategy="subdomain",    # ← identify tenant from subdomain
    isolation_strategy="schema",
    # domain_suffix tells the resolver what the base domain is.
    # Everything before the first dot is the tenant identifier.
    domain_suffix=".localhost.local",
)

store   = InMemoryTenantStore()
manager = TenancyManager(config, store)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.create(Tenant(id="t1", identifier="acme-corp",  name="Acme Corp"))
    await store.create(Tenant(id="t2", identifier="globex",     name="Globex Inc."))
    await store.create(Tenant(id="t3", identifier="initech",    name="Initech"))
    yield


app = FastAPI(title="Subdomain Resolution Demo", lifespan=lifespan)
app.add_middleware(
    TenancyMiddleware,
    manager=manager,
    excluded_paths=["/health"],
)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root(tenant: Annotated[Tenant, Depends(get_current_tenant)]):
    return {
        "tenant": tenant.identifier,
        "name": tenant.name,
        "message": f"Welcome to {tenant.name}'s portal!",
    }
