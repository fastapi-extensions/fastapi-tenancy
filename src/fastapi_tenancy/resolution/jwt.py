"""JWT-based tenant resolution strategy.

Decodes a Bearer JSON Web Token from the ``Authorization`` header and reads a
configured claim to identify the tenant.

Dependencies
------------
Requires the ``PyJWT`` package — install via the ``[jwt]`` extra::

    pip install fastapi-tenancy[jwt]

Supported algorithms
--------------------
All algorithms supported by ``PyJWT`` are available.  The default is
``HS256``.  For RS256 (asymmetric), pass ``secret`` as the PEM-encoded
public key.

Security
--------
- The token signature is always verified; do not disable this.
- Token expiry (``exp`` claim) is verified automatically by PyJWT.
- The extracted tenant identifier is validated against slug rules before
  any database lookup.
- The ``secret`` parameter is **never** included in error messages or logs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi_tenancy.core.exceptions import TenantResolutionError
from fastapi_tenancy.resolution.base import BaseTenantResolver
from fastapi_tenancy.utils.validation import validate_tenant_identifier

if TYPE_CHECKING:
    from starlette.requests import Request

    from fastapi_tenancy.core.types import Tenant
    from fastapi_tenancy.storage.tenant_store import TenantStore

logger = logging.getLogger(__name__)

_BEARER_PREFIX = "Bearer "


class JWTTenantResolver(BaseTenantResolver):
    """Resolve the current tenant from a signed Bearer JWT.

    Reads ``Authorization: Bearer <token>`` from the request, verifies the
    signature, and extracts the configured claim (default: ``tenant_id``).

    Args:
        store: Tenant metadata store.
        secret: JWT signing secret (HMAC) or public key (RSA).  Required.
        algorithm: Signing algorithm (default: ``"HS256"``).
        tenant_claim: JWT payload claim holding the tenant identifier
            (default: ``"tenant_id"``).

    Raises:
        ImportError: When ``PyJWT`` is not installed.

    Example::

        resolver = JWTTenantResolver(
            store,
            secret="my-super-secret-key-at-least-32-chars",
            tenant_claim="tenant_id",
        )

        # Request: Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6Ikp...
        tenant = await resolver.resolve(request)
    """

    def __init__(
        self,
        store: TenantStore[Tenant],
        secret: str,
        algorithm: str = "HS256",
        tenant_claim: str = "tenant_id",
    ) -> None:
        super().__init__(store)
        try:
            import jwt as _pyjwt  # noqa: PLC0415

            self._jwt = _pyjwt
        except ImportError as exc:
            raise ImportError(
                "JWT resolution requires 'PyJWT>=2.8'. "
                "Install it with: pip install 'fastapi-tenancy[jwt]'"
            ) from exc

        self._secret = secret
        self._algorithm = algorithm
        self._tenant_claim = tenant_claim

    def _decode_token(self, token: str) -> dict[str, Any]:
        """Verify and decode a JWT string.

        Args:
            token: Raw JWT string (without the ``Bearer `` prefix).

        Returns:
            Decoded payload dictionary.

        Raises:
            TenantResolutionError: On any JWT verification failure.
        """
        try:
            return self._jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
            )
        except self._jwt.ExpiredSignatureError:
            raise TenantResolutionError(
                reason="JWT token has expired",
                strategy="jwt",
            ) from None
        except self._jwt.InvalidTokenError as exc:
            raise TenantResolutionError(
                reason="JWT token is invalid or signature verification failed",
                strategy="jwt",
                details={"jwt_error": type(exc).__name__},
            ) from exc

    async def resolve(self, request: Request) -> Tenant:
        """Decode the Bearer JWT and resolve the tenant from the payload claim.

        Args:
            request: Incoming HTTP request.

        Returns:
            Resolved :class:`~fastapi_tenancy.core.types.Tenant`.

        Raises:
            TenantResolutionError: When the ``Authorization`` header is absent,
                malformed, or the JWT is invalid / expired.
            TenantNotFoundError: When the extracted identifier has no
                matching tenant.
        """
        auth_header = request.headers.get("authorization", "")
        if not auth_header:
            raise TenantResolutionError(
                reason="Authorization header is missing",
                strategy="jwt",
            )
        if not auth_header.startswith(_BEARER_PREFIX):
            raise TenantResolutionError(
                reason="Authorization header does not use Bearer scheme",
                strategy="jwt",
            )

        token = auth_header[len(_BEARER_PREFIX):].strip()
        if not token:
            raise TenantResolutionError(
                reason="Bearer token is empty",
                strategy="jwt",
            )

        payload = self._decode_token(token)

        identifier = payload.get(self._tenant_claim)
        if not identifier or not isinstance(identifier, str):
            raise TenantResolutionError(
                reason=f"JWT payload is missing claim {self._tenant_claim!r}",
                strategy="jwt",
                details={"claim": self._tenant_claim},
            )
        if not validate_tenant_identifier(identifier):
            raise TenantResolutionError(
                reason=f"JWT claim {self._tenant_claim!r} contains an invalid tenant identifier",
                strategy="jwt",
                details={"claim": self._tenant_claim},
            )

        logger.debug("JWT resolver: claim=%r → identifier=%r", self._tenant_claim, identifier)
        return await self.store.get_by_identifier(identifier)


__all__ = ["JWTTenantResolver"]
