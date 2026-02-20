"""Factory for creating tenant resolver instances from configuration.

:class:`ResolverFactory` is a pure factory — it has no instance state and
all logic lives in the static :meth:`ResolverFactory.create` method.

Custom resolver hook
--------------------
The :attr:`~fastapi_tenancy.core.types.ResolutionStrategy.CUSTOM` strategy is
not handled by this factory; it is handled by
:class:`~fastapi_tenancy.manager.TenancyManager` which accepts an explicit
``resolver`` argument that bypasses the factory entirely::

    manager = TenancyManager(
        config=config,
        tenant_store=store,
        resolver=MyCookieTenantResolver(cookie_name="session", tenant_store=store),
    )
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.exceptions import ConfigurationError
from fastapi_tenancy.core.types import ResolutionStrategy
from fastapi_tenancy.resolution.base import BaseTenantResolver
from fastapi_tenancy.storage.tenant_store import TenantStore

if TYPE_CHECKING:
    pass


class ResolverFactory:
    """Static factory that constructs a :class:`BaseTenantResolver` from config.

    Resolver classes are imported lazily inside :meth:`create` to avoid
    unnecessary top-level imports for strategies that are not in use.
    """

    @staticmethod
    def create(
        strategy: ResolutionStrategy,
        config: TenancyConfig,
        tenant_store: TenantStore,
    ) -> BaseTenantResolver:
        """Build a resolver instance for *strategy* using *config* and *tenant_store*.

        Args:
            strategy: The desired :class:`~fastapi_tenancy.core.types.ResolutionStrategy`.
            config: Application-wide tenancy configuration.
            tenant_store: Storage backend passed to the resolver.

        Returns:
            A fully-initialised :class:`BaseTenantResolver`.

        Raises:
            ConfigurationError: When *strategy* is
                :attr:`~fastapi_tenancy.core.types.ResolutionStrategy.CUSTOM`
                (must be injected via :class:`~fastapi_tenancy.manager.TenancyManager`)
                or is an unrecognised value.
        """
        if strategy == ResolutionStrategy.CUSTOM:
            raise ConfigurationError(
                parameter="resolution_strategy",
                reason=(
                    "CUSTOM strategy requires injecting a resolver directly into "
                    "TenancyManager(resolver=...) rather than using ResolverFactory."
                ),
            )

        if strategy == ResolutionStrategy.HEADER:
            from fastapi_tenancy.resolution.header import HeaderTenantResolver

            return HeaderTenantResolver(
                header_name=config.tenant_header_name,
                tenant_store=tenant_store,
            )

        if strategy == ResolutionStrategy.SUBDOMAIN:
            from fastapi_tenancy.resolution.subdomain import SubdomainTenantResolver

            if not config.domain_suffix:
                raise ConfigurationError(
                    parameter="domain_suffix",
                    reason="domain_suffix is required for SUBDOMAIN resolution.",
                )
            return SubdomainTenantResolver(
                domain_suffix=config.domain_suffix,
                tenant_store=tenant_store,
            )

        if strategy == ResolutionStrategy.PATH:
            from fastapi_tenancy.resolution.path import PathTenantResolver

            return PathTenantResolver(
                path_prefix=config.path_prefix,
                tenant_store=tenant_store,
            )

        if strategy == ResolutionStrategy.JWT:
            from fastapi_tenancy.resolution.jwt import JWTTenantResolver

            if not config.jwt_secret:
                raise ConfigurationError(
                    parameter="jwt_secret",
                    reason="jwt_secret is required for JWT resolution.",
                )
            return JWTTenantResolver(
                secret=config.jwt_secret,
                algorithm=config.jwt_algorithm,
                tenant_claim=config.jwt_tenant_claim,
                tenant_store=tenant_store,
            )

        raise ConfigurationError(
            parameter="resolution_strategy",
            reason=f"Unrecognised resolution strategy: {strategy!r}.",
        )


__all__ = ["ResolverFactory"]
