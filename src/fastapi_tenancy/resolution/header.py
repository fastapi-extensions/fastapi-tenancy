"""Header-based tenant resolution strategy.

Extracts the tenant identifier from a named HTTP request header.

This is the simplest and most widely applicable resolution strategy:

* Works with every HTTP client without URL routing changes.
* Trivial to test — just set a header in your test client.
* Explicit: clients always know which tenant they are acting as.
* Suitable for API clients, SDKs, mobile apps, and service-to-service calls.

Example request::

    GET /api/users HTTP/1.1
    Host: api.example.com
    X-Tenant-ID: acme-corp
    Authorization: Bearer <token>

Security note
-------------
Error responses from this resolver deliberately omit the full list of
headers present in the request.  Leaking header names can expose
internal infrastructure headers (``X-Forwarded-For``, auth tokens, etc.)
to untrusted clients.  Only the *expected* header name is disclosed.
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


class HeaderTenantResolver(BaseTenantResolver):
    """Resolve tenant from a named HTTP request header.

    Header name matching is case-insensitive by default, consistent with
    RFC 7230 §3.2 which specifies that HTTP/1.1 field names are
    case-insensitive.

    Args:
        header_name: Name of the header to read.  Defaults to ``"X-Tenant-ID"``.
        tenant_store: Storage backend for tenant lookup.
        case_sensitive: When ``True``, the header name must match exactly.
            Default ``False`` honours HTTP case-insensitivity.

    Example::

        resolver = HeaderTenantResolver(
            header_name="X-Tenant-ID",
            tenant_store=store,
        )
        tenant = await resolver.resolve(request)
    """

    def __init__(
        self,
        header_name: str = "X-Tenant-ID",
        tenant_store: TenantStore | None = None,
        case_sensitive: bool = False,
    ) -> None:
        super().__init__(tenant_store)
        self._header_name = header_name
        self._case_sensitive = case_sensitive
        logger.debug(
            "HeaderTenantResolver header=%r case_sensitive=%s",
            header_name,
            case_sensitive,
        )

    async def resolve(self, request: Request) -> Tenant:
        """Extract the tenant identifier from the configured header and look it up.

        Args:
            request: Incoming FastAPI / Starlette request.

        Returns:
            The resolved :class:`~fastapi_tenancy.core.types.Tenant`.

        Raises:
            TenantResolutionError: When the header is absent, empty, or the
                identifier has an invalid format.
            TenantNotFoundError: When no tenant matches the extracted identifier.
        """
        raw = self._extract_header(request)

        if raw is None:
            raise TenantResolutionError(
                reason="Required tenant header is not present in the request.",
                strategy="header",
                details={"expected_header": self._header_name},
            )

        identifier = raw.strip()
        if not identifier:
            raise TenantResolutionError(
                reason="Tenant header is present but contains an empty value.",
                strategy="header",
                details={"header_name": self._header_name},
            )

        if not self.validate_tenant_identifier(identifier):
            raise TenantResolutionError(
                reason="Tenant header value is not a valid tenant identifier.",
                strategy="header",
                details={
                    "hint": (
                        "Must be 3-63 characters, start with a lowercase letter, "
                        "and contain only lowercase letters, digits, and hyphens."
                    )
                },
            )

        logger.debug(
            "Resolving tenant from header %r identifier=%r",
            self._header_name,
            identifier,
        )
        return await self.get_tenant_by_identifier(identifier)

    def _extract_header(self, request: Request) -> str | None:
        """Return the raw header value, or ``None`` if absent."""
        if self._case_sensitive:
            return request.headers.get(self._header_name)
        target = self._header_name.lower()
        for name, value in request.headers.items():
            if name.lower() == target:
                return value
        return None


__all__ = ["HeaderTenantResolver"]
