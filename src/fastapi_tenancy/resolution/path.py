"""Path-based tenant resolution strategy.

Extracts the tenant identifier from a fixed URL path prefix.

URL scheme::

    {path_prefix}/{tenant_identifier}/...

Examples with default prefix ``/tenants``::

    /tenants/acme-corp/users          → "acme-corp"
    /tenants/widgets-inc/orders/123   → "widgets-inc"
    /tenants/my-org/api/v2/items      → "my-org"

Advantages
----------
* No DNS or TLS wildcard required — a single domain serves all tenants.
* Works in environments that do not support custom subdomains.
* Easy to route in API gateways and reverse proxies.

Disadvantage
------------
* Tenant identifier is visible in every URL — ensure it is not sensitive.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi_tenancy.core.exceptions import TenantResolutionError
from fastapi_tenancy.resolution.base import BaseTenantResolver

if TYPE_CHECKING:
    from fastapi import Request

    from fastapi_tenancy.core.types import Tenant
    from fastapi_tenancy.storage.tenant_store import TenantStore

logger = logging.getLogger(__name__)


class PathTenantResolver(BaseTenantResolver):
    """Resolve tenant from a fixed URL path prefix.

    Args:
        path_prefix: Path segment that immediately precedes the tenant
            identifier.  Trailing slashes are stripped automatically.
            Defaults to ``"/tenants"``.
        tenant_store: Storage backend for tenant lookup.

    Example::

        resolver = PathTenantResolver(path_prefix="/tenants", tenant_store=store)
        # GET /tenants/acme-corp/users
        tenant = await resolver.resolve(request)  # → Tenant(identifier="acme-corp")
    """

    def __init__(
        self,
        path_prefix: str = "/tenants",
        tenant_store: TenantStore | None = None,
    ) -> None:
        super().__init__(tenant_store)
        self._prefix = path_prefix.rstrip("/")
        logger.debug("PathTenantResolver prefix=%r", self._prefix)

    async def resolve(self, request: Request) -> Tenant:
        """Extract the tenant identifier from the request URL path.

        Args:
            request: Incoming FastAPI / Starlette request.

        Returns:
            The resolved :class:`~fastapi_tenancy.core.types.Tenant`.

        Raises:
            TenantResolutionError: When the path does not start with the
                configured prefix, the prefix is not followed by a tenant
                identifier, or the identifier has an invalid format.
            TenantNotFoundError: When no tenant matches the extracted identifier.
        """
        path = request.url.path

        if not path.startswith(self._prefix):
            raise TenantResolutionError(
                reason="Request path does not start with the configured prefix.",
                strategy="path",
                details={"expected_prefix": self._prefix},
            )

        remainder = path[len(self._prefix):].lstrip("/")
        if not remainder:
            raise TenantResolutionError(
                reason="No tenant identifier found after the path prefix.",
                strategy="path",
                details={"prefix": self._prefix},
            )

        identifier = remainder.split("/")[0]

        if not self.validate_tenant_identifier(identifier):
            raise TenantResolutionError(
                reason="Path segment is not a valid tenant identifier.",
                strategy="path",
                details={
                    "hint": (
                        "Must be 3-63 characters, start with a lowercase letter, "
                        "and contain only lowercase letters, digits, and hyphens."
                    )
                },
            )

        logger.debug("Resolving tenant from path identifier=%r", identifier)
        return await self.get_tenant_by_identifier(identifier)


__all__ = ["PathTenantResolver"]
