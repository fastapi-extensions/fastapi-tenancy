"""JWT-based tenant resolution strategy.

Extracts the tenant identifier from a claim inside a Bearer JWT token
provided in the ``Authorization`` request header.

Example JWT payload::

    {
        "sub": "user-abc",
        "tenant_id": "acme-corp",
        "roles": ["admin"],
        "exp": 1893456000
    }

Security notes
--------------
* Validation failures always return a generic ``"JWT validation failed"``
  error message.  Internal details (claim names, token contents, algorithm
  specifics) are logged at ``WARNING`` level for operators but are never
  exposed to callers.
* Tokens are verified using the configured ``secret`` and ``algorithm``.
  Never set ``algorithm`` to ``"none"``; the underlying library rejects this,
  but application code must never pass untrusted algorithm values from the
  token header.

Installation
------------
Requires the ``jwt`` extra::

    pip install fastapi-tenancy[jwt]
    # which installs: python-jose[cryptography]
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

try:
    from jose import JWTError, jwt as _jose_jwt

    _JOSE_AVAILABLE = True
except ImportError:  # pragma: no cover
    _JOSE_AVAILABLE = False
    JWTError = Exception  # type: ignore[misc, assignment]
    _jose_jwt = None  # type: ignore[assignment]

from fastapi_tenancy.core.exceptions import TenantResolutionError
from fastapi_tenancy.resolution.base import BaseTenantResolver

if TYPE_CHECKING:
    from fastapi import Request

    from fastapi_tenancy.core.types import Tenant
    from fastapi_tenancy.storage.tenant_store import TenantStore

logger = logging.getLogger(__name__)


class JWTTenantResolver(BaseTenantResolver):
    """Resolve tenant from a JWT Bearer token claim.

    Reads the ``Authorization: Bearer <token>`` header, validates the JWT,
    and extracts the configured tenant claim.

    Args:
        secret: Signing secret (HS256) or public key (RS256 / EC).
        algorithm: JWT signing algorithm.  Defaults to ``"HS256"``.
        tenant_claim: Name of the JWT payload claim containing the tenant
            identifier.  Defaults to ``"tenant_id"``.
        tenant_store: Storage backend for tenant lookup.

    Raises:
        ImportError: When ``python-jose`` is not installed.
        ValueError: When ``secret`` is empty or shorter than 32 characters.

    Example::

        resolver = JWTTenantResolver(
            secret=os.environ["JWT_SECRET"],
            tenant_claim="tenant_id",
            tenant_store=store,
        )
        tenant = await resolver.resolve(request)
    """

    def __init__(
        self,
        secret: str,
        algorithm: str = "HS256",
        tenant_claim: str = "tenant_id",
        tenant_store: TenantStore | None = None,
    ) -> None:
        if not _JOSE_AVAILABLE:
            raise ImportError(
                "JWTTenantResolver requires the 'jwt' extra: "
                "pip install fastapi-tenancy[jwt]"
            )
        if not secret:
            raise ValueError(
                "JWTTenantResolver requires a non-empty JWT secret."
            )
        if len(secret) < 32:
            raise ValueError(
                "JWT secret must be at least 32 characters long for adequate security."
            )

        super().__init__(tenant_store)
        self._secret = secret
        self._algorithm = algorithm
        self._claim = tenant_claim
        logger.debug(
            "JWTTenantResolver algorithm=%r tenant_claim=%r", algorithm, tenant_claim
        )

    async def resolve(self, request: Request) -> Tenant:
        """Validate the Bearer JWT and extract the tenant identifier from its payload.

        Args:
            request: Incoming FastAPI / Starlette request.

        Returns:
            The resolved :class:`~fastapi_tenancy.core.types.Tenant`.

        Raises:
            TenantResolutionError: When the ``Authorization`` header is absent,
                malformed, the token is invalid / expired, or the tenant claim
                is missing.
            TenantNotFoundError: When no tenant matches the extracted identifier.
        """
        # Extract Authorization header
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            raise TenantResolutionError(
                reason="Authorization header is not present in the request.",
                strategy="jwt",
            )

        parts = auth_header.split(maxsplit=1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise TenantResolutionError(
                reason="Authorization header must use the Bearer scheme.",
                strategy="jwt",
            )

        token = parts[1].strip()
        if not token:
            raise TenantResolutionError(
                reason="Bearer token is empty.",
                strategy="jwt",
            )

        # Validate and decode JWT
        try:
            payload: dict = _jose_jwt.decode( # type: ignore
                token,
                self._secret,
                algorithms=[self._algorithm],
            )
        except JWTError as exc:
            # Log the real reason for operators; return generic message to caller.
            logger.warning("JWT validation failed: %s", exc)
            raise TenantResolutionError(
                reason="JWT validation failed.",
                strategy="jwt",
            ) from exc

        # Extract tenant claim
        raw_claim = payload.get(self._claim)
        if not raw_claim:
            # Log claim name for operators; never return claim names to caller
            # to avoid leaking JWT structure to untrusted clients.
            logger.warning(
                "JWT payload missing required claim %r — available claims omitted",
                self._claim,
            )
            raise TenantResolutionError(
                reason="JWT token does not contain the required tenant claim.",
                strategy="jwt",
            )

        identifier = str(raw_claim).strip()
        if not self.validate_tenant_identifier(identifier):
            raise TenantResolutionError(
                reason="Tenant identifier extracted from JWT claim has an invalid format.",
                strategy="jwt",
            )

        logger.debug("Resolving tenant from JWT claim=%r identifier=%r", self._claim, identifier)
        return await self.get_tenant_by_identifier(identifier)


__all__ = ["JWTTenantResolver"]
