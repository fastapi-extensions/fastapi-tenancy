---
title: Path Resolution
description: Identify tenants from a fixed URL path prefix — great for single-domain APIs.
---

# Path Resolution

The `path` strategy extracts the tenant identifier from a fixed prefix in the URL path.

```
/tenants/acme-corp/orders     →  "acme-corp"
/tenants/globex/users/42      →  "globex"
```

## Configuration

```python
config = TenancyConfig(
    database_url="...",
    resolution_strategy="path",
    path_prefix="/tenants",  # default
)
```

## How a request is resolved

```
GET /tenants/acme-corp/orders HTTP/1.1
```

1. The path `/tenants/acme-corp/orders` is compared against the configured prefix `/tenants/`
2. The next path segment after the prefix (`acme-corp`) is extracted as the identifier
3. It is validated against tenant slug rules
4. `store.get_by_identifier("acme-corp")` looks up the tenant
5. `request.state.tenant_path_remainder` is set to `"/orders"` for downstream handlers

## FastAPI route setup

```python
app.add_middleware(
    TenancyMiddleware,
    manager=manager,
    excluded_paths=["/health", "/docs", "/openapi.json"],
)

# Routes must include the tenant prefix
@app.get("/tenants/{tenant_id}/orders")
async def list_orders(
    tenant_id: str,
    tenant: Annotated[Tenant, Depends(get_current_tenant)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    # tenant_id path param and tenant.identifier will match
    result = await session.execute(select(Order))
    return result.scalars().all()
```

## Path remainder

The resolver stores the path after the tenant segment in `request.state.tenant_path_remainder`:

```python
# Request: GET /tenants/acme-corp/orders/42
request.state.tenant_path_remainder  # → "/orders/42"
```

This is useful if you need to strip the tenant prefix for routing or logging purposes.

## Custom prefix

```python
config = TenancyConfig(
    database_url="...",
    resolution_strategy="path",
    path_prefix="/org",  # /org/acme-corp/orders
)
```

## Use cases

- **Single-domain APIs** — when subdomains are not available (e.g. mobile app backends)
- **B2B APIs** — server-side clients that find path-based routing clearer
- **Multi-region deployments** — one domain with region+tenant in the path: `/us/acme-corp/orders`

## Excluded paths

Paths that don't match the prefix raise `TenantResolutionError`. Always exclude
your health/docs endpoints from middleware:

```python
app.add_middleware(
    TenancyMiddleware,
    manager=manager,
    excluded_paths=["/health", "/docs", "/redoc", "/openapi.json"],
)
```
