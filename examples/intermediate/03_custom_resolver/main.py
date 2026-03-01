"""
Intermediate Example 3 — Custom Resolver: API Key Authentication
=================================================================
Identify tenants by a pre-shared API key in the X-API-Key header.

Use case: machine-to-machine integrations where JWT or header slugs are
impractical.  Each tenant is assigned a hashed API key that maps to their
tenant record.

What you'll learn
-----------------
- Implementing the TenantResolver protocol (duck typing, no inheritance required)
- Injecting a custom resolver into TenancyManager
- Layering security concerns (key validation) with tenant resolution
- API key rotation pattern

Run
---
    pip install "fastapi-tenancy" "fastapi[standard]"
    uvicorn main:app --reload

Test
----
    # Tenant acme-corp has key: "key-acme-ABCDEF"
    curl http://localhost:8000/data \\
         -H "X-API-Key: key-acme-ABCDEF"

    # Tenant globex has key: "key-globex-123456"
    curl http://localhost:8000/data \\
         -H "X-API-Key: key-globex-123456"

    # Missing key → 400
    curl http://localhost:8000/data

    # Invalid key → 404
    curl http://localhost:8000/data -H "X-API-Key: wrong-key"
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
from typing import TYPE_CHECKING

from fastapi import FastAPI

from fastapi_tenancy import TenancyConfig, TenancyManager, TenancyMiddleware, Tenant
from fastapi_tenancy.core.exceptions import TenantResolutionError
from fastapi_tenancy.storage.memory import InMemoryTenantStore

if TYPE_CHECKING:
    from starlette.requests import Request

    from fastapi_tenancy.dependencies import TenantDep
    from fastapi_tenancy.storage.tenant_store import TenantStore


# ── Custom Resolver ───────────────────────────────────────────────────────────

class ApiKeyTenantResolver:
    """
    Resolves the current tenant from the X-API-Key request header.

    In real production code, API keys would be stored (hashed) in the same
    database as the tenants.  Here we keep a simple in-memory lookup for
    illustration.

    This class satisfies the TenantResolver protocol without inheriting from
    BaseTenantResolver — fastapi-tenancy uses structural (duck-type) checking.
    """

    def __init__(self, store: TenantStore[Tenant]) -> None:
        self._store = store
        # Map SHA-256(api_key) → tenant_identifier
        # In production: store hashed keys in DB alongside tenant records.
        self._key_map: dict[str, str] = {}

    def register_key(self, api_key: str, tenant_identifier: str) -> None:
        """Associate an API key with a tenant identifier."""
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        self._key_map[key_hash] = tenant_identifier

    async def resolve(self, request: Request) -> Tenant:
        """
        Extract and validate the API key, then look up the tenant.

        Raises:
            TenantResolutionError: Key missing or invalid hash.
            TenantNotFoundError: Key is valid but tenant not in store.
        """
        raw_key = request.headers.get("X-API-Key", "").strip()
        if not raw_key:
            raise TenantResolutionError(
                reason="X-API-Key header is required",
                strategy="api_key",
            )

        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        identifier = self._key_map.get(key_hash)
        if identifier is None:
            raise TenantResolutionError(
                reason="Invalid API key",
                strategy="api_key",
            )

        return await self._store.get_by_identifier(identifier)


# ── App setup ─────────────────────────────────────────────────────────────────

store    = InMemoryTenantStore()
resolver = ApiKeyTenantResolver(store)

# Register demo API keys (in production: load from DB)
resolver.register_key("key-acme-ABCDEF", "acme-corp")
resolver.register_key("key-globex-123456", "globex")

config = TenancyConfig(
    database_url="sqlite+aiosqlite:///:memory:",
    resolution_strategy="custom",    # ← signals we're providing our own resolver
    isolation_strategy="schema",
)

manager = TenancyManager(
    config,
    store,
    custom_resolver=resolver,        # ← inject our custom resolver
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.create(Tenant(id="t1", identifier="acme-corp", name="Acme Corp",  metadata={"quota": 10_000}))  # noqa: E501
    await store.create(Tenant(id="t2", identifier="globex",    name="Globex Inc.", metadata={"quota": 1_000}))  # noqa: E501
    yield


app = FastAPI(title="API Key Resolver Demo", lifespan=lifespan)
app.add_middleware(
    TenancyMiddleware,
    manager=manager,
    excluded_paths=["/health", "/docs", "/openapi.json"],
)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/data")
async def get_data(tenant: TenantDep):
    """Return tenant-scoped data — resolved from X-API-Key header."""
    return {
        "tenant": tenant.identifier,
        "name": tenant.name,
        "quota": tenant.metadata.get("quota"),
        "message": f"Successfully authenticated as {tenant.name} via API key",
    }


@app.get("/me")
async def me(tenant: TenantDep):
    return tenant.model_dump_safe()
