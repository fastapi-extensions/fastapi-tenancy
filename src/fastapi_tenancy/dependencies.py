"""FastAPI dependency-injection helpers for multi-tenant applications.

All public symbols in this module are designed to be used with
``fastapi.Depends``.

Available dependencies
----------------------
:func:`get_tenant_db`
    Yield a database session scoped to the current tenant's namespace.
    The session's ``search_path``, connection target, or RLS variable is
    configured automatically by the active
    :class:`~fastapi_tenancy.isolation.base.BaseIsolationProvider`.

:func:`require_active_tenant`
    Return the current tenant, raising ``403 Forbidden`` if inactive.
    Useful for routes that bypass the standard middleware.

:func:`get_tenant_config`
    Build a :class:`~fastapi_tenancy.core.types.TenantConfig` from the
    tenant's ``metadata`` blob.

Note on active-tenant validation
---------------------------------
:class:`~fastapi_tenancy.middleware.tenancy.TenancyMiddleware` validates
tenant status *before* the request reaches any route handler.
:func:`get_tenant_db` therefore does **not** re-validate — the check would
be redundant dead code.  Use :func:`require_active_tenant` only in routes
that deliberately bypass the standard middleware stack.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, Request, status

from fastapi_tenancy.core.context import get_current_tenant
from fastapi_tenancy.core.types import Tenant, TenantConfig

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession


async def get_tenant_db(
    tenant: Annotated[Tenant, Depends(get_current_tenant)],
    request: Request,
) -> AsyncIterator[AsyncSession]:
    """Yield a database session scoped to the current tenant.

    The session is configured by the active
    :class:`~fastapi_tenancy.isolation.base.BaseIsolationProvider`
    (``search_path``, tenant session variable, or dedicated connection)
    before yielding.

    The session is automatically closed after the response — either on
    successful completion or on exception.

    Args:
        tenant: Injected current tenant (from ``get_current_tenant``).
        request: FastAPI request (used to access ``app.state``).

    Yields:
        An :class:`~sqlalchemy.ext.asyncio.AsyncSession` scoped to the
        current tenant's data namespace.

    Raises:
        RuntimeError: When ``isolation_provider`` is not found on ``app.state``
            (i.e. :meth:`~fastapi_tenancy.manager.TenancyManager.create_lifespan`
            was not used).

    Example::

        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession
        from fastapi import Depends
        from fastapi_tenancy.dependencies import get_tenant_db

        @app.get("/users")
        async def list_users(
            session: AsyncSession = Depends(get_tenant_db),
        ):
            result = await session.execute(select(User))
            return result.scalars().all()
    """
    isolation_provider = getattr(request.app.state, "isolation_provider", None)
    if isolation_provider is None:
        raise RuntimeError(
            "isolation_provider not found on app.state. "
            "Ensure TenancyManager.create_lifespan() is used as the FastAPI lifespan."
        )
    async with isolation_provider.get_session(tenant) as session:
        yield session


async def require_active_tenant(
    tenant: Annotated[Tenant, Depends(get_current_tenant)],
) -> Tenant:
    """Return the current tenant; raise 403 if inactive.

    Use this dependency in routes that deliberately bypass the standard
    middleware (e.g. webhook endpoints, admin-only routes with a custom
    skip-path configuration).

    For most routes, active-tenant validation is already performed by
    :class:`~fastapi_tenancy.middleware.tenancy.TenancyMiddleware` and
    this dependency is not needed.

    Args:
        tenant: Injected current tenant.

    Returns:
        The current :class:`~fastapi_tenancy.core.types.Tenant`.

    Raises:
        HTTPException: 403 Forbidden when the tenant is not active.

    Example::

        @app.post("/webhook/{event}")
        async def webhook(
            event: str,
            tenant: Tenant = Depends(require_active_tenant),
        ):
            ...
    """
    if not tenant.is_active():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Tenant {tenant.identifier!r} is {tenant.status.value}.",
        )
    return tenant


async def get_tenant_config(
    tenant: Annotated[Tenant, Depends(get_current_tenant)],
) -> TenantConfig:
    """Build a :class:`~fastapi_tenancy.core.types.TenantConfig` from the current tenant's metadata.

    All :class:`~fastapi_tenancy.core.types.TenantConfig` fields have
    defaults so this dependency never raises even when the tenant's
    metadata dict is empty or missing expected keys.

    Args:
        tenant: Injected current tenant.

    Returns:
        A :class:`~fastapi_tenancy.core.types.TenantConfig` instance hydrated
        from ``tenant.metadata``.

    Example::

        @app.get("/config")
        async def show_config(
            cfg: TenantConfig = Depends(get_tenant_config),
        ):
            return {
                "max_users": cfg.max_users,
                "features": cfg.features_enabled,
                "rate_limit": cfg.rate_limit_per_minute,
            }
    """
    return TenantConfig(
        max_users=tenant.metadata.get("max_users"),
        max_storage_gb=tenant.metadata.get("max_storage_gb"),
        features_enabled=tenant.metadata.get("features_enabled", []),
        rate_limit_per_minute=tenant.metadata.get("rate_limit_per_minute", 100),
        custom_settings=tenant.metadata.get("custom_settings", {}),
    )


__all__ = ["get_tenant_config", "get_tenant_db", "require_active_tenant"]
