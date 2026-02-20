"""Tenancy middleware — request-scoped tenant resolution and context management.

The middleware sits at the ASGI boundary and ensures that every request
passing through it has a resolved, active tenant bound to the current
:class:`~fastapi_tenancy.core.context.TenantContext` before the route
handler is invoked.

Processing pipeline
-------------------
1. **Skip** — bypass resolution for configured paths and OPTIONS requests.
2. **Guard** — return 503 if the resolver is not yet initialised.
3. **Resolve** — run the configured resolver to extract the tenant.
4. **Validate** — confirm the tenant is in ``ACTIVE`` status.
5. **Bind** — set ``TenantContext`` and attach tenant to ``request.state``.
6. **Forward** — pass control to the next handler.
7. **Cleanup** — ``TenantContext.clear()`` in ``finally`` — always runs.

Middleware registration timing
------------------------------
FastAPI (Starlette) raises ``RuntimeError`` if ``add_middleware`` is called
after the application has started.  Always register this middleware **before**
the lifespan yields, either via :meth:`~fastapi_tenancy.manager.TenancyManager.create_lifespan`
(recommended) or manually::

    @asynccontextmanager
    async def lifespan(app):
        app.add_middleware(TenancyMiddleware, manager=manager)  # ← BEFORE yield
        await manager.initialize()
        yield
        await manager.shutdown()
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from fastapi_tenancy.core.context import TenantContext
from fastapi_tenancy.core.exceptions import (
    TenancyError,
    TenantInactiveError,
    TenantNotFoundError,
    TenantResolutionError,
)

if TYPE_CHECKING:
    from starlette.middleware.base import RequestResponseEndpoint

    from fastapi_tenancy.core.config import TenancyConfig
    from fastapi_tenancy.manager import TenancyManager
    from fastapi_tenancy.resolution.base import BaseTenantResolver

logger = logging.getLogger(__name__)

_DEFAULT_SKIP_PATHS: tuple[str, ...] = (
    "/health",
    "/metrics",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
)


class TenancyMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that resolves the current tenant for every request.

    Args:
        app: The ASGI application (injected by Starlette's middleware machinery).
        config: Tenancy configuration — used for ``debug_headers`` decisions.
        resolver: Pre-built resolver.  Mutually exclusive with *manager*.
        manager: If provided, the resolver is fetched from
            ``manager.resolver`` after ``manager.initialize()`` completes.
            This allows the middleware to be registered *before*
            ``initialize()`` is called.
        skip_paths: URL path prefixes that bypass tenant resolution.
            Defaults to health, metrics, and OpenAPI documentation endpoints.
        debug_headers: When ``True``, add ``X-Tenant-ID`` and
            ``X-Tenant-Identifier`` to every response and expose error details
            in 5xx responses.

    Example — via create_lifespan (recommended)::

        app = FastAPI(lifespan=TenancyManager.create_lifespan(config))

    Example — manual registration::

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            app.add_middleware(
                TenancyMiddleware,
                manager=manager,
                debug_headers=True,
            )
            await manager.initialize()
            yield
            await manager.shutdown()
    """

    def __init__(
        self,
        app: Any,
        *,
        config: TenancyConfig | None = None,
        resolver: BaseTenantResolver | None = None,
        manager: TenancyManager | None = None,
        skip_paths: list[str] | None = None,
        debug_headers: bool = False,
    ) -> None:
        super().__init__(app)
        self.config = config
        self._resolver = resolver
        self._manager = manager
        self._skip_paths: tuple[str, ...] = (
            tuple(skip_paths) if skip_paths is not None else _DEFAULT_SKIP_PATHS
        )
        self._debug = debug_headers
        logger.info("TenancyMiddleware registered skip_paths=%s", self._skip_paths)

    # ------------------------------------------------------------------
    # Resolver access
    # ------------------------------------------------------------------

    @property
    def resolver(self) -> BaseTenantResolver | None:
        """Return the active resolver.

        When a manager was injected, the resolver is fetched from the manager
        so the middleware always uses the post-initialisation resolver even
        when registered before ``initialize()`` ran.
        """
        if self._manager is not None and hasattr(self._manager, "resolver"):
            return self._manager.resolver
        return self._resolver

    # ------------------------------------------------------------------
    # Skip helpers
    # ------------------------------------------------------------------

    def _is_path_skipped(self, path: str) -> bool:
        """Return ``True`` when *path* starts with any configured skip prefix.

        This helper is intentionally public so test code can assert on it
        without issuing real HTTP requests.
        """
        return any(path.startswith(p) for p in self._skip_paths)

    def _should_skip(self, request: Request) -> bool:
        """Return ``True`` when this request should bypass tenant resolution."""
        return request.method == "OPTIONS" or self._is_path_skipped(request.url.path)

    # ------------------------------------------------------------------
    # ASGI dispatch
    # ------------------------------------------------------------------

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Resolve tenant, set context, forward request, and clean up."""
        start = time.perf_counter()

        if self._should_skip(request):
            logger.debug("Skipping tenant resolution path=%s", request.url.path)
            return await call_next(request)

        _resolver = self.resolver
        if _resolver is None:
            logger.error(
                "TenancyMiddleware has no resolver — "
                "was TenancyManager.create_lifespan() used correctly?"
            )
            return self._json_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "service_unavailable",
                "Tenant service is not yet initialised.",
            )

        try:
            tenant = await _resolver.resolve(request)

            logger.info(
                "Resolved tenant identifier=%s id=%s path=%s",
                tenant.identifier,
                tenant.id,
                request.url.path,
            )

            if not tenant.is_active():
                raise TenantInactiveError(
                    tenant_id=tenant.id,
                    status=tenant.status.value,
                )

            # Bind to the async-safe per-request context.
            token = TenantContext.set(tenant)
            request.state.tenant = tenant
            TenantContext.set_metadata("request_path", request.url.path)
            TenantContext.set_metadata("request_method", request.method)

            response = await call_next(request)

            if self._debug:
                response.headers["X-Tenant-ID"] = tenant.id
                response.headers["X-Tenant-Identifier"] = tenant.identifier

            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "Request completed tenant=%s %s %s [%d] %.2f ms",
                tenant.identifier,
                request.method,
                request.url.path,
                response.status_code,
                elapsed_ms,
            )
            return response

        except TenantNotFoundError as exc:
            logger.warning("Tenant not found: %s", exc.message)
            return self._json_error(
                status.HTTP_404_NOT_FOUND,
                "tenant_not_found",
                "The requested tenant does not exist.",
            )

        except TenantResolutionError as exc:
            logger.warning("Tenant resolution failed: %s", exc.message)
            return self._json_error(
                status.HTTP_400_BAD_REQUEST,
                "tenant_resolution_failed",
                "Unable to identify tenant from request.",
                exc.details if self._debug else {},
            )

        except TenantInactiveError as exc:
            logger.warning("Inactive tenant access: %s", exc.message)
            return self._json_error(
                status.HTTP_403_FORBIDDEN,
                "tenant_inactive",
                "This tenant is not active.",
            )

        except TenancyError as exc:
            logger.error("Tenancy error: %s", exc.message, exc_info=True)
            return self._json_error(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "tenancy_error",
                "An error occurred processing tenant information.",
                exc.details if self._debug else {},
            )

        except Exception as exc:
            logger.error("Unexpected middleware error: %s", exc, exc_info=True)
            return self._json_error(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "internal_error",
                "An unexpected error occurred.",
            )

        finally:
            # Always clear the context after the request — even on exception.
            # Using TenantContext.clear() (not reset) because we want to zero
            # out the context for this task regardless of previous state.
            TenantContext.clear()

    @staticmethod
    def _json_error(
        http_status: int,
        error: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> JSONResponse:
        """Build a consistent JSON error response."""
        body: dict[str, Any] = {"error": error, "message": message}
        if details:
            body["details"] = details
        return JSONResponse(status_code=http_status, content=body)


__all__ = ["TenancyMiddleware"]
