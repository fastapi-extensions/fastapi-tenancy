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
    jwt_algorithm="HS256",    # default
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
4. The `jwt_tenant_claim` value (`"acme-corp"`) is extracted
5. It is validated against tenant slug rules
6. `store.get_by_identifier("acme-corp")` looks up the tenant

## Issuing tokens

Your auth service issues JWTs that include the `tenant_id` claim:

```python
import jwt
from datetime import UTC, datetime, timedelta

def issue_token(user_id: str, tenant_identifier: str) -> str:
    payload = {
        "sub": user_id,
        "tenant_id": tenant_identifier,   # ← must match the configured claim name
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(hours=8),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")
```

## Asymmetric algorithms (RS256)

For production, prefer RS256 with a key pair — the app only holds the **public key**:

```python
import jwt

# Generate key pair (do this once, store private key securely)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
public_key = private_key.public_key()

PUBLIC_PEM = public_key.public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()
```

```python
# fastapi-tenancy only needs the public key
config = TenancyConfig(
    database_url="...",
    resolution_strategy="jwt",
    jwt_secret=PUBLIC_PEM,    # public key PEM string
    jwt_algorithm="RS256",
)
```

## Error responses

| Situation | HTTP status | Cause |
|-----------|-------------|-------|
| Missing `Authorization` header | `400` | Header not present |
| Token not prefixed with `Bearer ` | `400` | Malformed header value |
| Invalid signature | `400` | Wrong secret or tampered token |
| Expired token | `400` | `exp` claim is in the past |
| Missing tenant claim | `400` | `tenant_id` not in payload |

## Combining with FastAPI security

You can use fastapi-tenancy JWT resolution alongside FastAPI's built-in security:

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
