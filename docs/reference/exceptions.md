---
title: Exception Hierarchy
description: All exceptions in fastapi-tenancy and when they are raised.
---

# Exception Hierarchy

```
Exception
└── TenancyError
    ├── TenantNotFoundError
    ├── TenantResolutionError
    ├── TenantInactiveError
    ├── IsolationError
    ├── ConfigurationError
    ├── MigrationError
    ├── RateLimitExceededError
    ├── TenantDataLeakageError
    ├── TenantQuotaExceededError
    └── DatabaseConnectionError
```

All exceptions inherit from `TenancyError`, so you can catch the entire family with a single `except TenancyError` clause.

## HTTP status mapping (middleware)

| Exception | HTTP status |
|-----------|-------------|
| `TenantResolutionError` | 400 |
| `TenantNotFoundError` | 404 |
| `TenantInactiveError` | 403 |
| `RateLimitExceededError` | 429 |
| Any other `TenancyError` | 500 |

## Exception details

Every exception has a `details` dict that is safe to log. It never contains raw secrets, full stack traces, or user PII.

```python
try:
    tenant = await store.get_by_identifier("unknown")
except TenantNotFoundError as exc:
    print(exc.message)   # "Tenant not found: 'unknown'"
    print(exc.details)   # {}
    print(exc.identifier) # "unknown"
```

## Security note: `TenantDataLeakageError`

`TenantDataLeakageError` is a **critical security exception**. Any occurrence should trigger an immediate alert and incident response — it indicates that cross-tenant data access was detected.

```python
from fastapi_tenancy.core.exceptions import TenantDataLeakageError

try:
    await do_operation()
except TenantDataLeakageError as exc:
    # CRITICAL: page on-call immediately
    alert_oncall(
        f"DATA LEAKAGE: {exc.operation} "
        f"expected={exc.expected_tenant} actual={exc.actual_tenant}"
    )
    raise
```
