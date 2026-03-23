"""Async-safe tenant context management using :mod:`contextvars`.

Each async task (i.e. each HTTP request handled by FastAPI) automatically
receives its own copy of every ``ContextVar``, so tenant state set in the
middleware layer is naturally isolated from every other concurrent request
without any explicit locking.

Public surface
--------------
``TenantContext``
    Namespace of static methods managing both the current ``Tenant`` and an
    auxiliary metadata dictionary.

``tenant_scope(tenant)``
    Async context manager that sets a tenant for the duration of a block and
    correctly restores the previous state on exit — recommended for background
    tasks and tests.

``get_current_tenant()``
    FastAPI dependency returning the current ``Tenant`` or raising on miss.
``get_current_tenant_optional()``
    FastAPI dependency returning the current ``Tenant`` or ``None``.

Thread-safety note
------------------
``contextvars`` variables are isolated per asyncio task (and per OS thread).
No locks are needed.  Setting a variable in one coroutine does *not* affect
any concurrently running coroutine.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any

from fastapi_tenancy.core.exceptions import TenantNotFoundError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi_tenancy.core.types import Tenant

#########################################################################
# Module-level context variables                                        #
#                                                                       #
# Defined at module scope (not as class attributes) so they cannot be   #
# accidentally overwritten by subclassing TenantContext.                #
#########################################################################

_tenant_ctx: ContextVar[Tenant | None] = ContextVar("_tenant_ctx", default=None)
_metadata_ctx: ContextVar[dict[str, Any] | None] = ContextVar("_metadata_ctx", default=None)


class TenantContext:
    """Namespace for async-safe per-request tenant context.

    All methods are static.  This class is never instantiated — it exists
    purely as a namespace grouping related operations.

    Usage in ASGI middleware::

        token = TenantContext.set(tenant)
        try:
            await call_next(request)
        finally:
            TenantContext.reset(token)   # restore, not clear

    Usage in route handlers / dependencies::

        tenant = TenantContext.get()          # raises if not set
        tenant = TenantContext.get_optional() # returns None if not set
    """

    ####################
    # Tenant accessors #
    ####################

    @staticmethod
    def set(tenant: Tenant) -> Token[Tenant | None]:
        """Set *tenant* as the current request's tenant.

        Args:
            tenant: The resolved tenant to make current.

        Returns:
            A ``Token`` that can restore the previous state via ``reset()``.
            Always pass this token to ``reset()`` rather than calling
            ``clear()``, which discards all context unconditionally.
        """
        return _tenant_ctx.set(tenant)

    @staticmethod
    def get() -> Tenant:
        """Return the current tenant; raise ``TenantNotFoundError`` if unset.

        Returns:
            The currently active ``Tenant``.

        Raises:
            TenantNotFoundError: When called outside a tenancy-aware request
                (e.g. from a background task that did not set the context).
        """
        tenant = _tenant_ctx.get()
        if tenant is None:
            raise TenantNotFoundError(
                details={
                    "hint": (
                        "Ensure the request passed through TenancyMiddleware, "
                        "or use tenant_scope() for background tasks."
                    )
                }
            )
        return tenant

    @staticmethod
    def get_optional() -> Tenant | None:
        """Return the current tenant, or ``None`` if none is set.

        Use this variant for endpoints that can serve both anonymous and
        tenant-scoped requests.

        Returns:
            The active ``Tenant``, or ``None``.
        """
        return _tenant_ctx.get()

    @staticmethod
    def reset(token: Token[Tenant | None]) -> None:
        """Restore the tenant context to the state captured in *token*.

        Prefer this over ``clear()`` when managing nested scopes — it
        correctly restores a *previous* tenant rather than unconditionally
        setting the context to ``None``.

        Args:
            token: Token returned by a previous ``set()`` call.
        """
        _tenant_ctx.reset(token)

    @staticmethod
    def clear() -> tuple[Token[Tenant | None], Token[dict[str, Any] | None]]:
        """Set both the tenant and all metadata to ``None`` in the current context.

        Returns tokens that callers can pass to :meth:`reset_all` to restore
        the previous state.  This makes ``clear()`` safe for use at any scope
        depth — not just the outermost one.

        **When to use ``clear()`` vs ``reset(token)``**

        - :meth:`reset` / ``reset_all``: prefer this in all middleware and
          nested-scope code where a previous state (possibly a non-``None``
          tenant) should be restored on exit.  This is what
          :func:`tenant_scope` and :class:`~fastapi_tenancy.middleware.tenancy.TenancyMiddleware`
          use.

        - ``clear()`` + :meth:`reset_all`: use this in **test fixtures** and
          application startup/shutdown where you *want* to unconditionally
          discard any inherited context.  Call ``reset_all(*clear())`` in the
          fixture teardown to restore whatever was there before (usually
          ``None``, but safe either way).

        Returns:
            A ``(tenant_token, metadata_token)`` tuple.  Pass both to
            :meth:`reset_all` to restore the previous state.

        Example — test fixture (safe at any nesting depth):

        .. code-block:: python

            @pytest.fixture(autouse=True)
            def clean_context():
                tokens = TenantContext.clear()
                yield
                TenantContext.reset_all(*tokens)
        """
        tenant_token = _tenant_ctx.set(None)
        meta_token = _metadata_ctx.set(None)
        return tenant_token, meta_token

    @staticmethod
    def reset_all(
        tenant_token: Token[Tenant | None],
        metadata_token: Token[dict[str, Any] | None],
    ) -> None:
        """Restore both context variables to the state captured by *clear()*.

        This is the counterpart to :meth:`clear`.  Use it in ``finally``
        blocks or fixture teardowns to restore exactly the state that existed
        before ``clear()`` was called, regardless of scope depth.

        Args:
            tenant_token: Token returned by a previous :meth:`clear` call.
            metadata_token: Token returned by the same :meth:`clear` call.

        Example:

        .. code-block:: python

            tokens = TenantContext.clear()
            try:
                ...
            finally:
                TenantContext.reset_all(*tokens)
        """
        _tenant_ctx.reset(tenant_token)
        _metadata_ctx.reset(metadata_token)

    ######################
    # Metadata accessors #
    ######################

    @staticmethod
    def set_metadata(key: str, value: Any) -> None:
        """Attach a key-value pair to the current request's tenant context.

        Metadata is isolated per request, just like the tenant itself.  It is
        useful for propagating request-scoped state (request ID, user ID,
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
            The stored value, or *default* when the key does not exist.
        """
        meta = _metadata_ctx.get()
        if meta is None:
            return default
        return meta.get(key, default)

    @staticmethod
    def get_all_metadata() -> dict[str, Any]:
        """Return a copy of all metadata in the current context.

        Returns:
            A plain dictionary.  Mutating it does not affect the context.
        """
        meta = _metadata_ctx.get()
        return dict(meta) if meta is not None else {}

    @staticmethod
    def clear_metadata() -> Token[dict[str, Any] | None]:
        """Set all metadata to ``None`` while keeping the tenant set.

        Returns a token that callers can pass to ``_metadata_ctx.reset(token)``
        to restore the previous metadata state.  This makes the method safe for
        use in nested scopes — not just at the outermost request level.

        Useful in middleware that wants to reset per-request supplementary data
        without disturbing the tenant identity.

        Returns:
            A ``Token`` that can restore the previous metadata state.

        Example:

        .. code-block:: python

            token = TenantContext.clear_metadata()
            try:
                ...
            finally:
                _metadata_ctx.reset(token)  # or just let it drop at outermost scope
        """
        return _metadata_ctx.set(None)


##############################################################
# Recommended context manager for background tasks and tests #
##############################################################


@asynccontextmanager
async def tenant_scope(tenant: Tenant) -> AsyncIterator[Tenant]:
    """Async context manager that activates a tenant scope for a block.

    Sets *tenant* as the current tenant for the duration of the ``async with``
    block and restores the previous state on exit — even if an exception is
    raised.  This is the recommended pattern for background tasks and tests.

    Unlike middleware-style usage, this always uses ``reset(token)`` to
    restore, not ``clear()``, making it safe for nested usage.

    Args:
        tenant: The tenant to activate for the duration of the block.

    Yields:
        The active ``Tenant`` (same object as *tenant*).

    Example — background task::

        async with tenant_scope(tenant) as t:
            await process_tenant_data(t)
        # Previous context (usually None) is restored here.

    Example — nested scopes::

        async with tenant_scope(outer_tenant):
            async with tenant_scope(inner_tenant):
                assert TenantContext.get() is inner_tenant
            assert TenantContext.get() is outer_tenant
    """
    token = _tenant_ctx.set(tenant)
    meta_token = _metadata_ctx.set(None)
    try:
        yield tenant
    finally:
        _tenant_ctx.reset(token)
        _metadata_ctx.reset(meta_token)


################################
# FastAPI dependency functions #
################################


def get_current_tenant() -> Tenant:
    """FastAPI dependency — return the current tenant or raise 500.

    Inject this via ``Depends`` in any route that requires a tenant::

        from fastapi import Depends
        from fastapi_tenancy.core.context import get_current_tenant

        @app.get("/users")
        async def list_users(tenant: Tenant = Depends(get_current_tenant)):
            ...

    The middleware populates the context before calling route handlers, so
    this dependency succeeds for every request that passes through
    ``TenancyMiddleware``.

    Returns:
        The currently active ``Tenant``.

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
        The active ``Tenant``, or ``None``.
    """
    return TenantContext.get_optional()


__all__ = [
    "TenantContext",
    "get_current_tenant",
    "get_current_tenant_optional",
    "tenant_scope",
]
