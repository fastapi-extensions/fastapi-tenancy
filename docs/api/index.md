---
title: API Reference
description: Complete auto-generated API reference for every public class, function, and type in fastapi-tenancy.
---

# API Reference

Complete reference documentation for the fastapi-tenancy public API, auto-generated from docstrings.

## Modules

| Module | Contents |
|--------|----------|
| [TenancyManager](manager.md) | Central orchestrator — lifecycle, tenant management, rate limiting |
| [TenancyConfig](config.md) | Configuration model with all fields and validators |
| [Domain Types](types.md) | `Tenant`, `TenantConfig`, `AuditLog`, `TenantMetrics`, enums, protocols |
| [Exceptions](exceptions.md) | Complete exception hierarchy |
| [Context](context.md) | `TenantContext`, `tenant_scope`, FastAPI dependency functions |
| [Dependencies](dependencies.md) | `make_tenant_db_dependency`, `make_tenant_config_dependency`, `make_audit_log_dependency` |
| [Middleware](middleware.md) | `TenancyMiddleware` — raw ASGI middleware |
| [Isolation Providers](isolation.md) | `SchemaIsolationProvider`, `DatabaseIsolationProvider`, `RLSIsolationProvider`, `HybridIsolationProvider` |
| [Resolution Strategies](resolution.md) | `HeaderTenantResolver`, `SubdomainTenantResolver`, `PathTenantResolver`, `JWTTenantResolver` |
| [Tenant Stores](storage.md) | `TenantStore` ABC, `SQLAlchemyTenantStore`, `InMemoryTenantStore`, `RedisTenantStore` |
| [Cache](cache.md) | `TenantCache` |
| [Migrations](migrations.md) | `TenantMigrationManager` |

## Top-level imports

The most commonly used symbols are importable directly from `fastapi_tenancy`:

```python
from fastapi_tenancy import (
    TenancyConfig,
    TenancyManager,
    TenancyMiddleware,
    Tenant,
    TenantStatus,
    IsolationStrategy,
    ResolutionStrategy,
)
```
