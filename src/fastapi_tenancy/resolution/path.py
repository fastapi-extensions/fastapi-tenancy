"""URL path-based tenant resolution strategy.

Extracts the tenant identifier from a fixed prefix in the URL path.

Expected path format::

    /tenants/{identifier}/...

Example::

    GET /tenants/acme-corp/orders  →  identifier: "acme-corp"

The prefix is configurable (default: ``"/tenants"``).  After extraction the
remaining path is stored in ``request.state.tenant_path_remainder`` so that
route handlers can use it if needed.

Use case
--------
Preferred when subdomains are not available (e.g. single-domain deployments,
mobile apps, B2B APIs consumed by server-side clients).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi_tenancy.core.exceptions import TenantResolutionError
from fastapi_tenancy.resolution.base import BaseTenantResolver
from fastapi_tenancy.utils.validation import validate_tenant_identifier

if TYPE_CHECKING:
    from starlette.requests import Request

    from fastapi_tenancy.core.types import Tenant
    from fastapi_tenancy.storage.tenant_store import TenantStore

logger = logging.getLogger(__name__)


class PathTenantResolver(BaseTenantResolver):
    """Resolve the current tenant from the URL path prefix.

    Args:
        store: Tenant metadata store.
        path_prefix: The fixed path prefix before the tenant identifier
            (default: ``"/tenants"``).

    Example::

        resolver = PathTenantResolver(store, path_prefix="/tenants")

        # Request: GET /tenants/acme-corp/orders
        tenant = await resolver.resolve(request)
        # → Tenant(identifier="acme-corp", …)
        # request.state.tenant_path_remainder == "/orders"
    """

    def __init__(
        self,
        store: TenantStore[Tenant],
        path_prefix: str = "/tenants",
    ) -> None:
        super().__init__(store)
        # Normalise: strip trailing slash, ensure leading slash.
        self._prefix = "/" + path_prefix.strip("/")

    async def resolve(self, request: Request) -> Tenant:
        """Extract the tenant identifier from the request path.

        Args:
            request: Incoming HTTP request.

        Returns:
            Resolved :class:`~fastapi_tenancy.core.types.Tenant`.

        Raises:
            TenantResolutionError: When the path does not match the expected
                format or the identifier fails validation.
            TenantNotFoundError: When the identifier has no matching tenant.
        """
        path = request.url.path
        prefix_with_slash = self._prefix.rstrip("/") + "/"

        if not path.startswith(prefix_with_slash):
            raise TenantResolutionError(
                reason=(
                    f"Path {path!r} does not start with expected prefix "
                    f"{prefix_with_slash!r}"
                ),
                strategy="path",
            )

        remainder = path[len(prefix_with_slash):]
        # The identifier is the first path segment after the prefix.
        identifier = remainder.split("/")[0]
        if not identifier:
            raise TenantResolutionError(
                reason=f"No tenant identifier found after prefix {self._prefix!r}",
                strategy="path",
            )
        if not validate_tenant_identifier(identifier):
            raise TenantResolutionError(
                reason=f"Path segment {identifier!r} is not a valid tenant identifier",
                strategy="path",
            )

        # Store the remainder (path after the tenant segment) on request state
        # for downstream handlers that need it.
        path_after_tenant = remainder[len(identifier):]
        request.state.tenant_path_remainder = path_after_tenant or "/"

        logger.debug("Path resolver: path=%r → identifier=%r", path, identifier)
        return await self.store.get_by_identifier(identifier)


__all__ = ["PathTenantResolver"]
