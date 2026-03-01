---
title: In-Memory Tenant Store
description: Fast in-process store for unit tests and local development.
---

# In-Memory Tenant Store

`InMemoryTenantStore` keeps all tenant data in Python dictionaries. It requires no database and is ideal for unit tests, local prototyping, and CI environments where spinning up a real database is unnecessary.

!!! warning "Not for production"
    All data is **lost when the process exits**. Use `SQLAlchemyTenantStore` for production.

## Usage

```python
from fastapi_tenancy.storage.memory import InMemoryTenantStore
from fastapi_tenancy.core.types import Tenant

store = InMemoryTenantStore()

# No initialize() needed — it's always ready
tenant = await store.create(Tenant(
    id="t-001",
    identifier="acme-corp",
    name="Acme Corporation",
))
```

## Pytest fixture

```python
import pytest
from fastapi_tenancy.storage.memory import InMemoryTenantStore
from fastapi_tenancy.core.types import Tenant

@pytest.fixture
def store():
    s = InMemoryTenantStore()
    return s

@pytest.fixture
async def tenant(store):
    return await store.create(Tenant(
        id="t-test",
        identifier="test-corp",
        name="Test Corp",
    ))
```

## Seeding tenants

```python
store = InMemoryTenantStore()
for i in range(10):
    await store.create(Tenant(
        id=f"t-{i:03d}",
        identifier=f"tenant-{i:03d}",
        name=f"Tenant {i:03d}",
    ))

tenants = await store.list()
assert len(tenants) == 10
```

## Performance

All reads are O(1) — two dictionaries (`id → Tenant` and `identifier → tenant_id`) are kept in sync for constant-time lookups. This means even test suites with thousands of tenants run without meaningful overhead.
