---
title: Header Resolution
description: Identify tenants from an HTTP request header — the simplest and most flexible strategy.
---

# Header Resolution

The `header` strategy reads the tenant identifier from a configurable HTTP request header. It is the default strategy and the easiest to set up.

## Configuration

```python
config = TenancyConfig(
    database_url="...",
    resolution_strategy="header",
    tenant_header_name="X-Tenant-ID",  # default — any header name works
)
```

## How a request is resolved

```
GET /api/orders HTTP/1.1
Host: api.example.com
X-Tenant-ID: acme-corp          ← resolver reads this
Authorization: Bearer <token>
```

1. The middleware calls `HeaderTenantResolver.resolve(request)`
2. The header value `"acme-corp"` is extracted and stripped of whitespace
3. It is validated against tenant slug rules (lowercase letters, digits, hyphens, 3–63 chars)
4. `store.get_by_identifier("acme-corp")` looks up the tenant
5. If found and active, the request proceeds; otherwise a 400/404/403 is returned

## Custom header name

```python
config = TenancyConfig(
    database_url="...",
    resolution_strategy="header",
    tenant_header_name="X-Organisation-ID",
)
```

Any valid HTTP header name works. The comparison is case-insensitive per HTTP spec.

## Security

The header value is validated against tenant slug rules **before** any database query. Invalid values (containing SQL characters, path separators, etc.) raise `TenantResolutionError` immediately — no database round-trip occurs.

Responses never reveal why resolution failed (missing header vs unknown tenant) to prevent information leakage about which tenant identifiers exist.

## Example: API gateway forwarding

When using an API gateway (Kong, AWS API GW, nginx), the gateway can inject the tenant header after authenticating the request:

```nginx
# nginx — extract from JWT and forward as header
set $tenant_id "";
access_by_lua_block {
    local jwt = require "resty.jwt"
    local token = ngx.req.get_headers()["Authorization"]
    -- ... decode JWT, extract tenant_id claim ...
    ngx.req.set_header("X-Tenant-ID", tenant_id)
}
```

```python
# fastapi-tenancy — trusts the header set by the gateway
config = TenancyConfig(
    database_url="...",
    resolution_strategy="header",
    tenant_header_name="X-Tenant-ID",
)
```

## Testing

```python
from httpx import AsyncClient

async def test_tenant_resolution(app):
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.get(
            "/orders",
            headers={"X-Tenant-ID": "acme-corp"},
        )
        assert response.status_code == 200
```

Missing header → `400`:

```python
response = await client.get("/orders")  # no X-Tenant-ID
assert response.status_code == 400
assert response.json()["detail"] == "Header 'X-Tenant-ID' is missing or empty"
```
