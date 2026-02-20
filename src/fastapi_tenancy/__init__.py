"""fastapi-tenancy — Enterprise-grade multi-tenancy for FastAPI.

Quick start
-----------
.. code-block:: python

    from fastapi import FastAPI
    from fastapi_tenancy import TenancyManager, TenancyConfig

    config = TenancyConfig(
        database_url="postgresql+asyncpg://user:pass@localhost/myapp",
        resolution_strategy="header",
        isolation_strategy="schema",
    )

    app = FastAPI(lifespan=TenancyManager.create_lifespan(config))

Public API
----------
Configuration
    :class:`TenancyConfig`

Core types
    :class:`Tenant`, :class:`TenantStatus`, :class:`IsolationStrategy`,
    :class:`ResolutionStrategy`, :class:`TenantConfig`, :class:`AuditLog`,
    :class:`TenantMetrics`

Manager & Middleware
    :class:`TenancyManager`, :class:`TenancyMiddleware`

Context helpers
    :class:`TenantContext`, :func:`get_current_tenant`,
    :func:`get_current_tenant_optional`

FastAPI dependencies
    :func:`get_tenant_db`, :func:`require_active_tenant`, :func:`get_tenant_config`

Storage backends
    :class:`TenantStore`, :class:`InMemoryTenantStore`,
    :class:`SQLAlchemyTenantStore`

Isolation providers
    :class:`BaseIsolationProvider`

Resolution strategies
    :class:`BaseTenantResolver`

Optional — requires ``[redis]`` extra
    :class:`TenantCache`, :class:`RedisTenantStore`

Optional — requires ``[migrations]`` extra
    :class:`MigrationManager`

Exceptions
    :class:`TenancyError` and all its subclasses
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__: str = _pkg_version("fastapi-tenancy")
except PackageNotFoundError:
    __version__ = "0.0.0+dev"

__author__ = "fastapi-tenancy contributors"
__license__ = "MIT"

# Configuration
from fastapi_tenancy.core.config import TenancyConfig

# Context
from fastapi_tenancy.core.context import (
    TenantContext,
    get_current_tenant,
    get_current_tenant_optional,
)

# Exceptions
from fastapi_tenancy.core.exceptions import (
    ConfigurationError,
    DatabaseConnectionError,
    IsolationError,
    MigrationError,
    RateLimitExceededError,
    TenancyError,
    TenantDataLeakageError,
    TenantInactiveError,
    TenantNotFoundError,
    TenantQuotaExceededError,
    TenantResolutionError,
)

# Core types
from fastapi_tenancy.core.types import (
    AuditLog,
    IsolationStrategy,
    ResolutionStrategy,
    Tenant,
    TenantConfig,
    TenantMetrics,
    TenantResolver,
    TenantStatus,
)

# FastAPI dependencies
from fastapi_tenancy.dependencies import (
    get_tenant_config,
    get_tenant_db,
    require_active_tenant,
)

# Manager & Middleware
from fastapi_tenancy.manager import TenancyManager
from fastapi_tenancy.middleware.tenancy import TenancyMiddleware

# Storage
from fastapi_tenancy.storage.database import SQLAlchemyTenantStore
from fastapi_tenancy.storage.memory import InMemoryTenantStore
from fastapi_tenancy.storage.tenant_store import TenantStore

# Extension-point base classes
from fastapi_tenancy.isolation.base import BaseIsolationProvider
from fastapi_tenancy.resolution.base import BaseTenantResolver

# Optional — requires: pip install fastapi-tenancy[redis]
try:
    from fastapi_tenancy.storage.redis import RedisTenantStore
    from fastapi_tenancy.cache.tenant_cache import TenantCache
except ImportError:
    RedisTenantStore = None  # type: ignore[assignment, misc]
    TenantCache = None  # type: ignore[assignment, misc]

# Optional — requires: pip install fastapi-tenancy[migrations]
try:
    from fastapi_tenancy.migrations.manager import MigrationManager
except ImportError:
    MigrationManager = None  # type: ignore[assignment, misc]


__all__ = [
    "__version__",
    # Types
    "Tenant",
    "TenantStatus",
    "TenantConfig",
    "TenantMetrics",
    "TenantResolver",
    "AuditLog",
    "IsolationStrategy",
    "ResolutionStrategy",
    # Config
    "TenancyConfig",
    # Context
    "TenantContext",
    "get_current_tenant",
    "get_current_tenant_optional",
    # Exceptions
    "TenancyError",
    "TenantNotFoundError",
    "TenantResolutionError",
    "TenantInactiveError",
    "IsolationError",
    "ConfigurationError",
    "MigrationError",
    "RateLimitExceededError",
    "TenantDataLeakageError",
    "TenantQuotaExceededError",
    "DatabaseConnectionError",
    # Manager
    "TenancyManager",
    # Middleware
    "TenancyMiddleware",
    # Dependencies
    "get_tenant_db",
    "require_active_tenant",
    "get_tenant_config",
    # Storage
    "TenantStore",
    "InMemoryTenantStore",
    "SQLAlchemyTenantStore",
    "RedisTenantStore",
    # Extension points
    "BaseIsolationProvider",
    "BaseTenantResolver",
    # Optional
    "TenantCache",
    "MigrationManager",
]
