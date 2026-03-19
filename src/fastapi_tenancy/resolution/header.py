"""Header-based tenant resolution strategy.

Reads the tenant identifier from a configurable HTTP request header
(default: ``X-Tenant-ID``).

Security note
-------------
The header value is treated as an untrusted identifier: it is validated
against tenant slug rules before any database lookup.  Responses never
reveal *why* resolution failed (valid header vs unknown tenant) to avoid
information leakage about which tenant identifiers exist.

All resolution failures — missing header, invalid identifier format, and
unknown tenant — raise :exc:`TenantResolutionError` with the same generic
reason string ``"Tenant not found"`` so that unauthenticated callers cannot
enumerate valid tenant identifiers by comparing error messages.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi_tenancy.core.exceptions import TenantNotFoundError, TenantResolutionError
from fastapi_tenancy.resolution.base import BaseTenantResolver
from fastapi_tenancy.utils.validation import validate_tenant_identifier

if TYPE_CHECKING:
    from starlette.requests import Request

    from fastapi_tenancy.core.types import Tenant
    from fastapi_tenancy.storage.tenant_store import TenantStore

logger = logging.getLogger(__name__)

_GENERIC_REASON = "Tenant not found"


class HeaderTenantResolver(BaseTenantResolver):
    """Resolve the current tenant from an HTTP request header.

    Args:
        store: Tenant metadata store.
        header_name: Header to read (default: ``"X-Tenant-ID"``).

    Example::

        resolver = HeaderTenantResolver(store, header_name="X-Tenant-ID")

        # Request: GET /api/users HTTP/1.1
        #          X-Tenant-ID: acme-corp
        tenant = await resolver.resolve(request)
        # → Tenant(identifier="acme-corp", …)
    """

    def __init__(
        self,
        store: TenantStore[Tenant],
        header_name: str = "X-Tenant-ID",
    ) -> None:
        super().__init__(store)
        self._header_name = header_name

    async def resolve(self, request: Request) -> Tenant:
        """Resolve the tenant from the ``X-Tenant-ID`` header (or configured name).

        All failure modes (missing header, invalid identifier format, unknown
        tenant) raise :exc:`TenantResolutionError` with the same generic reason
        to prevent tenant enumeration by callers.

        Args:
            request: Incoming HTTP request.

        Returns:
            Resolved :class:`~fastapi_tenancy.core.types.Tenant`.

        Raises:
            TenantResolutionError: When the header is absent, fails identifier
                validation, or no matching tenant exists.
        """
        identifier = request.headers.get(self._header_name, "").strip()
        if not identifier:
            logger.debug("Header resolver: header %r missing or empty", self._header_name)
            raise TenantResolutionError(reason=_GENERIC_REASON, strategy="header")
        if not validate_tenant_identifier(identifier):
            logger.debug("Header resolver: invalid identifier format %r", identifier)
            raise TenantResolutionError(reason=_GENERIC_REASON, strategy="header")
        logger.debug("Header resolver: identifier=%r", identifier)
        try:
            return await self.store.get_by_identifier(identifier)
        except TenantNotFoundError:
            # Re-raise as TenantResolutionError with the same generic message
            # so the middleware returns 400 (not 404) for unknown tenants —
            # a 404 would confirm to callers that the identifier format is valid.
            raise TenantResolutionError(reason=_GENERIC_REASON, strategy="header")  # noqa: B904


__all__ = ["HeaderTenantResolver"]
