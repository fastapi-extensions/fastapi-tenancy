"""FastAPI dependency factories for tenant-scoped sessions and configuration.

Critical fix: closure-based dependency factories
-------------------------------------------------
The original ``get_tenant_db`` dependency attempted to read
``request.app.state.isolation_provider`` at call time — a value that was
never set during ``TenancyManager.create_lifespan()``, causing a runtime
``RuntimeError`` on **every** request in production.

The correct pattern is a **closure-based factory**: the ``TenancyManager``
(which owns the isolation provider) is captured in the closure at startup.
No ``app.state`` lookup is needed.

Usage pattern — in your FastAPI app::

    from fastapi import FastAPI, Depends
    from sqlalchemy.ext.asyncio import AsyncSession
    from fastapi_tenancy import TenancyManager
    from fastapi_tenancy.dependencies import make_tenant_db_dependency

    manager = TenancyManager(config, store)
    # Create the dependency once and reuse everywhere:
    get_tenant_db = make_tenant_db_dependency(manager)

    @app.get("/orders")
    async def list_orders(
        tenant: Annotated[Tenant, Depends(get_current_tenant)],
        session: Annotated[AsyncSession, Depends(get_tenant_db)],
    ):
        ...

Annotated shorthand::

    TenantDep = Annotated[Tenant, Depends(get_current_tenant)]
    TenantOptionalDep = Annotated[Tenant | None, Depends(get_current_tenant_optional)]

    @app.get("/orders")
    async def list_orders(
        tenant: TenantDep,
        session: Annotated[AsyncSession, Depends(get_tenant_db)]
    ):
        ...
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends

from fastapi_tenancy.core.context import get_current_tenant, get_current_tenant_optional
from fastapi_tenancy.core.types import Tenant, TenantConfig

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from fastapi_tenancy.manager import TenancyManager


##################################################
# Re-export context dependencies for convenience #
##################################################

#: Annotated type alias for the current tenant dependency.
#: Use in route function signatures: ``tenant: TenantDep``
TenantDep = Annotated[Tenant, Depends(get_current_tenant)]

#: Annotated type alias for the optional tenant dependency.
#: Use when some routes serve both anonymous and tenant-scoped requests.
TenantOptionalDep = Annotated[Tenant | None, Depends(get_current_tenant_optional)]


####################################
# Closure-based dependency factory #
####################################


def make_tenant_db_dependency(
    manager: TenancyManager,
) -> Any:
    """Create a FastAPI dependency that yields a tenant-scoped ``AsyncSession``.

    The returned async generator function captures *manager* in its closure.
    This is the correct pattern — no ``app.state`` lookup, no circular
    imports, and the dependency works regardless of application startup order.

    Args:
        manager: The configured :class:`~fastapi_tenancy.manager.TenancyManager`.

    Returns:
        An async generator function suitable for use as a FastAPI ``Depends``.

    Example::

        get_tenant_db = make_tenant_db_dependency(manager)

        @app.get("/orders")
        async def list_orders(
            session: Annotated[AsyncSession, Depends(get_tenant_db)],
        ):
            result = await session.execute(select(Order))
    """

    async def _get_tenant_db(
        tenant: Annotated[Tenant, Depends(get_current_tenant)],
    ) -> AsyncIterator[AsyncSession]:
        """Yield a database session scoped to the current tenant.

        Args:
            tenant: The current request's tenant (injected by FastAPI).

        Yields:
            An :class:`~sqlalchemy.ext.asyncio.AsyncSession` configured for
            *tenant*'s isolation namespace.

        Raises:
            IsolationError: When the session cannot be opened.
        """
        async with manager.isolation_provider.get_session(tenant) as session:
            yield session

    return _get_tenant_db


def make_tenant_config_dependency(
    manager: TenancyManager,
) -> Any:
    """Create a FastAPI dependency that yields the current tenant's config.

    Reads ``tenant.metadata`` and constructs a ``TenantConfig`` with typed
    quota and feature-flag fields.  Falls back to defaults when fields are
    absent from the metadata.

    Args:
        manager: The configured :class:`~fastapi_tenancy.manager.TenancyManager`.

    Returns:
        An async function returning a ``TenantConfig`` instance.

    Example::

        get_tenant_config = make_tenant_config_dependency(manager)

        @app.get("/status")
        async def status(
            config: Annotated[TenantConfig, Depends(get_tenant_config)],
        ):
            return {"max_users": config.max_users}
    """

    async def _get_tenant_config(
        tenant: Annotated[Tenant, Depends(get_current_tenant)],
    ) -> TenantConfig:
        """Return the ``TenantConfig`` parsed from the current tenant's metadata.

        Args:
            tenant: The current request's tenant.

        Returns:
            Validated :class:`~fastapi_tenancy.core.types.TenantConfig`.
        """
        return TenantConfig.model_validate(tenant.metadata)

    return _get_tenant_config


def make_audit_log_dependency(
    manager: TenancyManager,
) -> Any:
    """Create a FastAPI dependency that provides an audit-log writer function.

    Returns a callable that the route handler uses to record operations::

        get_audit_logger = make_audit_log_dependency(manager)

        @app.delete("/orders/{order_id}")
        async def delete_order(
            order_id: str,
            audit: Annotated[..., Depends(get_audit_logger)],
            tenant: TenantDep,
        ):
            ...
            await audit(action="delete", resource="order", resource_id=order_id)

    Args:
        manager: The configured :class:`~fastapi_tenancy.manager.TenancyManager`.

    Returns:
        An async function that returns a write-audit-log callable.
    """
    from fastapi_tenancy.core.types import AuditLog  # noqa: PLC0415

    async def _get_audit_logger(
        tenant: Annotated[Tenant, Depends(get_current_tenant)],
    ) -> Any:
        """Return an async function that logs an audit entry for the current tenant.

        Args:
            tenant: Current tenant.

        Returns:
            An ``async def log(action, resource, **kwargs)`` callable.
        """

        async def log(
            action: str,
            resource: str,
            resource_id: str | None = None,
            metadata: dict[str, Any] | None = None,
            user_id: str | None = None,
        ) -> None:
            """Record an audit log entry.

            Args:
                action: Verb (e.g. ``"create"``, ``"delete"``).
                resource: Resource type (e.g. ``"order"``, ``"user"``).
                resource_id: Optional resource identifier.
                metadata: Optional supplementary context.
                user_id: Optional authenticated user ID.
            """
            from datetime import UTC, datetime  # noqa: PLC0415

            entry = AuditLog(
                tenant_id=tenant.id,
                user_id=user_id,
                action=action,
                resource=resource,
                resource_id=resource_id,
                metadata=metadata or {},
                timestamp=datetime.now(UTC),
            )
            # Delegate to manager's audit log writer if configured.
            if hasattr(manager, "write_audit_log"):
                await manager.write_audit_log(entry)

        return log

    return _get_audit_logger


#################################################
# Type aliases (import-time, no manager needed) #
#################################################

__all__ = [
    "TenantDep",
    "TenantOptionalDep",
    "get_current_tenant",
    "get_current_tenant_optional",
    "make_audit_log_dependency",
    "make_tenant_config_dependency",
    "make_tenant_db_dependency",
]
