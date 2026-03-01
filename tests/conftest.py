"""Shared pytest fixtures for the fastapi-tenancy test suite.

Hierarchy
---------
tenant_factory          callable that builds Tenant objects with sensible defaults
mem_store               fresh InMemoryTenantStore per test
acme                    one active Tenant seeded into mem_store
globex                  second active Tenant seeded into mem_store
sqlite_store            SQLAlchemyTenantStore backed by SQLite :memory:
header_config           TenancyConfig wired for HEADER resolution + SCHEMA isolation
manager                 TenancyManager with InMemoryTenantStore
asgi_app                minimal FastAPI + TenancyMiddleware
http_client             httpx.AsyncClient → asgi_app, pre-loaded with acme header
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
import uuid

from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio

from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.context import TenantContext
from fastapi_tenancy.core.types import IsolationStrategy, ResolutionStrategy, Tenant, TenantStatus
from fastapi_tenancy.manager import TenancyManager
from fastapi_tenancy.middleware.tenancy import TenancyMiddleware
from fastapi_tenancy.storage.database import SQLAlchemyTenantStore
from fastapi_tenancy.storage.memory import InMemoryTenantStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

##################
# Tenant factory #
##################


@pytest.fixture
def tenant_factory():
    """Return a factory that produces unique Tenant objects."""
    counter = [0]

    def _make(
        *,
        identifier: str | None = None,
        name: str | None = None,
        status: TenantStatus = TenantStatus.ACTIVE,
        isolation_strategy: IsolationStrategy | None = None,
        metadata: dict[str, Any] | None = None,
        tenant_id: str | None = None,
    ) -> Tenant:
        counter[0] += 1
        n = counter[0]
        ts = datetime.now(UTC)
        return Tenant(
            id=tenant_id or f"t-{uuid.uuid4().hex[:16]}",
            identifier=identifier or f"tenant-{n:04d}",
            name=name or f"Test Tenant {n}",
            status=status,
            isolation_strategy=isolation_strategy,
            metadata=metadata or {},
            created_at=ts,
            updated_at=ts,
        )

    return _make


###################
# In-memory store #
###################


@pytest.fixture
def mem_store() -> InMemoryTenantStore:
    return InMemoryTenantStore()


@pytest_asyncio.fixture
async def acme(mem_store: InMemoryTenantStore, tenant_factory) -> Tenant:
    t = tenant_factory(
        identifier="acme-corp",
        name="Acme Corporation",
        metadata={"plan": "enterprise", "max_users": 500},
    )
    return await mem_store.create(t)


@pytest_asyncio.fixture
async def globex(mem_store: InMemoryTenantStore, tenant_factory) -> Tenant:
    t = tenant_factory(identifier="globex-inc", name="Globex Inc")
    return await mem_store.create(t)


################
# SQLite store #
################


@pytest_asyncio.fixture
async def sqlite_store() -> AsyncIterator[SQLAlchemyTenantStore]:
    s = SQLAlchemyTenantStore("sqlite+aiosqlite:///:memory:")
    await s.initialize()
    yield s
    await s.close()


###########
# Configs #
###########


@pytest.fixture
def header_config() -> TenancyConfig:
    return TenancyConfig(
        database_url="sqlite+aiosqlite:///:memory:",
        resolution_strategy=ResolutionStrategy.HEADER,
        isolation_strategy=IsolationStrategy.SCHEMA,
        tenant_header_name="X-Tenant-ID",
    )


###########
# Manager #
###########


@pytest_asyncio.fixture
async def manager(
    header_config: TenancyConfig,
    mem_store: InMemoryTenantStore,
) -> AsyncIterator[TenancyManager]:
    m = TenancyManager(header_config, mem_store)
    await m.initialize()
    yield m
    await m.close()


##########################
# ASGI app + HTTP client #
##########################


@pytest_asyncio.fixture
async def asgi_app(manager: TenancyManager):
    """Return a minimal FastAPI app wrapped in TenancyMiddleware."""
    from fastapi import FastAPI  # noqa: PLC0415

    from fastapi_tenancy.core.context import get_current_tenant  # noqa: PLC0415

    app = FastAPI()
    app.add_middleware(
        TenancyMiddleware,
        manager=manager,
        excluded_paths=["/health", "/docs", "/openapi.json"],
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/tenant")
    async def whoami():
        t = get_current_tenant()
        return {"id": t.id, "identifier": t.identifier, "status": t.status}

    @app.get("/meta")
    async def meta():
        return TenantContext.get_all_metadata()

    return app


@pytest_asyncio.fixture
async def http_client(
    asgi_app,
    acme: Tenant,
) -> AsyncIterator[AsyncClient]:
    """Return AsyncClient pre-configured with the acme tenant header."""
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app),
        base_url="http://testserver",
        headers={"X-Tenant-ID": acme.identifier},
    ) as client:
        yield client
