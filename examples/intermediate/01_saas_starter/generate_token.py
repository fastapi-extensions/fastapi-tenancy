#!/usr/bin/env python3
"""
Generate a test JWT token for the SaaS Starter example.

Usage:
    python generate_token.py acme-corp
    python generate_token.py globex

The token includes a "tenant_id" claim that JWTTenantResolver reads.
"""
import sys
import time

import jwt  # PyJWT


def generate(tenant_identifier: str, secret: str = "change-me-in-production") -> str:
    payload = {
        "sub": f"user-{tenant_identifier}",
        "tenant_id": tenant_identifier,   # ← the claim JWTTenantResolver reads
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,   # expires in 1 hour
    }
    return jwt.encode(payload, secret, algorithm="HS256")


if __name__ == "__main__":
    identifier = sys.argv[1] if len(sys.argv) > 1 else "acme-corp"
    token = generate(identifier)
    print(f"\nTenant: {identifier}")
    print(f"Token:  {token}")
    print(f"\nUsage:")
    print(f'  curl http://localhost:8000/posts -H "Authorization: Bearer {token}"')
