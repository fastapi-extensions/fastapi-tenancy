---
title: JWT Resolution
description: Identify tenants from a claim inside a signed Bearer JWT token.
---

# JWT Resolution

The `jwt` strategy decodes a Bearer JWT from the `Authorization` header and reads a configured claim to identify the tenant.

## Requirements

Install the `[jwt]` extra:

```bash
pip install "fastapi-tenancy[jwt]"
```

## Configuration

```python
config = TenancyConfig(
    database_url="...",
    resolution_strategy="jwt",
    jwt_secret="your-secret-key-at-least-32-characters-long",  # required
    jwt_algorithm="HS256",         # default
    jwt_tenant_claim="tenant_id",  # default — claim name in the JWT payload
)
```

!!! warning "Secret strength"
    `jwt_secret` must be at least **32 characters** long. Shorter secrets
    raise `ValidationError` at construction time.

## Token format

The resolver expects `Authorization: Bearer <token>` in the request headers.
The JWT payload must include the configured `jwt_tenant_claim`:

```json
{
  "sub": "user-123",
  "tenant_id": "acme-corp",
  "exp": 1893456000,
  "iat": 1700000000
}
```

## How a request is resolved

```
GET /api/orders HTTP/1.1
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

1. `Authorization` header is read and the `Bearer ` prefix is stripped
2. The token is decoded and the signature is verified using `jwt_secret`
3. Token expiry (`exp` claim) is verified automatically
4. When `audience=` is set, the `aud` claim is verified against the expected value
5. The `jwt_tenant_claim` value (`"acme-corp"`) is extracted
6. It is validated against tenant slug rules
7. `store.get_by_identifier("acme-corp")` looks up the tenant

## Audience validation (recommended)

!!! danger "Cross-service token replay risk"
    If multiple services share the same JWT secret, a token issued for
    **Service A** is valid for **Service B** without audience validation.
    An attacker with a valid token for any service can impersonate any tenant
    on any other service that shares the secret.

Configure an `audience` on the resolver to prevent cross-service replay attacks:

```python
# Via JWTTenantResolver directly (custom wiring)
from fastapi_tenancy.resolution.jwt import JWTTenantResolver

resolver = JWTTenantResolver(
    store,
    secret="your-secret-key-at-least-32-chars",
    audience="my-api-service",   # tokens must carry aud="my-api-service"
)
```

When `audience` is configured:

- PyJWT verifies that the decoded token contains an `aud` claim matching the expected value
- Tokens with a missing, wrong, or absent `aud` claim raise `TenantResolutionError` with reason `"JWT audience claim does not match expected audience"`
- The `details` dict includes `{"expected_audience": "my-api-service"}` for debugging

When `audience=None` (the default), a **WARNING** is logged at resolver construction time to alert operators of the replay risk:

```
WARNING  fastapi_tenancy.resolution.jwt: JWTTenantResolver: no 'audience'
configured. If multiple services share the same JWT secret, set audience= to
prevent cross-service token replay attacks.
```

## Issuing tokens

Your auth service issues JWTs that include the `tenant_id` claim (and optionally `aud`):

```python
import jwt
from datetime import UTC, datetime, timedelta

def issue_token(user_id: str, tenant_identifier: str) -> str:
    payload = {
        "sub": user_id,
        "tenant_id": tenant_identifier,   # ← must match the configured claim name
        "aud": "my-api-service",          # ← match the audience= on the resolver
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(hours=8),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")
```

## Asymmetric algorithms (RS256)

For production, prefer RS256 with a key pair — the app only holds the **public key**:

```python
# fastapi-tenancy only needs the public key
config = TenancyConfig(
    database_url="...",
    resolution_strategy="jwt",
    jwt_secret=PUBLIC_PEM,    # public key PEM string
    jwt_algorithm="RS256",
)
```

With RS256 the private key never reaches the API service, so audience validation is still recommended to prevent tokens issued for other services from being accepted.

## Error responses

| Situation | HTTP status | Reason field |
|-----------|-------------|-------------|
| Missing `Authorization` header | `400` | `"Authorization header is missing"` |
| Not prefixed with `Bearer ` | `400` | `"Authorization header does not use Bearer scheme"` |
| Empty token | `400` | `"Bearer token is empty"` |
| Invalid signature | `400` | `"JWT token is invalid or signature verification failed"` |
| Expired token | `400` | `"JWT token has expired"` |
| Wrong `aud` claim | `400` | `"JWT audience claim does not match expected audience"` |
| Missing tenant claim | `400` | `"JWT payload is missing claim 'tenant_id'"` |
| Invalid identifier in claim | `400` | `"JWT claim 'tenant_id' contains an invalid tenant identifier"` |
| Tenant not found in store | `404` | *(from `TenantNotFoundError`)* |

## Combining with FastAPI security

```python
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import Security

bearer = HTTPBearer()

@app.get("/orders")
async def list_orders(
    tenant: Annotated[Tenant, Depends(get_current_tenant)],
    session: Annotated[AsyncSession, Depends(get_db)],
    credentials: HTTPAuthorizationCredentials = Security(bearer),
):
    # credentials.credentials contains the raw JWT
    # tenant is already resolved from the same JWT by the middleware
    ...
```

!!! tip "Single source of truth"
    The middleware resolves the tenant from the JWT once per request.
    Route handlers receive the already-resolved `Tenant` via `get_current_tenant` —
    there is no need to re-decode the token in route handlers.
