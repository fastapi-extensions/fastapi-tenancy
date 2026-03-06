---
title: Custom Resolution
description: Write a custom tenant resolver using the TenantResolver protocol or BaseTenantResolver base class.
---

# Custom Resolution

The `custom` strategy lets you supply any object that satisfies the `TenantResolver` protocol. This covers cases not handled by the built-in strategies: cookies, sessions, API keys, database lookups, and more.

## The protocol

Any object with an `async def resolve(self, request) -> Tenant` method satisfies the `TenantResolver` protocol — no inheritance required:

```python
from starlette.requests import Request
from fastapi_tenancy.core.types import Tenant
from fastapi_tenancy.core.exceptions import TenantResolutionError, TenantNotFoundError

class CookieTenantResolver:
    """Resolve tenant from a session cookie."""

    def __init__(self, store):
        self._store = store

    async def resolve(self, request: Request) -> Tenant:
        slug = request.cookies.get("X-Tenant")
        if not slug:
            raise TenantResolutionError("Cookie 'X-Tenant' missing", strategy="cookie")
        return await self._store.get_by_identifier(slug)
```

## Using `BaseTenantResolver`

Alternatively, subclass `BaseTenantResolver` for automatic store injection:

```python
from fastapi_tenancy.resolution.base import BaseTenantResolver
from fastapi_tenancy.core.exceptions import TenantResolutionError

class ApiKeyTenantResolver(BaseTenantResolver):
    """Resolve tenant from an X-API-Key header via an API-key → tenant mapping."""

    def __init__(self, store, api_key_map: dict[str, str]):
        super().__init__(store)  # sets self.store
        self._api_key_map = api_key_map  # {api_key: tenant_identifier}

    async def resolve(self, request) -> Tenant:
        api_key = request.headers.get("X-API-Key", "").strip()
        if not api_key:
            raise TenantResolutionError("X-API-Key header missing", strategy="api-key")

        tenant_id = self._api_key_map.get(api_key)
        if not tenant_id:
            raise TenantResolutionError("Unknown API key", strategy="api-key")

        return await self.store.get_by_identifier(tenant_id)
```

## Registering the resolver

Pass it to `TenancyManager` and set `resolution_strategy="custom"`:

```python
from fastapi_tenancy import TenancyConfig, TenancyManager

config = TenancyConfig(
    database_url="...",
    resolution_strategy="custom",   # required
)

resolver = ApiKeyTenantResolver(store, api_key_map={
    "sk-abc123": "acme-corp",
    "sk-xyz789": "globex",
})

manager = TenancyManager(config, store, custom_resolver=resolver)
```

!!! warning "strategy must be `custom`"
    If `resolution_strategy` is anything other than `"custom"`, the
    `custom_resolver` argument is ignored and a warning is logged.

## Async lookups

Custom resolvers can perform async database or network calls:

```python
class DatabaseApiKeyResolver(BaseTenantResolver):
    def __init__(self, store, db_session):
        super().__init__(store)
        self._db = db_session

    async def resolve(self, request) -> Tenant:
        api_key = request.headers.get("X-API-Key", "")
        # Async lookup in a separate api_keys table
        row = await self._db.execute(
            select(ApiKey).where(ApiKey.key == api_key)
        )
        key_record = row.scalar_one_or_none()
        if not key_record:
            raise TenantResolutionError("Invalid API key", strategy="db-api-key")
        return await self.store.get_by_identifier(key_record.tenant_identifier)
```

## Error handling contract

Custom resolvers must raise one of:

| Exception | When to raise |
|-----------|--------------|
| `TenantResolutionError(reason, strategy)` | The request doesn't carry usable tenant information (missing cookie, malformed header, etc.) |
| `TenantNotFoundError(identifier)` | The identifier is valid but no tenant matches |

Do **not** return `None` — always raise if resolution fails.

## Runtime-checkable protocol check

```python
from fastapi_tenancy.core.types import TenantResolver

resolver = CookieTenantResolver(store)
assert isinstance(resolver, TenantResolver)  # True — duck-typed, no inheritance needed
```
