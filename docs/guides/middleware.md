---
title: Middleware
description: How TenancyMiddleware works — raw ASGI, excluded paths, WebSocket support, error mapping.
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

The middleware maps typed exceptions to scope-appropriate responses:

### HTTP scopes

| Exception | HTTP status | JSON body |
|-----------|-------------|-----------|
| `TenantResolutionError` | `400` | `{"detail": "<reason>"}` |
| `TenantNotFoundError` | `404` | `{"detail": "Tenant not found"}` |
| `TenantInactiveError` | `403` | `{"detail": "Tenant is not active (status: <status>)"}` |
| `RateLimitExceededError` | `429` | `{"detail": "Rate limit exceeded ..."}` with `Retry-After` header |
| Any other `TenancyError` | `500` | `{"detail": "Internal tenancy error"}` |

All HTTP error responses have `Content-Type: application/json`.

### WebSocket scopes

For WebSocket connections, HTTP response events (`http.response.start`) are invalid and would corrupt the ASGI connection. The middleware sends `websocket.close` frames instead:

| Condition | WebSocket close code | Meaning |
|-----------|---------------------|---------|
| Resolution failure / tenant not found / inactive | `1008` | Policy Violation |
| Rate limit exceeded | `1008` | Policy Violation |
| Unexpected internal error | `1011` | Internal Error |

## Request lifecycle

```
Client request
    │
    ▼
TenancyMiddleware.__call__()
    │
    ├── scope["type"] not "http"/"websocket" → pass through unchanged
    │
    ├── path in excluded_paths → pass through unchanged
    │
    └── _handle()
            │
            ├── Build Request (HTTP) or WebSocket (ws) object for resolver
            │
            ├── resolver.resolve(request) → Tenant  ──or── _send_error()
            │
            ├── tenant.is_active()? No → _send_error(403 / ws:1008)
            │
            ├── check_rate_limit(tenant)? Exceeded → _send_error(429 / ws:1008)
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

tenant = TenantContext.get()  # raises TenantNotFoundError if not set
```

## WebSocket support

The middleware resolves the tenant from the WebSocket upgrade request using the same resolver that handles HTTP. Resolution is based on the request headers (or path / JWT, depending on `resolution_strategy`).

```python
@app.websocket("/ws/events")
async def websocket_events(
    websocket: WebSocket,
    tenant: Annotated[Tenant, Depends(get_current_tenant)],
):
    await websocket.accept()
    # tenant is resolved from the upgrade request headers
    while True:
        data = await websocket.receive_json()
        await websocket.send_json({"tenant": tenant.identifier, "echo": data})
```

If the tenant is not found, inactive, or the rate limit is exceeded during a WebSocket handshake, the middleware closes the connection with an appropriate close code rather than sending an HTTP response.

## `_json_response` internal helper

The `_json_response` function used internally to build HTTP error responses is a proper `async def` coroutine. This makes it safe to call with `await` and ensures any forgotten `await` is caught as a type error rather than silently producing a no-op coroutine object.
