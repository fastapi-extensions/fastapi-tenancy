---
title: Middleware
description: How TenancyMiddleware works — raw ASGI, excluded paths, error mapping.
---

# Middleware

`TenancyMiddleware` is a raw ASGI middleware that resolves the current tenant for every HTTP and WebSocket request and makes it available via `TenantContext` for the duration of the request.

## Why raw ASGI?

Starlette's `BaseHTTPMiddleware` has two documented problems that make it unsuitable as a tenancy middleware:

1. **Response buffering** — it buffers the entire response body before sending, breaking SSE, chunked transfers, and streaming endpoints.
2. **`contextvars` propagation bug** — mutations to `ContextVar` inside `dispatch()` are not visible to background tasks spawned during the request. Since the middleware sets a `ContextVar` (the current tenant), background tasks would silently lose tenant context.

`TenancyMiddleware` uses the raw ASGI 3-callable interface (`__call__(scope, receive, send)`) — zero buffering overhead and correct `ContextVar` propagation.

## Adding the middleware

```python
from fastapi_tenancy.middleware.tenancy import TenancyMiddleware

app.add_middleware(
    TenancyMiddleware,
    manager=manager,
    excluded_paths=["/health", "/metrics", "/docs", "/redoc", "/openapi.json"],
)
```

!!! tip "Add middleware before routes are registered"
    FastAPI applies middleware in reverse registration order. Add
    `TenancyMiddleware` before any other middleware that needs tenant context.

## Excluded paths

Paths in `excluded_paths` bypass tenant resolution entirely. The check is a
**prefix match** — `/health` excludes `/health`, `/health/live`, `/health/ready`, etc.

```python
excluded_paths=[
    "/health",         # health probes
    "/metrics",        # Prometheus
    "/docs",           # Swagger UI
    "/redoc",          # ReDoc
    "/openapi.json",   # OpenAPI spec
    "/favicon.ico",    # browser auto-request
    "/static",         # static file directory
]
```

## Error handling

The middleware converts typed exceptions to HTTP JSON responses:

| Exception | HTTP | JSON body |
|-----------|------|-----------|
| `TenantResolutionError` | 400 | `{"detail": "<reason>"}` |
| `TenantNotFoundError` | 404 | `{"detail": "Tenant not found"}` |
| `TenantInactiveError` | 403 | `{"detail": "Tenant is not active (status: <status>)"}` |
| `RateLimitExceededError` | 429 | `{"detail": "Rate limit exceeded ..."}` |
| Any other `TenancyError` | 500 | `{"detail": "Internal tenancy error"}` |

All error responses have `Content-Type: application/json`.

## Request lifecycle

```
Client request
    │
    ▼
TenancyMiddleware.__call__()
    │
    ├── scope["type"] not "http"/"websocket" → pass through
    │
    ├── path in excluded_paths → pass through
    │
    └── _handle()
            │
            ├── resolver.resolve(request) → Tenant  ──or── error response
            │
            ├── tenant.is_active()? No → 403
            │
            ├── check_rate_limit(tenant)? Exceeded → 429
            │
            ├── TenantContext.set(tenant) → token
            │
            ├── scope["state"]["tenant"] = tenant
            │
            ├── await app(scope, receive, send)  ← your routes run here
            │
            └── finally: TenantContext.reset(token)  ← always restored
```

## Accessing the tenant in route handlers

The middleware sets the tenant in `TenantContext` before calling your route handler. Access it via the `get_current_tenant` dependency:

```python
from fastapi import Depends
from fastapi_tenancy.dependencies import get_current_tenant

@app.get("/orders")
async def list_orders(tenant: Annotated[Tenant, Depends(get_current_tenant)]):
    return {"tenant": tenant.identifier}
```

Or directly via `TenantContext` (not recommended in route handlers — prefer `Depends`):

```python
from fastapi_tenancy.core.context import TenantContext

tenant = TenantContext.get()  # raises if not set
```

## WebSocket support

The middleware handles both `http` and `websocket` scope types. WebSocket connections are resolved the same way as HTTP requests:

```python
@app.websocket("/ws/events")
async def websocket_events(
    websocket: WebSocket,
    tenant: Annotated[Tenant, Depends(get_current_tenant)],
):
    await websocket.accept()
    # tenant is resolved from the upgrade request headers
    ...
```
