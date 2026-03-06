---
title: Testing
description: Unit, integration, and end-to-end test patterns for multi-tenant FastAPI applications.
---

# Testing

fastapi-tenancy is designed for testability. `InMemoryTenantStore` eliminates database dependencies for unit tests, and `tenant_scope()` gives precise control over tenant context in every scenario.

## Unit tests — no database

Use `InMemoryTenantStore` and `SQLite` in-memory for fast, isolated tests:

```python
import pytest
import pytest_asyncio
from fastapi_tenancy import TenancyConfig, TenancyManager, TenancyMiddleware, Tenant
from fastapi_tenancy.storage.memory import InMemoryTenantStore

@pytest_asyncio.fixture
async def store():
    s = InMemoryTenantStore()
    yield s

@pytest_asyncio.fixture
async def tenant(store):
    return await store.create(Tenant(
        id="t-test",
        identifier="test-corp",
        name="Test Corporation",
    ))

@pytest_asyncio.fixture
async def config():
    return TenancyConfig(
        database_url="sqlite+aiosqlite:///:memory:",
        resolution_strategy="header",
        isolation_strategy="schema",
    )

@pytest_asyncio.fixture
async def manager(config, store):
    m = TenancyManager(config, store)
    await m.initialize()
    yield m
    await m.close()
```

## Testing routes with the HTTP client

```python
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI

@pytest_asyncio.fixture
async def app(manager):
    application = FastAPI()
    application.add_middleware(TenancyMiddleware, manager=manager, excluded_paths=["/health"])
    # ... add routes ...
    return application

@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c

async def test_list_orders(client, tenant):
    response = await client.get(
        "/orders",
        headers={"X-Tenant-ID": tenant.identifier},
    )
    assert response.status_code == 200
    assert response.json()["tenant"] == tenant.identifier
```

## Testing with `tenant_scope`

When you need to test business logic that calls `TenantContext.get()` directly:

```python
from fastapi_tenancy.core.context import tenant_scope

async def test_tenant_aware_function(tenant):
    async with tenant_scope(tenant):
        result = await my_business_logic()
        assert result.tenant_id == tenant.id
```

## Testing error scenarios

```python
async def test_unknown_tenant(client):
    response = await client.get(
        "/orders",
        headers={"X-Tenant-ID": "does-not-exist"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Tenant not found"

async def test_missing_tenant_header(client):
    response = await client.get("/orders")  # no header
    assert response.status_code == 400

async def test_suspended_tenant(client, store, tenant):
    await store.set_status(tenant.id, TenantStatus.SUSPENDED)
    response = await client.get(
        "/orders",
        headers={"X-Tenant-ID": tenant.identifier},
    )
    assert response.status_code == 403
    assert "suspended" in response.json()["detail"]
```

## Integration tests — real database

For integration tests, use a real database (started via Docker Compose):

```python
import os
import pytest

POSTGRES_URL = os.getenv(
    "POSTGRES_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/test_tenancy"
)

@pytest_asyncio.fixture(scope="session")
async def integration_store():
    from fastapi_tenancy.storage.database import SQLAlchemyTenantStore
    store = SQLAlchemyTenantStore(POSTGRES_URL)
    await store.initialize()
    yield store
    await store.close()
```

```bash
# Start test infrastructure
docker compose -f docker-compose.test.yml up -d

# Run integration tests
pytest tests/integration/ -m integration
```

## Coverage

The project enforces ≥95% branch coverage:

```bash
make coverage     # runs full suite and opens HTML report
make test         # unit tests only (no Docker)
make test-int     # starts Docker, runs integration tests
```

## Pytest markers

```python
# Mark slow tests
@pytest.mark.slow
async def test_migrate_all_tenants():
    ...

# Skip without database
@pytest.mark.integration
async def test_postgres_rls():
    ...
```

Register them in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
markers = [
    "unit: Fast unit tests with no I/O",
    "integration: Integration tests (requires database)",
    "e2e: End-to-end tests (full stack)",
    "slow: Tests that take > 1 s",
]
```
