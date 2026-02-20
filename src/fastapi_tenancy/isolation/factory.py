"""Factory for creating isolation provider instances from configuration."""

from __future__ import annotations

from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.exceptions import ConfigurationError
from fastapi_tenancy.core.types import IsolationStrategy
from fastapi_tenancy.isolation.base import BaseIsolationProvider


class IsolationProviderFactory:
    """Static factory that constructs an :class:`BaseIsolationProvider` from config.

    Provider classes are imported lazily inside :meth:`create` to avoid
    unnecessary top-level imports for strategies that are not in use.
    """

    @staticmethod
    def create(
        strategy: IsolationStrategy,
        config: TenancyConfig,
    ) -> BaseIsolationProvider:
        """Build an isolation provider for *strategy*.

        Args:
            strategy: The desired :class:`~fastapi_tenancy.core.types.IsolationStrategy`.
            config: Application-wide tenancy configuration.

        Returns:
            A fully-initialised :class:`BaseIsolationProvider`.

        Raises:
            ConfigurationError: When *strategy* is unrecognised.
        """
        if strategy == IsolationStrategy.SCHEMA:
            from fastapi_tenancy.isolation.schema import SchemaIsolationProvider
            return SchemaIsolationProvider(config)

        if strategy == IsolationStrategy.DATABASE:
            from fastapi_tenancy.isolation.database import DatabaseIsolationProvider
            return DatabaseIsolationProvider(config)

        if strategy == IsolationStrategy.RLS:
            from fastapi_tenancy.isolation.rls import RLSIsolationProvider
            return RLSIsolationProvider(config)

        if strategy == IsolationStrategy.HYBRID:
            from fastapi_tenancy.isolation.hybrid import HybridIsolationProvider
            return HybridIsolationProvider(config)

        raise ConfigurationError(
            parameter="isolation_strategy",
            reason=f"Unrecognised isolation strategy: {strategy!r}.",
        )


__all__ = ["IsolationProviderFactory"]
