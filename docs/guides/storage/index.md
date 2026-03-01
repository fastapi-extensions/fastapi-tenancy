---
title: Tenant Storage
description: Where and how fastapi-tenancy persists tenant metadata.
---

# Tenant Storage

A **tenant store** is responsible for persisting and querying tenant metadata — the `identifier`, `name`, `status`, `metadata`, and other fields of every `Tenant` object. fastapi-tenancy ships with three built-in stores and a protocol for custom implementations.

## Choosing a store

| Store | Persistence | Best for |
|-------|-------------|----------|
| `SQLAlchemyTenantStore` | Database | Production — PostgreSQL, MySQL, SQLite, MSSQL |
| `RedisTenantStore` | Redis (cache layer) | Wraps SQLAlchemy store for high read throughput |
| `InMemoryTenantStore` | Process memory | Unit tests and local prototyping |
| Custom | Anywhere | Legacy databases, microservice registries, etc. |

## The `TenantStore` contract

All stores implement the same abstract interface:

```python
class TenantStore(ABC, Generic[TenantT]):
    # Reads
    async def get_by_id(self, tenant_id: str) -> TenantT: ...
    async def get_by_identifier(self, identifier: str) -> TenantT: ...
    async def list(self, skip=0, limit=100, status=None) -> Sequence[TenantT]: ...
    async def count(self, status=None) -> int: ...
    async def exists(self, tenant_id: str) -> bool: ...
    async def search(self, query: str, limit=10) -> list[TenantT]: ...

    # Writes
    async def create(self, tenant: TenantT) -> TenantT: ...
    async def update(self, tenant: TenantT) -> TenantT: ...
    async def delete(self, tenant_id: str) -> None: ...
    async def set_status(self, tenant_id: str, status: TenantStatus) -> TenantT: ...
    async def update_metadata(self, tenant_id: str, metadata: dict) -> TenantT: ...

    # Batch
    async def get_by_ids(self, tenant_ids) -> list[TenantT]: ...
    async def bulk_update_status(self, tenant_ids, status) -> list[TenantT]: ...
```

Methods that look up a single tenant **always raise `TenantNotFoundError`** — they never return `None`.

## In-depth guides

- [SQLAlchemy store](sqlalchemy.md) — production database backend
- [Redis store](redis.md) — write-through caching wrapper
- [In-memory store](memory.md) — for tests and local development
- [Custom store](custom.md) — implement the protocol
