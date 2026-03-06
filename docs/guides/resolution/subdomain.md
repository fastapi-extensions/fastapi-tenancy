---
title: Subdomain Resolution
description: Identify tenants from the leftmost subdomain of the Host header — classic SaaS URL pattern.
---

# Subdomain Resolution

The `subdomain` strategy extracts the tenant identifier from the leftmost subdomain of the incoming `Host` (or `X-Forwarded-Host`) header.

```
acme-corp.example.com  →  "acme-corp"
globex.myapp.io        →  "globex"
```

## Configuration

```python
config = TenancyConfig(
    database_url="...",
    resolution_strategy="subdomain",
    domain_suffix=".example.com",  # required — must include the leading dot
)
```

!!! warning "`domain_suffix` is required"
    The `subdomain` strategy will raise `ValidationError` at construction time
    if `domain_suffix` is not set.

## How a request is resolved

```
GET /api/orders HTTP/1.1
Host: acme-corp.example.com        ← resolver reads this
```

1. The `Host` header value `"acme-corp.example.com"` is extracted
2. The port suffix is stripped (e.g. `"acme-corp.example.com:8000"` → `"acme-corp.example.com"`)
3. The configured `domain_suffix` (`.example.com`) is stripped, leaving `"acme-corp"`
4. The identifier is validated against tenant slug rules
5. `store.get_by_identifier("acme-corp")` looks up the tenant

## Reverse proxies and `X-Forwarded-Host`

When your app sits behind a reverse proxy (nginx, Traefik, AWS ALB), the original `Host` header may be replaced by the proxy. The resolver reads `X-Forwarded-Host` first when `trust_x_forwarded=True` (the default):

```python
# Default — reads X-Forwarded-Host first
resolver = SubdomainTenantResolver(store, domain_suffix=".example.com", trust_x_forwarded=True)

# Disable if you DON'T have a trusted reverse proxy
resolver = SubdomainTenantResolver(store, domain_suffix=".example.com", trust_x_forwarded=False)
```

!!! danger "Disable `trust_x_forwarded` without a trusted proxy"
    If your application is directly internet-accessible (no reverse proxy),
    any client can spoof `X-Forwarded-Host`. Set `trust_x_forwarded=False`
    in that case.

## DNS setup

Each tenant needs a DNS record pointing to your application:

```
# DNS zone file for example.com
acme-corp    CNAME   app.example.com.
globex       CNAME   app.example.com.
*            CNAME   app.example.com.   # wildcard — catches new tenants automatically
```

For local development, add entries to `/etc/hosts`:

```
127.0.0.1   acme-corp.example.local
127.0.0.1   globex.example.local
```

## TLS certificates

Use a **wildcard certificate** for `*.example.com` so new tenants automatically get HTTPS:

```bash
# Let's Encrypt wildcard with Certbot + DNS challenge
certbot certonly \
  --manual \
  --preferred-challenges dns \
  -d "*.example.com"
```

Or use a CDN/load balancer (Cloudflare, AWS CloudFront) that handles wildcard TLS automatically.

## Testing

```python
async def test_subdomain_resolution(app):
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.get(
            "/orders",
            headers={"Host": "acme-corp.example.com"},
        )
        assert response.status_code == 200
```
