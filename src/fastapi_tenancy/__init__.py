"""fastapi-tenancy — enterprise-grade multi-tenancy for FastAPI.

This package provides four data-isolation strategies (schema, database,
row-level-security, hybrid), pluggable tenant resolution (header, subdomain,
path, JWT, custom), async-safe context management, and a clean repository
abstraction — all typed, linted, and ready for production.

Quick start
-----------
.. code-block:: python

    from fastapi import FastAPI
    from fastapi_tenancy import TenancyManager, TenancyConfig
    from fastapi_tenancy.storage.database import SQLAlchemyTenantStore
    from fastapi_tenancy.middleware.tenancy import TenancyMiddleware
    from fastapi_tenancy.dependencies import make_tenant_db_dependency

    config = TenancyConfig(
        database_url="postgresql+asyncpg://user:pass@localhost/myapp",
        resolution_strategy="header",
        isolation_strategy="schema",
    )
    store  = SQLAlchemyTenantStore(config.database_url)
    manager = TenancyManager(config, store)

    app = FastAPI(lifespan=manager.create_lifespan())
    app.add_middleware(TenancyMiddleware, manager=manager)

    get_tenant_db = make_tenant_db_dependency(manager)

Public surface
--------------
The symbols exported below form the **stable public API**.  Anything not
listed here is an implementation detail and may change between minor versions.

Optional extras
---------------
Some symbols require optional extras to be installed:

- ``RedisTenantStore`` — requires ``pip install fastapi-tenancy[redis]``
- ``JWTTenantResolver`` — requires ``pip install fastapi-tenancy[jwt]``
- ``TenantMigrationManager`` — requires ``pip install fastapi-tenancy[migrations]``

Importing this package without an extra installed is safe: the corresponding
class is absent from the namespace but no ``ImportError`` is raised at package
import time.
"""

from fastapi_tenancy.cache.tenant_cache import TenantCache
from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.context import TenantContext, get_current_tenant, tenant_scope
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
from fastapi_tenancy.core.types import (
    AuditLog,
    IsolationStrategy,
    ResolutionStrategy,
    Tenant,
    TenantConfig,
    TenantMetrics,
    TenantStatus,
)
from fastapi_tenancy.isolation.base import BaseIsolationProvider
from fastapi_tenancy.isolation.database import DatabaseIsolationProvider
from fastapi_tenancy.isolation.hybrid import HybridIsolationProvider
from fastapi_tenancy.isolation.rls import RLSIsolationProvider
from fastapi_tenancy.isolation.schema import SchemaIsolationProvider
from fastapi_tenancy.manager import TenancyManager
from fastapi_tenancy.middleware.tenancy import TenancyMiddleware
from fastapi_tenancy.resolution.base import BaseTenantResolver
from fastapi_tenancy.resolution.header import HeaderTenantResolver
from fastapi_tenancy.resolution.path import PathTenantResolver
from fastapi_tenancy.resolution.subdomain import SubdomainTenantResolver
from fastapi_tenancy.storage.database import SQLAlchemyTenantStore
from fastapi_tenancy.storage.memory import InMemoryTenantStore
from fastapi_tenancy.storage.tenant_store import TenantStore

############################################################################
# Optional extras — imported lazily so the package is usable without them. #
############################################################################

try:
    from fastapi_tenancy.resolution.jwt import JWTTenantResolver
    _has_jwt = True
except ImportError:  # pragma: no cover — tested only when PyJWT is absent
    _has_jwt = False  # type: ignore[assignment]

try:
    from fastapi_tenancy.storage.redis import RedisTenantStore
    _has_redis = True
except ImportError:  # pragma: no cover — tested only when redis is absent
    _has_redis = False  # type: ignore[assignment]

try:
    from fastapi_tenancy.migrations.manager import TenantMigrationManager
    _has_migrations = True
except ImportError:  # pragma: no cover — tested only when alembic is absent
    _has_migrations = False  # type: ignore[assignment]

try:
    from importlib.metadata import version as _pkg_version
    __version__: str = _pkg_version("fastapi-tenancy")
except Exception:  # pragma: no cover — package not installed in editable mode without build
    __version__ = "0.0.0.dev0"
__all__ = [  # NOQA
    # Version
    "__version__",
    # Configuration
    "TenancyConfig",
    # Manager
    "TenancyManager",
    # Domain types
    "AuditLog",
    "IsolationStrategy",
    "ResolutionStrategy",
    "Tenant",
    "TenantConfig",
    "TenantMetrics",
    "TenantStatus",
    # Context
    "TenantContext",
    "get_current_tenant",
    "tenant_scope",
    # Exceptions
    "ConfigurationError",
    "DatabaseConnectionError",
    "IsolationError",
    "MigrationError",
    "RateLimitExceededError",
    "TenancyError",
    "TenantDataLeakageError",
    "TenantInactiveError",
    "TenantNotFoundError",
    "TenantQuotaExceededError",
    "TenantResolutionError",
    # Storage
    "SQLAlchemyTenantStore",
    "InMemoryTenantStore",
    "TenantStore",
    # Isolation providers
    "BaseIsolationProvider",
    "DatabaseIsolationProvider",
    "HybridIsolationProvider",
    "RLSIsolationProvider",
    "SchemaIsolationProvider",
    # Middleware
    "TenancyMiddleware",
    # Resolvers
    "BaseTenantResolver",
    "HeaderTenantResolver",
    "PathTenantResolver",
    "SubdomainTenantResolver",
    # Cache
    "TenantCache",
    # Optional extras — conditionally appended below.
    # redis extra:      RedisTenantStore
    # jwt extra:        JWTTenantResolver
    # migrations extra: TenantMigrationManager
]

# Append optional symbols so they appear in __all__ only when the extra is installed.
if _has_redis:
    __all__ += ["RedisTenantStore"]  # type: ignore[operator]
if _has_jwt:
    __all__ += ["JWTTenantResolver"]  # type: ignore[operator]
if _has_migrations:
    __all__ += ["TenantMigrationManager"]  # type: ignore[operator]
