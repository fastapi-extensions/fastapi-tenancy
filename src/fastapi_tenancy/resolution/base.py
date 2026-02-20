"""Abstract base class for tenant resolution strategies.

All resolution strategies — header, subdomain, path, JWT, and custom — derive
from :class:`BaseTenantResolver` and implement a single abstract method:
:meth:`resolve`.  The base class provides common helpers for store lookup and
identifier validation so concrete implementations stay thin.

Extension pattern::

    from fastapi_tenancy.resolution.base import BaseTenantResolver
    from fastapi_tenancy.core.exceptions import TenantResolutionError

    class CookieTenantResolver(BaseTenantResolver):
        def __init__(self, cookie_name: str, tenant_store: TenantStore) -> None:
            super().__init__(tenant_store)
            self._cookie = cookie_name

        async def resolve(self, request: Request) -> Tenant:
            value = request.cookies.get(self._cookie)
            if not value:
                raise TenantResolutionError(
                    reason="Tenant cookie not found",
                    strategy="cookie",
                )
            return await self.get_tenant_by_identifier(value)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from fastapi_tenancy.core.exceptions import TenantNotFoundError

if TYPE_CHECKING:
    from fastapi_tenancy.core.types import Tenant
    from fastapi_tenancy.storage.tenant_store import TenantStore

logger = logging.getLogger(__name__)


class BaseTenantResolver(ABC):
    """Abstract base class for tenant resolution strategies.

    Concrete subclasses must implement :meth:`resolve`.  The base class
    supplies :meth:`get_tenant_by_identifier` and
    :meth:`validate_tenant_identifier` as shared utilities.

    Args:
        tenant_store: Storage backend used by :meth:`get_tenant_by_identifier`.
            May be ``None`` for resolvers that embed the tenant ID directly in
            the request (e.g. a signed JWT) without a secondary store lookup —
            though most strategies will require a store.
    """

    def __init__(self, tenant_store: TenantStore | None = None) -> None:
        self._store = tenant_store
        logger.debug("Initialised %s", type(self).__name__)

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def resolve(self, request: Any) -> Tenant:
        """Extract and return the current tenant from *request*.

        Args:
            request: A FastAPI / Starlette :class:`~starlette.requests.Request`.

        Returns:
            The resolved :class:`~fastapi_tenancy.core.types.Tenant`.

        Raises:
            TenantResolutionError: When the request does not carry sufficient
                information to identify a tenant.
            TenantNotFoundError: When the identifier is valid but matches no
                known tenant.
        """

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    async def get_tenant_by_identifier(self, identifier: str) -> Tenant:
        """Look up *identifier* in the configured store and return the tenant.

        Args:
            identifier: Human-readable tenant slug.

        Returns:
            The matching :class:`~fastapi_tenancy.core.types.Tenant`.

        Raises:
            ValueError: When no store was configured.
            TenantNotFoundError: When no tenant with *identifier* exists.
        """
        if self._store is None:
            raise ValueError(
                f"{type(self).__name__} requires a tenant_store to be configured."
            )
        try:
            tenant = await self._store.get_by_identifier(identifier)
            logger.debug(
                "Resolved identifier=%r to tenant id=%s via %s",
                identifier,
                tenant.id,
                type(self).__name__,
            )
            return tenant
        except TenantNotFoundError:
            logger.warning(
                "Tenant not found: identifier=%r strategy=%s",
                identifier,
                type(self).__name__,
            )
            raise

    @staticmethod
    def validate_tenant_identifier(identifier: str) -> bool:
        """Return ``True`` if *identifier* passes the tenant slug validation rules.

        Delegates to :func:`~fastapi_tenancy.utils.validation.validate_tenant_identifier`
        so validation is consistent across the entire library.

        Args:
            identifier: Value to validate.

        Returns:
            ``True`` when *identifier* is a valid tenant slug.
        """
        from fastapi_tenancy.utils.validation import validate_tenant_identifier

        return validate_tenant_identifier(identifier)


__all__ = ["BaseTenantResolver"]
