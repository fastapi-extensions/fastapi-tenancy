"""Subdomain-based tenant resolution strategy.

Extracts the tenant identifier from the leftmost subdomain of the incoming
``Host`` / ``X-Forwarded-Host`` header.

Example::

    Host: acme-corp.example.com → identifier: "acme-corp"
    Host: globex.myapp.io       → identifier: "globex"

Security notes
--------------
- Only the leftmost label is extracted; everything after the first ``.`` is
  the configured ``domain_suffix`` and is not used for identification.
- The extracted label is validated against tenant slug rules before lookup.
- ``X-Forwarded-Host`` is used when present (reverse-proxy environments).
  If your deployment does **not** use a trusted reverse proxy, disable
  ``X-Forwarded-Host`` reading by passing ``trust_x_forwarded=False``.
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


class SubdomainTenantResolver(BaseTenantResolver):
    """Resolve the current tenant from the leftmost subdomain.

    Args:
        store: Tenant metadata store.
        domain_suffix: The base domain (e.g. ``".example.com"``).  Used to
            strip the suffix before validation; if the host does not end
            with this suffix the resolver raises.
        trust_x_forwarded: Whether to read ``X-Forwarded-Host`` before
            ``Host``.  Default ``True`` (assume trusted reverse proxy).

    Example::

        resolver = SubdomainTenantResolver(store, domain_suffix=".example.com")

        # Request: Host: acme-corp.example.com
        tenant = await resolver.resolve(request)
        # → Tenant(identifier="acme-corp", …)
    """

    def __init__(
        self,
        store: TenantStore[Tenant],
        domain_suffix: str = "",
        trust_x_forwarded: bool = True,
    ) -> None:
        super().__init__(store)
        # Normalise: always starts with "." unless empty.
        self._domain_suffix = (
            domain_suffix if not domain_suffix or domain_suffix.startswith(".")
            else f".{domain_suffix}"
        )
        self._trust_x_forwarded = trust_x_forwarded

    def _extract_identifier(self, host: str) -> str:
        """Extract and validate the tenant subdomain from *host*.

        Args:
            host: Raw ``Host`` header value (may include port).

        Returns:
            The tenant identifier string.

        Raises:
            TenantResolutionError: When the subdomain cannot be extracted or
                fails validation.
        """
        # Strip port suffix (e.g. "host:8000" → "host").
        hostname = host.split(":", maxsplit=1)[0].lower().strip()

        if self._domain_suffix and not hostname.endswith(self._domain_suffix):
            # _domain_suffix is always normalised to start with "." in __init__,
            # so we can do a single endswith check on the full dotted form.
                raise TenantResolutionError(
                    reason=(
                        f"Host {hostname!r} does not end with "
                        f"configured domain suffix {self._domain_suffix!r}"
                    ),
                    strategy="subdomain",
                )

        parts = hostname.split(".")
        if len(parts) < 2:
            raise TenantResolutionError(
                reason=f"Host {hostname!r} has no subdomain component",
                strategy="subdomain",
            )

        identifier = parts[0]
        if not validate_tenant_identifier(identifier):
            raise TenantResolutionError(
                reason=f"Subdomain {identifier!r} is not a valid tenant identifier",
                strategy="subdomain",
            )
        return identifier

    async def resolve(self, request: Request) -> Tenant:
        """Extract the tenant identifier from the request's hostname.

        Args:
            request: Incoming HTTP request.

        Returns:
            Resolved :class:`~fastapi_tenancy.core.types.Tenant`.

        Raises:
            TenantResolutionError: When the subdomain is absent, does not
                match the configured suffix, or fails validation.
            TenantNotFoundError: When the identifier has no matching tenant.
        """
        host = ""
        if self._trust_x_forwarded:
            host = request.headers.get("x-forwarded-host", "")
        if not host:
            host = request.headers.get("host", "")
        if not host:
            raise TenantResolutionError(
                reason="Neither Host nor X-Forwarded-Host header is present",
                strategy="subdomain",
            )

        identifier = self._extract_identifier(host)
        logger.debug("Subdomain resolver: host=%r → identifier=%r", host, identifier)
        return await self.store.get_by_identifier(identifier)


__all__ = ["SubdomainTenantResolver"]
