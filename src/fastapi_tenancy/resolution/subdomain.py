"""Subdomain-based tenant resolution strategy.

Extracts the tenant identifier from the leftmost subdomain component of the
request ``Host`` header.

Example mapping::

    acme-corp.example.com     → "acme-corp"
    widgets-inc.example.com   → "widgets-inc"
    app.acme-corp.example.com → "acme-corp"  (rightmost before suffix)

Requirements
------------
* Wildcard DNS record: ``*.example.com → <your server>``
* A wildcard TLS certificate covering ``*.example.com``
* :attr:`~fastapi_tenancy.core.config.TenancyConfig.domain_suffix` configured
  to ``".example.com"``

Advantages
----------
* Clean, branded per-tenant URLs.
* Natural browser isolation (separate cookie jars per subdomain).
* SEO-friendly.
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


class SubdomainTenantResolver(BaseTenantResolver):
    """Resolve tenant from the subdomain component of the request hostname.

    Multi-level subdomains (``app.acme-corp.example.com``) are handled by
    using the rightmost subdomain segment immediately before the configured
    ``domain_suffix`` — so the result is ``"acme-corp"``.

    Args:
        domain_suffix: Base domain suffix (e.g. ``".example.com"`` or
            ``"example.com"`` — the leading dot is added automatically).
        tenant_store: Storage backend for tenant lookup.

    Example::

        resolver = SubdomainTenantResolver(
            domain_suffix=".example.com",
            tenant_store=store,
        )
        # Request to https://acme-corp.example.com/api
        tenant = await resolver.resolve(request)  # → Tenant(identifier="acme-corp")
    """

    def __init__(
        self,
        domain_suffix: str,
        tenant_store: TenantStore | None = None,
    ) -> None:
        super().__init__(tenant_store)
        suffix = domain_suffix.lower().strip()
        self._domain_suffix = suffix if suffix.startswith(".") else f".{suffix}"
        logger.debug("SubdomainTenantResolver suffix=%r", self._domain_suffix)

    async def resolve(self, request: Request) -> Tenant:
        """Extract the tenant identifier from the request hostname subdomain.

        Args:
            request: Incoming FastAPI / Starlette request.

        Returns:
            The resolved :class:`~fastapi_tenancy.core.types.Tenant`.

        Raises:
            TenantResolutionError: When the hostname is absent, does not match
                the configured suffix, has no subdomain, or the extracted
                identifier has an invalid format.
            TenantNotFoundError: When no tenant matches the extracted identifier.
        """
        host = request.url.hostname
        if not host:
            raise TenantResolutionError(
                reason="No hostname found in request.",
                strategy="subdomain",
            )

        host = host.lower()

        if not host.endswith(self._domain_suffix):
            raise TenantResolutionError(
                reason="Request hostname does not match the configured domain suffix.",
                strategy="subdomain",
                details={"expected_suffix": self._domain_suffix},
            )

        subdomain_part = host[: -len(self._domain_suffix)]
        if not subdomain_part:
            raise TenantResolutionError(
                reason="No subdomain found — request is to the apex domain.",
                strategy="subdomain",
                details={"hint": "Use tenant.example.com, not example.com."},
            )

        # For multi-level subdomains (app.acme-corp), use the rightmost segment.
        identifier = subdomain_part.rsplit(".", maxsplit=1)[-1]

        if not self.validate_tenant_identifier(identifier):
            raise TenantResolutionError(
                reason="Subdomain is not a valid tenant identifier.",
                strategy="subdomain",
                details={
                    "hint": (
                        "Must be 3-63 characters, start with a lowercase letter, "
                        "and contain only lowercase letters, digits, and hyphens."
                    )
                },
            )

        logger.debug("Resolving tenant from subdomain identifier=%r", identifier)
        return await self.get_tenant_by_identifier(identifier)


__all__ = ["SubdomainTenantResolver"]
