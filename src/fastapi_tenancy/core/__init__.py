"""Core tenancy abstractions — types, config, context, and exceptions."""

from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.context import (
    TenantContext,
    get_current_tenant,
    get_current_tenant_optional,
)
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
    TenantResolver,
    TenantStatus,
)

__all__ = [
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
    # Types
    "AuditLog",
    "IsolationStrategy",
    "ResolutionStrategy",
    "Tenant",
    "TenantConfig",
    "TenantMetrics",
    "TenantResolver",
    "TenantStatus",
]
