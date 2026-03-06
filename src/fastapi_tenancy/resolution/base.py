"""Abstract base class for tenant resolution strategies.

:class:`BaseTenantResolver` is an optional convenience base class.  The only
**required** contract is the :class:`~fastapi_tenancy.core.types.TenantResolver`
structural protocol — any object with an ``async def resolve(request)`` method
satisfies it, whether or not it inherits from this class.

Note: ``TenantResolver`` is a ``@runtime_checkable`` Protocol, not an ABC.
Protocols do not support ``.register()``.  Duck-typing is automatic: any class
with an ``async def resolve(request)`` method satisfies the protocol via
``isinstance(obj, TenantResolver)`` without registration.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.requests import Request

    from fastapi_tenancy.core.types import Tenant
    from fastapi_tenancy.storage.tenant_store import TenantStore

logger = logging.getLogger(__name__)


class BaseTenantResolver(ABC):
    """Optional abstract base class for tenant resolution strategies.

    Subclass this to build a custom resolution strategy::

        class CookieResolver(BaseTenantResolver):
            async def resolve(self, request: Request) -> Tenant:
                tenant_id = request.cookies.get("X-Tenant")
                if not tenant_id:
                    raise TenantResolutionError("Cookie missing", strategy="cookie")
                return await self.store.get_by_identifier(tenant_id)

    Alternatively, implement the ``TenantResolver`` protocol directly —
    duck-typing is sufficient (no inheritance required).

    Args:
        store: The tenant metadata store used to look up tenants.
    """

    def __init__(self, store: TenantStore[Tenant]) -> None:
        self.store = store

    @abstractmethod
    async def resolve(self, request: Request) -> Tenant:
        """Resolve the current tenant from *request*.

        Args:
            request: Incoming FastAPI / Starlette request.

        Returns:
            The resolved :class:`~fastapi_tenancy.core.types.Tenant`.

        Raises:
            TenantResolutionError: When the request does not carry enough
                information to identify a tenant.
            TenantNotFoundError: When the identifier matches no known tenant.
        """


__all__ = ["BaseTenantResolver"]
