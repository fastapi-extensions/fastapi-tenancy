"""Raw ASGI tenancy middleware — correct, streaming-safe, context-clean.

Why raw ASGI instead of ``BaseHTTPMiddleware``
----------------------------------------------
Starlette's ``BaseHTTPMiddleware`` has two documented problems that make it
unsuitable as the tenancy middleware:

1. **Response buffering**: It buffers the entire response body before sending
   it to the client, breaking Server-Sent Events, chunked transfers, and any
   streaming endpoint.

2. **``contextvars`` propagation bug**: Mutations to ``ContextVar`` made inside
   ``BaseHTTPMiddleware.dispatch()`` are not visible to background tasks
   spawned during the request (Starlette issue #1001).  Because this middleware
   sets a ``ContextVar`` (the current tenant), that bug would silently drop
   the tenant context in any ``BackgroundTask``.

This implementation uses the raw ASGI 3-callable interface
``__call__(scope, receive, send)``.  It has zero buffering overhead and
``ContextVar`` mutations propagate correctly to all callables in the call chain.

ASGI lifecycle
--------------
::

    Client                        Middleware               App
      │                               │                    │
      │── HTTP request ──────────────►│                    │
      │                           resolve tenant           │
      │                           TenantContext.set()      │
      │                               ├── await app() ────►│
      │                               │◄── response ───────│
      │◄── response ─────────────────►│                    │
      │                           TenantContext.reset()    │

Thread / task safety
--------------------
``ContextVar.set()`` returns a ``Token``; ``ContextVar.reset(token)`` restores
the previous value regardless of what other coroutines ran in between.
``TenantContext.reset(token)`` is called in the ``finally`` block so the
context is always restored even when the app raises or the client disconnects.

Error handling
--------------
- ``TenantNotFoundError`` → ``404 Not Found``
- ``TenantInactiveError`` → ``403 Forbidden``
- ``TenantResolutionError`` → ``400 Bad Request``
- ``RateLimitExceededError`` → ``429 Too Many Requests``
- Any other ``TenancyError`` → ``500 Internal Server Error``

All error responses are plain JSON with a stable ``{"detail": "..."}`` shape.

Excluded paths
--------------
Pass a list of path prefixes to ``excluded_paths`` to bypass tenancy for
health checks, metrics endpoints, and static files::

    app.add_middleware(
        TenancyMiddleware,
        manager=manager,
        excluded_paths=["/health", "/metrics", "/docs", "/openapi.json"],
    )
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi_tenancy.core.context import TenantContext
from fastapi_tenancy.core.exceptions import (
    RateLimitExceededError,
    TenancyError,
    TenantInactiveError,
    TenantNotFoundError,
    TenantResolutionError,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)


async def _json_response(
    send: Send,
    status_code: int,
    detail: str,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    """Build and send a minimal JSON error response over an HTTP connection.

    Args:
        send: ASGI send callable.
        status_code: HTTP status code.
        detail: Human-readable error description for the ``detail`` field.
        extra_headers: Optional additional response headers (e.g. Retry-After).
    """
    body = json.dumps({"detail": detail}).encode("utf-8")
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if extra_headers:
        headers.extend(extra_headers)

    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": headers,
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": body,
            "more_body": False,
        }
    )


async def _ws_close(send: Send, code: int = 1008) -> None:
    """Send a WebSocket close frame.

    WebSocket error handling must not emit ``http.response.start``
    (only valid for HTTP scopes).  Use ``websocket.close`` instead so the
    ASGI server does not corrupt the connection.

    Args:
        send: ASGI send callable from a ``websocket`` scope.
        code: WebSocket close code.  1008 = Policy Violation (tenant inactive /
              rate limited); 1011 = Internal Error (unexpected tenancy error).
    """
    with contextlib.suppress(Exception):
        await send({"type": "websocket.close", "code": code})


class TenancyMiddleware:
    """Raw ASGI middleware that resolves the current tenant per request.

    Sets the tenant on ``TenantContext`` at the start of every HTTP request
    and restores the previous state at the end — even on error.

    Args:
        app: The downstream ASGI application.
        manager: The configured :class:`~fastapi_tenancy.manager.TenancyManager`.
        excluded_paths: List of URL path prefixes that bypass tenancy
            resolution (e.g. ``["/health", "/docs"]``).

    Example::

        from fastapi_tenancy.middleware.tenancy import TenancyMiddleware

        app.add_middleware(
            TenancyMiddleware,
            manager=manager,
            excluded_paths=["/health", "/docs", "/openapi.json"],
        )
    """

    def __init__(
        self,
        app: ASGIApp,
        manager: Any,  # TenancyManager — Any avoids circular import
        excluded_paths: list[str] | None = None,
    ) -> None:
        self._app = app
        self._manager = manager
        self._excluded: list[str] = excluded_paths or []

    def _is_excluded(self, path: str) -> bool:
        """Return ``True`` when *path* starts with any excluded prefix.

        Args:
            path: Request URL path.

        Returns:
            ``True`` when this path should bypass tenancy resolution.
        """
        return any(path.startswith(prefix) for prefix in self._excluded)

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Process an ASGI request.

        Handles only ``http`` and ``websocket`` scopes.  All other scopes
        (``lifespan``, etc.) are passed through unchanged.

        Args:
            scope: ASGI connection scope.
            receive: ASGI receive callable.
            send: ASGI send callable.
        """
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        path: str = scope.get("path", "/")
        if self._is_excluded(path):
            await self._app(scope, receive, send)
            return

        await self._handle(scope, receive, send)

    async def _handle(  # noqa: PLR0912, PLR0915
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Resolve tenant, set context, delegate to app, restore context.

        Wraps ``send`` in a thin sentinel so that exception handlers in the
        ``finally`` block can detect whether the downstream app has already
        started sending a response.  Attempting to send a second
        ``http.response.start`` event after the first would violate the ASGI
        spec and corrupt the connection — the guard prevents that.

        Args:
            scope: ASGI connection scope.
            receive: ASGI receive callable.
            send: ASGI send callable.
        """
        from starlette.requests import Request  # noqa: PLC0415

        is_http = scope["type"] == "http"

        # Request() asserts scope["type"] == "http", so for WebSocket
        # connections we must build a WebSocket object instead.  Both expose the
        # same .headers interface that resolvers rely on.
        if is_http:
            request = Request(scope, receive)
        else:
            from starlette.websockets import WebSocket  # noqa: PLC0415

            request = WebSocket(scope, receive, send)  # type: ignore[assignment]

        ##################################################################
        # Wrap send to track whether response headers have been sent.    #
        # Only relevant for HTTP scopes; WebSocket connections don't     #
        # send http.response.start so response_started stays False.      #
        ##################################################################
        response_started = False

        async def _send_wrapper(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        async def _send_error(
            status: int, detail: str, extra: list[tuple[bytes, bytes]] | None = None
        ) -> None:
            """Send an error response appropriate for the current scope type."""
            if is_http:
                if not response_started:
                    await _json_response(send, status, detail, extra)
            else:
                # WebSocket error — close with a policy-violation code.
                # 1008 = Policy Violation; 1011 = Internal Error.
                ws_code = 1011 if status == 500 else 1008
                await _ws_close(send, ws_code)

        # Resolve the tenant using the configured resolver.
        try:
            tenant = await self._manager.resolver.resolve(request)
        except TenantResolutionError as exc:
            logger.debug("Tenant resolution failed: %s", exc)
            await _send_error(400, exc.reason)
            return
        except TenantNotFoundError as exc:
            logger.debug("Tenant not found: %s", exc)
            await _send_error(404, "Tenant not found")
            return
        except TenancyError as exc:
            logger.exception("Tenancy error during resolution: %s", exc)  # noqa: TRY401
            await _send_error(500, "Internal tenancy error")
            return

        # Guard: only ACTIVE tenants may proceed.
        if not tenant.is_active():
            logger.info(
                "Blocked request for inactive tenant %s (status=%s)", tenant.id, tenant.status
            )
            await _send_error(403, f"Tenant is not active (status: {tenant.status.value})")
            return

        # Check rate limit when enabled.
        if self._manager.config.enable_rate_limiting:
            try:
                await self._manager.check_rate_limit(tenant)
            except RateLimitExceededError:
                logger.info("Rate limit exceeded for tenant %s", tenant.id)
                window = self._manager.config.rate_limit_window_seconds
                await _send_error(
                    429,
                    "Rate limit exceeded. Please slow down.",
                    [(b"retry-after", str(window).encode())],
                )
                return

        # Set tenant context — use token-based reset in finally for safety.
        # Initialise token to None so the finally block can safely guard against
        # NameError if an exception occurs before TenantContext.set() is reached.
        token = None
        token = TenantContext.set(tenant)
        try:
            # Attach tenant to scope so route handlers can access it without
            # going through TenantContext (useful for debugging tools).
            if "state" not in scope:
                from starlette.datastructures import State  # noqa: PLC0415

                scope["state"] = State()
            # Support both State objects (attribute access) and plain dicts.
            state = scope["state"]
            if isinstance(state, dict):
                state["tenant"] = tenant
            else:
                state.tenant = tenant

            await self._app(scope, receive, _send_wrapper)  # type: ignore[arg-type]
        except RateLimitExceededError:
            window = self._manager.config.rate_limit_window_seconds
            logger.info("RateLimitExceededError raised in app for tenant %s", tenant.id)
            await _send_error(
                429,
                "Rate limit exceeded. Please slow down.",
                [(b"retry-after", str(window).encode())],
            )
        except TenantInactiveError:
            logger.info("TenantInactiveError raised in app for tenant %s", tenant.id)
            await _send_error(403, "Tenant is not active.")
        except TenancyError as exc:
            logger.exception("Unhandled tenancy error: %s", exc)  # noqa: TRY401
            await _send_error(500, "Internal tenancy error")
        finally:
            # Always restore — never unconditionally clear.
            # Guard against NameError if set() was never reached.
            if token is not None:
                TenantContext.reset(token)


__all__ = ["TenancyMiddleware"]
