---
title: Dependency Injection
description: Tenant-scoped session, config, and audit-log FastAPI dependencies.
---

# Dependency Injection

fastapi-tenancy provides three **closure-based dependency factories** that create FastAPI `Depends`-compatible functions. All dependencies capture `TenancyManager` in their closure — no `app.state` lookups, no circular imports.

## Tenant-scoped database session

The most common dependency: yields an `AsyncSession` pointing at the current tenant's isolation namespace.

```python
from fastapi_tenancy.dependencies import make_tenant_db_dependency

# Create once at startup
get_tenant_db = make_tenant_db_dependency(manager)

# Use in routes
@app.get("/orders")
async def list_orders(
    session: Annotated[AsyncSession, Depends(get_tenant_db)],
):
    result = await session.execute(select(Order))
    return result.scalars().all()
```

The session is automatically committed/rolled-back and closed via the `async with` context manager wrapping `isolation_provider.get_session()`.

## Current tenant

`get_current_tenant` is a standalone dependency (no factory needed):

```python
from fastapi_tenancy.dependencies import get_current_tenant

@app.get("/me")
async def me(tenant: Annotated[Tenant, Depends(get_current_tenant)]):
    return {"id": tenant.id, "name": tenant.name}
```

### Optional tenant

For endpoints that serve both anonymous and tenant-scoped requests:

```python
from fastapi_tenancy.dependencies import get_current_tenant_optional

@app.get("/status")
async def status(
    tenant: Annotated[Tenant | None, Depends(get_current_tenant_optional)],
):
    if tenant:
        return {"status": "tenant", "id": tenant.id}
    return {"status": "anonymous"}
```

## Annotated type aliases

Reusable type aliases reduce boilerplate across your routes:

```python
from typing import Annotated
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi_tenancy.dependencies import (
    get_current_tenant,
    get_current_tenant_optional,
    make_tenant_db_dependency,
)

# Define once
TenantDep         = Annotated[Tenant, Depends(get_current_tenant)]
TenantOptionalDep = Annotated[Tenant | None, Depends(get_current_tenant_optional)]
SessionDep        = Annotated[AsyncSession, Depends(make_tenant_db_dependency(manager))]

# Use everywhere
@app.get("/orders")
async def list_orders(
    tenant: TenantDep,
    session: SessionDep,
):
    ...
```

## Tenant config

The `make_tenant_config_dependency` factory creates a dependency that reads `tenant.metadata` and returns a typed `TenantConfig`:

```python
from fastapi_tenancy.dependencies import make_tenant_config_dependency

get_tenant_config = make_tenant_config_dependency(manager)

@app.get("/quota")
async def quota(config: Annotated[TenantConfig, Depends(get_tenant_config)]):
    return {
        "max_users": config.max_users,
        "rate_limit": config.rate_limit_per_minute,
        "features": config.features_enabled,
    }
```

The metadata fields are validated against `TenantConfig` defaults, so missing keys fall back to defaults without raising.

## Audit log

The `make_audit_log_dependency` factory returns a callable that records structured audit entries:

```python
from fastapi_tenancy.dependencies import make_audit_log_dependency

get_audit = make_audit_log_dependency(manager)

@app.delete("/orders/{order_id}")
async def delete_order(
    order_id: str,
    tenant: TenantDep,
    session: SessionDep,
    audit: Annotated[Any, Depends(get_audit)],
):
    order = await session.get(Order, order_id)
    await session.delete(order)
    await session.commit()

    await audit(
        action="delete",
        resource="order",
        resource_id=order_id,
        user_id="user-123",
        metadata={"reason": "user request"},
    )
    return {"deleted": order_id}
```

## Combining dependencies

All three dependencies work together in the same route:

```python
@app.post("/users")
async def create_user(
    body: UserCreate,
    tenant: TenantDep,
    session: SessionDep,
    config: Annotated[TenantConfig, Depends(get_tenant_config)],
    audit: Annotated[Any, Depends(get_audit)],
):
    if config.max_users:
        current = await session.scalar(select(func.count(User.id)))
        if current >= config.max_users:
            raise HTTPException(
                status_code=402,
                detail=f"User limit ({config.max_users}) reached",
            )

    user = User(email=body.email, tenant_id=tenant.id)
    session.add(user)
    await session.commit()

    await audit(action="create", resource="user", resource_id=user.id)
    return user
```
