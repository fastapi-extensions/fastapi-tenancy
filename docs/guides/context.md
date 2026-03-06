---
title: Context Management
description: TenantContext, tenant_scope, and using tenant context in background tasks.
---

# Context Management

fastapi-tenancy uses Python's `contextvars` module to propagate tenant identity across async tasks without locking or thread-local storage. Each async task (i.e. each HTTP request) automatically gets its own copy of the context variable.

## `TenantContext`

`TenantContext` is a namespace of static methods for reading and writing the per-request tenant state:

```python
from fastapi_tenancy.core.context import TenantContext

# Set (returns a token — always reset with the token, don't discard it)
token = TenantContext.set(tenant)

# Get (raises TenantNotFoundError if not set)
tenant = TenantContext.get()

# Get optional (returns None if not set)
tenant = TenantContext.get_optional()

# Restore previous state
TenantContext.reset(token)

# Clear unconditionally (use reset(token) instead in most cases)
TenantContext.clear()
```

The middleware sets the tenant at the start of every request and resets it at the end — always using `reset(token)` in a `finally` block:

```python
token = TenantContext.set(tenant)
try:
    await app(scope, receive, send)
finally:
    TenantContext.reset(token)  # restores, not clears
```

## Request-scoped metadata

You can attach arbitrary metadata to the current request context without threading it through every function signature:

```python
from fastapi_tenancy.core.context import TenantContext

# In middleware or an early dependency
TenantContext.set_metadata("request_id", str(uuid4()))
TenantContext.set_metadata("user_id", current_user.id)

# In a deeply nested function
request_id = TenantContext.get_metadata("request_id")
user_id    = TenantContext.get_metadata("user_id", default=None)
all_meta   = TenantContext.get_all_metadata()
```

Metadata is isolated per request just like the tenant — it is never shared between concurrent requests.

## `tenant_scope` — background tasks

The `tenant_scope` async context manager is the recommended pattern for setting tenant context in background tasks, periodic jobs, and tests:

```python
from fastapi_tenancy.core.context import tenant_scope

async def send_welcome_email(tenant_id: str):
    tenant = await store.get_by_id(tenant_id)
    async with tenant_scope(tenant) as t:
        # TenantContext.get() works inside this block
        session = await get_session_for_tenant(t)
        user = await session.get(User, ...)
        await email.send(user.email, "Welcome!")
    # Previous context (usually None) is restored here

# FastAPI BackgroundTask
from fastapi import BackgroundTasks

@app.post("/register")
async def register(
    background_tasks: BackgroundTasks,
    tenant: TenantDep,
):
    background_tasks.add_task(send_welcome_email, tenant.id)
    return {"status": "registered"}
```

!!! warning "Background tasks run after response"
    FastAPI's `BackgroundTask` runs after the response is sent, when the
    middleware's `finally` block has already reset `TenantContext` to `None`.
    Always use `tenant_scope()` in background tasks — don't rely on the
    request context still being set.

## Nested scopes

`tenant_scope` is safe to nest — each scope restores the outer scope's tenant on exit:

```python
async with tenant_scope(tenant_a):
    assert TenantContext.get() is tenant_a

    async with tenant_scope(tenant_b):
        assert TenantContext.get() is tenant_b

    assert TenantContext.get() is tenant_a  # restored

# TenantContext.get() raises here — no outer scope
```

## Periodic jobs (APScheduler, Celery)

```python
# APScheduler example
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi_tenancy.core.context import tenant_scope

scheduler = AsyncIOScheduler()

@scheduler.scheduled_job("interval", hours=1)
async def hourly_report():
    tenants = await store.list(status=TenantStatus.ACTIVE)
    for tenant in tenants:
        async with tenant_scope(tenant):
            await generate_report(tenant)
```

## Testing with context

```python
import pytest
from fastapi_tenancy.core.context import tenant_scope
from fastapi_tenancy.core.types import Tenant

@pytest.fixture
def test_tenant():
    return Tenant(id="t-test", identifier="test-corp", name="Test Corp")

async def test_something_with_context(test_tenant):
    async with tenant_scope(test_tenant):
        result = await my_business_logic()
        assert result.tenant_id == test_tenant.id
```
