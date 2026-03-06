---
title: Resolution Strategies
description: How fastapi-tenancy identifies the current tenant from each HTTP request.
---

# Resolution Strategies

A **resolution strategy** controls how the current tenant is extracted from each HTTP request. fastapi-tenancy ships with five built-in strategies and a protocol for writing custom ones.

## Comparison

| Strategy | Tenant comes from | Typical use case |
|----------|------------------|-----------------|
| `header` | `X-Tenant-ID` header | Internal APIs, mobile apps, API gateways |
| `subdomain` | `acme.example.com` → `acme` | SaaS web apps with branded subdomains |
| `path` | `/tenants/acme/orders` → `acme` | REST APIs with tenant-prefixed paths |
| `jwt` | JWT claim in `Authorization: Bearer ...` | Auth-first APIs with per-tenant tokens |
| `custom` | Whatever your resolver returns | Cookie, session, database lookup, etc. |

## Quick configuration

=== "Header"

    ```python
    config = TenancyConfig(
        database_url="...",
        resolution_strategy="header",
        tenant_header_name="X-Tenant-ID",  # default
    )
    ```

=== "Subdomain"

    ```python
    config = TenancyConfig(
        database_url="...",
        resolution_strategy="subdomain",
        domain_suffix=".example.com",  # required
    )
    ```

=== "Path"

    ```python
    config = TenancyConfig(
        database_url="...",
        resolution_strategy="path",
        path_prefix="/tenants",  # default
    )
    ```

=== "JWT"

    ```python
    config = TenancyConfig(
        database_url="...",
        resolution_strategy="jwt",
        jwt_secret="your-secret-at-least-32-chars-long",
        jwt_algorithm="HS256",
        jwt_tenant_claim="tenant_id",
    )
    ```

=== "Custom"

    ```python
    from fastapi_tenancy.resolution.base import BaseTenantResolver
    from fastapi_tenancy.core.types import Tenant
    from starlette.requests import Request

    class CookieResolver(BaseTenantResolver):
        async def resolve(self, request: Request) -> Tenant:
            slug = request.cookies.get("X-Tenant")
            if not slug:
                raise TenantResolutionError("Cookie missing", strategy="cookie")
            return await self._store.get_by_identifier(slug)

    manager = TenancyManager(
        config=TenancyConfig(database_url="...", resolution_strategy="custom"),
        store=store,
        custom_resolver=CookieResolver(store),
    )
    ```

## Error responses

All resolvers raise typed exceptions that the middleware converts to HTTP responses:

| Exception | HTTP status | Cause |
|-----------|-------------|-------|
| `TenantResolutionError` | `400` | Request is malformed (missing header, bad JWT, wrong path) |
| `TenantNotFoundError` | `404` | Identifier is valid but no matching tenant exists |
| `TenantInactiveError` | `403` | Tenant exists but is suspended or deleted |

## In-depth guides

- [Header](header.md)
- [Subdomain](subdomain.md)
- [Path](path.md)
- [JWT](jwt.md)
- [Custom](custom.md)
