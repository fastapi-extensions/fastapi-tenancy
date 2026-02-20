"""Async-safe tenant context management using :mod:`contextvars`.

Each async task (i.e. each HTTP request handled by FastAPI) automatically
receives its own copy of every :class:`~contextvars.ContextVar`, so tenant
state set in the middleware layer is naturally isolated from every other
concurrent request without any explicit locking.

Public surface
--------------
:class:`TenantContext`
    Class with only static methods — acts as a namespace rather than an
    instance.  Manages both the current :class:`~fastapi_tenancy.core.types.Tenant`
    and an auxiliary metadata dictionary.

:func:`get_current_tenant`
    FastAPI-compatible dependency that returns the current
    :class:`~fastapi_tenancy.core.types.Tenant` or raises
    :class:`~fastapi_tenancy.core.exceptions.TenantNotFoundError`.

:func:`get_current_tenant_optional`
    FastAPI-compatible dependency that returns the current tenant or ``None``.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any

from fastapi_tenancy.core.exceptions import TenantNotFoundError

if TYPE_CHECKING:
    from fastapi_tenancy.core.types import Tenant

# ---------------------------------------------------------------------------
# Module-level context variables
#
# One ContextVar per piece of per-request state.  They are *not* class
# attributes so they cannot be accidentally overwritten by subclassing.
# ---------------------------------------------------------------------------

_tenant_ctx: ContextVar[Tenant | None] = ContextVar("tenant", default=None)
_metadata_ctx: ContextVar[dict[str, Any] | None] = ContextVar(
    "tenant_metadata", default=None
)


class TenantContext:
    """Namespace for async-safe per-request tenant context.

    All methods are static; this class is never instantiated.

    Thread-safety
    -------------
    :mod:`contextvars` context variables are isolated per asyncio task (and
    per thread).  No locks are required.  Setting a variable in one coroutine
    does *not* affect any other coroutine running concurrently.

    Usage in middleware::

        token = TenantContext.set(tenant)
        try:
            response = await call_next(request)
        finally:
            TenantContext.reset(token)

    Usage in route handlers / dependencies::

        tenant = TenantContext.get()   # raises if not set
        # or
        tenant = TenantContext.get_optional()  # returns None if not set
    """

    # ------------------------------------------------------------------
    # Tenant accessors
    # ------------------------------------------------------------------

    @staticmethod
    def set(tenant: Tenant) -> Token[Tenant | None]:
        """Set *tenant* as the current request's tenant.

        Args:
            tenant: The resolved tenant to make current.

        Returns:
            A :class:`~contextvars.Token` that can restore the previous
            state via :meth:`reset`.  Always pass this token to ``reset``
            rather than calling :meth:`clear`, which discards all context.
        """
        return _tenant_ctx.set(tenant)

    @staticmethod
    def get() -> Tenant:
        """Return the current tenant, raising if none is set.

        Returns:
            The currently active :class:`~fastapi_tenancy.core.types.Tenant`.

        Raises:
            TenantNotFoundError: When called outside a tenancy-aware request
                (e.g. from a background task that did not set the context).
        """
        tenant = _tenant_ctx.get()
        if tenant is None:
            raise TenantNotFoundError(
                "No tenant is set in the current execution context. "
                "Ensure the request passed through TenancyMiddleware."
            )
        return tenant

    @staticmethod
    def get_optional() -> Tenant | None:
        """Return the current tenant, or ``None`` if none is set.

        Use this variant for endpoints or utilities that can operate in
        both tenant-aware and tenant-agnostic modes.

        Returns:
            The currently active tenant, or ``None``.
        """
        return _tenant_ctx.get()

    @staticmethod
    def reset(token: Token[Tenant | None]) -> None:
        """Restore the tenant context to the state captured in *token*.

        Prefer this over :meth:`clear` when managing nested scopes; it
        correctly restores a previous tenant rather than unconditionally
        setting the context to ``None``.

        Args:
            token: Token returned by a previous :meth:`set` call.
        """
        _tenant_ctx.reset(token)

    @staticmethod
    def clear() -> None:
        """Clear both the tenant and all metadata from the current context.

        Typically called in middleware ``finally`` blocks to guarantee cleanup
        after request processing, regardless of whether an exception occurred.
        """
        _tenant_ctx.set(None)
        _metadata_ctx.set(None)

    # ------------------------------------------------------------------
    # Metadata accessors
    # ------------------------------------------------------------------

    @staticmethod
    def set_metadata(key: str, value: Any) -> None:
        """Attach a key-value pair to the current request's tenant context.

        Metadata is isolated per request, just like the tenant itself.  It
        is useful for propagating request-scoped state (request ID, user ID,
        feature flags) without threading it through every function signature.

        Args:
            key: Metadata key.
            value: Metadata value (any JSON-serialisable type recommended).

        Example::

            TenantContext.set_metadata("request_id", str(uuid4()))
            TenantContext.set_metadata("user_id", "user-abc")
        """
        existing = _metadata_ctx.get()
        updated = dict(existing) if existing is not None else {}
        updated[key] = value
        _metadata_ctx.set(updated)

    @staticmethod
    def get_metadata(key: str, default: Any = None) -> Any:
        """Retrieve a metadata value from the current request context.

        Args:
            key: Metadata key.
            default: Value returned when the key is absent.

        Returns:
            The stored value, or *default* if the key does not exist.
        """
        meta = _metadata_ctx.get()
        if meta is None:
            return default
        return meta.get(key, default)

    @staticmethod
    def get_all_metadata() -> dict[str, Any]:
        """Return a copy of all metadata in the current context.

        Returns:
            A plain dictionary; mutating it does not affect the context.
        """
        meta = _metadata_ctx.get()
        return dict(meta) if meta is not None else {}

    @staticmethod
    def clear_metadata() -> None:
        """Clear all metadata while keeping the tenant set.

        Useful in middleware that wants to reset per-request supplementary
        data without disturbing the tenant identity.
        """
        _metadata_ctx.set(None)

    # ------------------------------------------------------------------
    # Scope context manager
    # ------------------------------------------------------------------

    class scope:
        """Context manager for temporary tenant scope.

        Sets a tenant for the duration of a ``with`` or ``async with``
        block and restores the previous state on exit — even if an
        exception is raised.

        This is the recommended pattern for background tasks and tests::

            async with TenantContext.scope(tenant):
                await process_tenant_data()
            # Previous context (usually None) is restored here.

        The class supports both synchronous and asynchronous usage so it
        can be used from regular functions, coroutines, and
        :func:`asyncio.create_task` workers.
        """

        def __init__(self, tenant: Tenant) -> None:
            """Initialise the scope with the tenant to activate.

            Args:
                tenant: The tenant that will be current inside the scope.
            """
            self._tenant = tenant
            self._token: Token[Tenant | None] | None = None
            self._meta_token: Token[dict[str, Any] | None] | None = None

        # Async protocol ------------------------------------------------

        async def __aenter__(self) -> Tenant:
            """Enter the async scope and return the active tenant."""
            self._token = _tenant_ctx.set(self._tenant)
            self._meta_token = _metadata_ctx.set(None)
            return self._tenant

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc_val: BaseException | None,
            exc_tb: Any,
        ) -> None:
            """Exit the async scope and restore the previous context."""
            if self._token is not None:
                _tenant_ctx.reset(self._token)
            if self._meta_token is not None:
                _metadata_ctx.reset(self._meta_token)

        # Sync protocol -------------------------------------------------

        def __enter__(self) -> Tenant:
            """Enter the synchronous scope and return the active tenant."""
            self._token = _tenant_ctx.set(self._tenant)
            self._meta_token = _metadata_ctx.set(None)
            return self._tenant

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_val: BaseException | None,
            exc_tb: Any,
        ) -> None:
            """Exit the synchronous scope and restore the previous context."""
            if self._token is not None:
                _tenant_ctx.reset(self._token)
            if self._meta_token is not None:
                _metadata_ctx.reset(self._meta_token)


# ---------------------------------------------------------------------------
# FastAPI dependency functions
# ---------------------------------------------------------------------------


def get_current_tenant() -> Tenant:
    """FastAPI dependency — return the current tenant or raise 500.

    Inject this via ``Depends`` in any route that requires a tenant::

        @app.get("/users")
        async def list_users(tenant: Tenant = Depends(get_current_tenant)):
            ...

    The middleware populates the context before calling route handlers, so
    this dependency succeeds for every request that passes through
    :class:`~fastapi_tenancy.middleware.tenancy.TenancyMiddleware`.

    Returns:
        The currently active :class:`~fastapi_tenancy.core.types.Tenant`.

    Raises:
        TenantNotFoundError: When no tenant is set (route bypassed the
            middleware — misconfiguration).
    """
    return TenantContext.get()


def get_current_tenant_optional() -> Tenant | None:
    """FastAPI dependency — return the current tenant or ``None``.

    Use in routes that can serve both anonymous and tenant-scoped requests::

        @app.get("/status")
        async def status(tenant: Tenant | None = Depends(get_current_tenant_optional)):
            if tenant:
                return tenant_status(tenant)
            return global_status()

    Returns:
        The active :class:`~fastapi_tenancy.core.types.Tenant`, or ``None``.
    """
    return TenantContext.get_optional()


__all__ = [
    "TenantContext",
    "get_current_tenant",
    "get_current_tenant_optional",
]
