---
title: Custom Tenant Store
description: Implement TenantStore to use any data source for tenant metadata.
---

# Custom Tenant Store

Subclass `TenantStore` to use any data source: a legacy database, a microservice registry, an external API, or any combination.

## Minimal implementation

```python
from fastapi_tenancy.storage.tenant_store import TenantStore
from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.core.exceptions import TenantNotFoundError
from collections.abc import Sequence

class MyCustomStore(TenantStore[Tenant]):

    async def get_by_id(self, tenant_id: str) -> Tenant:
        row = await my_db.fetch_one("SELECT * FROM tenants WHERE id = $1", tenant_id)
        if not row:
            raise TenantNotFoundError(tenant_id)
        return _row_to_tenant(row)

    async def get_by_identifier(self, identifier: str) -> Tenant:
        row = await my_db.fetch_one("SELECT * FROM tenants WHERE identifier = $1", identifier)
        if not row:
            raise TenantNotFoundError(identifier)
        return _row_to_tenant(row)

    async def create(self, tenant: Tenant) -> Tenant:
        await my_db.execute(
            "INSERT INTO tenants (id, identifier, name, status) VALUES ($1,$2,$3,$4)",
            tenant.id, tenant.identifier, tenant.name, tenant.status,
        )
        return tenant

    async def update(self, tenant: Tenant) -> Tenant:
        await my_db.execute(
            "UPDATE tenants SET name=$1, status=$2 WHERE id=$3",
            tenant.name, tenant.status, tenant.id,
        )
        return tenant

    async def delete(self, tenant_id: str) -> None:
        await my_db.execute("DELETE FROM tenants WHERE id = $1", tenant_id)

    async def list(self, skip=0, limit=100, status=None) -> Sequence[Tenant]:
        q = "SELECT * FROM tenants"
        if status:
            q += f" WHERE status = '{status}'"
        q += f" ORDER BY created_at DESC LIMIT {limit} OFFSET {skip}"
        rows = await my_db.fetch_all(q)
        return [_row_to_tenant(r) for r in rows]

    async def count(self, status=None) -> int:
        q = "SELECT COUNT(*) FROM tenants"
        if status:
            q += f" WHERE status = '{status}'"
        return await my_db.fetch_val(q)

    async def exists(self, tenant_id: str) -> bool:
        result = await my_db.fetch_val(
            "SELECT 1 FROM tenants WHERE id = $1", tenant_id
        )
        return result is not None

    async def set_status(self, tenant_id: str, status: TenantStatus) -> Tenant:
        await my_db.execute(
            "UPDATE tenants SET status = $1 WHERE id = $2", status, tenant_id
        )
        return await self.get_by_id(tenant_id)

    async def update_metadata(self, tenant_id: str, metadata: dict) -> Tenant:
        tenant = await self.get_by_id(tenant_id)
        merged = {**tenant.metadata, **metadata}
        await my_db.execute(
            "UPDATE tenants SET metadata = $1 WHERE id = $2",
            json.dumps(merged), tenant_id
        )
        return await self.get_by_id(tenant_id)
```

## Override batch methods

The default batch implementations use N+1 calls. Override them to issue single queries:

```python
async def get_by_ids(self, tenant_ids) -> list[Tenant]:
    rows = await my_db.fetch_all(
        "SELECT * FROM tenants WHERE id = ANY($1)", list(tenant_ids)
    )
    return [_row_to_tenant(r) for r in rows]

async def bulk_update_status(self, tenant_ids, status) -> list[Tenant]:
    ids = list(tenant_ids)
    await my_db.execute(
        "UPDATE tenants SET status = $1 WHERE id = ANY($2)", status, ids
    )
    return await self.get_by_ids(ids)
```

## Rules

1. Methods that look up a single tenant must **raise `TenantNotFoundError`**, never return `None`
2. All methods must be **async** — even if they don't do I/O (e.g. InMemoryStore)
3. The store instance is **shared across all requests** — it must be concurrency-safe
4. Wrap unexpected errors in `TenancyError` with a `details` dict safe to log
