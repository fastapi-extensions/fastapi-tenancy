"""Abstract base class for data isolation strategies.

All isolation providers — schema, database, RLS, hybrid — derive from
:class:`BaseIsolationProvider` and implement its abstract interface.

The base class provides:

* A ``config`` attribute for access to the application-wide
  :class:`~fastapi_tenancy.core.config.TenancyConfig`.
* Helper methods for computing schema names and database URLs.
* A default :meth:`verify_isolation` that subclasses may override.
* Duck-type compatibility with the :class:`~fastapi_tenancy.core.types.IsolationProvider`
  structural protocol — any object exposing the same async methods satisfies
  the protocol without inheriting this class.

Extension pattern::

    from contextlib import asynccontextmanager
    from fastapi_tenancy.isolation.base import BaseIsolationProvider

    class RedisIsolationProvider(BaseIsolationProvider):
        @asynccontextmanager
        async def get_session(self, tenant):
            conn = await self._get_redis_client(tenant)
            try:
                yield conn
            finally:
                await conn.aclose()

        async def apply_filters(self, query, tenant):
            return query  # Redis is key-namespaced; no SQL filter needed

        async def initialize_tenant(self, tenant):
            # Allocate Redis keyspace prefix
            pass

        async def destroy_tenant(self, tenant, **kwargs):
            # SCAN + DEL all tenant keys
            pass
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy import MetaData
    from sqlalchemy.ext.asyncio import AsyncSession

    from fastapi_tenancy.core.config import TenancyConfig
    from fastapi_tenancy.core.types import SelectT, Tenant

logger = logging.getLogger(__name__)


class BaseIsolationProvider(ABC):
    """Abstract base class for data isolation strategies.

    Isolation providers are responsible for:

    1. **Scoping sessions** — returning a database session configured so
       that queries automatically target the correct tenant namespace
       (:meth:`get_session`).

    2. **Filtering queries** — adding ``WHERE`` clauses or other predicates
       that enforce per-tenant visibility (:meth:`apply_filters`).

    3. **Provisioning** — creating the necessary database structures (schemas,
       tables, RLS policies) when a new tenant is onboarded
       (:meth:`initialize_tenant`).

    4. **Deprovisioning** — removing all tenant data and structures when a
       tenant is deleted (:meth:`destroy_tenant`).

    Type safety
    -----------
    :meth:`apply_filters` is generic over :data:`~fastapi_tenancy.core.types.SelectT`
    (bound to ``sqlalchemy.sql.Select``) so that the concrete query type is
    preserved through the call::

        q: Select[tuple[User]] = select(User)
        filtered: Select[tuple[User]] = await provider.apply_filters(q, tenant)

    Args:
        config: Application-wide tenancy configuration.
    """

    def __init__(self, config: TenancyConfig) -> None:
        self.config = config
        logger.debug("Initialised %s", type(self).__name__)

    ######################
    # Required interface #
    ######################

    @abstractmethod
    def get_session(self, tenant: Tenant) -> AbstractAsyncContextManager[AsyncSession]:
        """Yield a database session scoped to *tenant*'s namespace.

        The session must be fully configured before yielding — i.e. the
        ``search_path``, session variable, or connection must already be
        set so that subsequent queries are automatically isolated.

        Args:
            tenant: The tenant whose data namespace should be active.

        Returns:
            An async context manager that yields a configured
            :class:`~sqlalchemy.ext.asyncio.AsyncSession`.

        Raises:
            IsolationError: When the session cannot be opened or configured.

        Example::

            async with provider.get_session(tenant) as session:
                result = await session.execute(select(User))
                await session.commit()
        """
        ...

    @abstractmethod
    async def apply_filters(self, query: SelectT, tenant: Tenant) -> SelectT:
        """Return *query* filtered to only expose *tenant*'s data.

        The implementation should use SQLAlchemy's ``.where()`` method with
        a properly bound parameter — never string interpolation.

        For strategies that already enforce isolation at the session level
        (e.g. ``DATABASE`` isolation with separate databases per tenant),
        this method may return *query* unchanged.

        The generic bound ``SelectT`` ensures the concrete ``Select`` subtype
        is preserved through the call, so mypy can type-check the result.

        Args:
            query: A SQLAlchemy ``Select`` query construct.
            tenant: The currently active tenant.

        Returns:
            The (potentially) filtered query.
        """

    @abstractmethod
    async def initialize_tenant(
        self,
        tenant: Tenant,
        metadata: MetaData | None = None,
    ) -> None:
        """Provision database structures for a newly created *tenant*.

        Called once when a tenant is registered.  The implementation should
        be idempotent so that repeated calls do not cause errors.

        Args:
            tenant: The newly created tenant.
            metadata: Optional SQLAlchemy ``MetaData`` — when provided, the
                provider creates all mapped tables in the tenant's namespace.

        Raises:
            IsolationError: When provisioning fails.
        """

    @abstractmethod
    async def destroy_tenant(self, tenant: Tenant, **kwargs: Any) -> None:
        """Deprovision and permanently delete all data for *tenant*.

        .. warning::
            This is a **destructive, irreversible** operation.  Ensure the
            caller has appropriate authorisation and a confirmed audit trail
            before invoking.

        Args:
            tenant: The tenant to destroy.
            **kwargs: Provider-specific options.

        Raises:
            IsolationError: When deprovisioning fails.
        """

    ######################
    # Optional overrides #
    ######################

    async def verify_isolation(self, tenant: Tenant) -> bool:
        """Return ``True`` if the tenant's isolation is functioning correctly.

        The base implementation logs a warning and returns ``True``.
        Subclasses should override this with a meaningful check (e.g.
        verify that the schema exists, the database is reachable, RLS
        policies are active).

        Args:
            tenant: The tenant to verify.

        Returns:
            ``True`` when isolation is verified; ``False`` when a problem is
            detected.
        """
        logger.warning(
            "%s does not implement verify_isolation() — returning True",
            type(self).__name__,
        )
        return True

    ##################
    # Shared helpers #
    ##################

    def get_schema_name(self, tenant: Tenant) -> str:
        """Compute the schema name for *tenant* using the global config.

        Delegates to :meth:`~fastapi_tenancy.core.config.TenancyConfig.get_schema_name`
        so the naming convention is always consistent.

        Args:
            tenant: The tenant to compute the schema name for.

        Returns:
            A safe, lowercased schema name string.
        """
        return self.config.get_schema_name(tenant.identifier)

    def get_database_url(self, tenant: Tenant) -> str:
        """Return the database connection URL for *tenant*.

        If the tenant has a ``database_url`` override set, that is returned.
        Otherwise the URL is derived from the global config template.

        Args:
            tenant: The tenant to get the URL for.

        Returns:
            A fully-qualified async database URL string.
        """
        if tenant.database_url:
            return tenant.database_url
        return self.config.get_database_url_for_tenant(tenant.id)


__all__ = ["BaseIsolationProvider"]
