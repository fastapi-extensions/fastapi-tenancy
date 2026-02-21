"""Custom exceptions for fastapi-tenancy.

All exceptions derive from ``TenancyError`` so callers can catch the entire
family with a single ``except TenancyError`` clause while still being able to
handle individual sub-types for fine-grained error recovery.

Exception hierarchy::

    TenancyError
    ├── TenantNotFoundError
    ├── TenantResolutionError
    ├── TenantInactiveError
    ├── IsolationError
    ├── ConfigurationError
    ├── MigrationError
    ├── RateLimitExceededError
    ├── TenantDataLeakageError
    ├── TenantQuotaExceededError
    └── DatabaseConnectionError

Design decisions:
    - Every exception carries a structured ``details`` dict that is safe to
      log or include in internal error reports.  It must never contain raw
      secrets, full stack traces, or user PII.
    - Error messages are human-readable and operator-focused.  Client-facing
      messages are constructed by the middleware, not here.
    - All subclasses call ``super().__init__(message, details)`` so the base
      ``TenancyError`` attributes are always populated.
"""

from __future__ import annotations

from typing import Any


class TenancyError(Exception):
    """Base exception for all fastapi-tenancy errors.

    Every public exception in this library subclasses ``TenancyError``,
    enabling callers to distinguish tenancy-specific failures from other
    application exceptions with a single ``except`` clause.

    Attributes:
        message: Human-readable description of the error.
        details: Supplementary key-value context.  Safe to log; must never
            contain raw secrets, full stack traces, or user PII.
    """

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message
        self.details: dict[str, Any] = details or {}
        super().__init__(message)

    def __str__(self) -> str:
        """Return ``human-readable`` string."""
        if self.details:
            return f"{self.message} | details={self.details}"
        return self.message

    def __repr__(self) -> str:
        """Return ``repr`` string for debugging purpose."""
        return f"{type(self).__name__}(message={self.message!r})"


class TenantNotFoundError(TenancyError):
    """Raised when a tenant cannot be located in the configured store.

    Typical causes:
        - The identifier extracted from the request matches no known tenant.
        - A tenant was deleted between resolution and usage.
        - The store is empty (e.g. first run without seed data).

    Attributes:
        identifier: The identifier that was looked up (``None`` when the
            context does not provide a useful identifier).
    """

    def __init__(
        self,
        identifier: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        message = f"Tenant not found: {identifier!r}" if identifier else "Tenant not found"
        super().__init__(message, details)
        self.identifier = identifier


class TenantResolutionError(TenancyError):
    """Raised when the configured strategy cannot extract a tenant from a request.

    This is distinct from ``TenantNotFoundError``:

    - Resolution failure → the request itself is malformed (missing header,
      bad JWT format, wrong path prefix, etc.)
    - Not-found → the identifier was valid but the tenant does not exist.

    Attributes:
        reason: A concise, operator-readable explanation of why resolution
            failed.  **Never** include raw user input in this field.
        strategy: The resolution strategy that was active (e.g. ``"header"``).
    """

    def __init__(
        self,
        reason: str,
        strategy: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        message = f"Tenant resolution failed: {reason}"
        if strategy:
            message += f" (strategy: {strategy})"
        super().__init__(message, details)
        self.reason = reason
        self.strategy = strategy


class TenantInactiveError(TenancyError):
    """Raised when a resolved tenant is not in the ``ACTIVE`` status.

    Prevents suspended, deleted, or provisioning tenants from accessing any
    resource behind the tenancy middleware.

    Attributes:
        tenant_id: Internal ID of the inactive tenant.
        status: The tenant's current lifecycle status value.
    """

    def __init__(
        self,
        tenant_id: str,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"Tenant {tenant_id!r} is {status}", details)
        self.tenant_id = tenant_id
        self.status = status


class IsolationError(TenancyError):
    """Raised when a data-isolation operation fails.

    Isolation errors are serious operational failures — they indicate that
    the library cannot guarantee tenant data separation.  Operators should
    treat these as high-severity alerts requiring immediate investigation.

    Attributes:
        operation: The isolation operation that failed (e.g. ``"get_session"``,
            ``"initialize_tenant"``).
        tenant_id: The affected tenant's ID (``None`` when not applicable).
    """

    def __init__(
        self,
        operation: str,
        tenant_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        message = f"Isolation operation failed: {operation}"
        if tenant_id:
            message += f" (tenant: {tenant_id!r})"
        super().__init__(message, details)
        self.operation = operation
        self.tenant_id = tenant_id


class ConfigurationError(TenancyError):
    """Raised when ``TenancyConfig`` contains an invalid or inconsistent value.

    Raised at construction time so misconfigured applications fail fast during
    startup rather than silently producing wrong behaviour at the first request.

    Attributes:
        parameter: The name of the invalid configuration field.
        reason: Why the current value is invalid.
    """

    def __init__(
        self,
        parameter: str,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"Invalid configuration for {parameter!r}: {reason}", details)
        self.parameter = parameter
        self.reason = reason


class MigrationError(TenancyError):
    """Raised when an Alembic migration operation fails for a tenant.

    Attributes:
        tenant_id: The affected tenant's ID.
        operation: The migration operation (e.g. ``"upgrade"``, ``"downgrade"``).
        reason: The underlying error description.
    """

    def __init__(
        self,
        tenant_id: str,
        operation: str,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            f"Migration failed for tenant {tenant_id!r} during {operation!r}: {reason}",
            details,
        )
        self.tenant_id = tenant_id
        self.operation = operation
        self.reason = reason


class RateLimitExceededError(TenancyError):
    """Raised when a tenant exceeds its configured request rate limit.

    Attributes:
        tenant_id: The rate-limited tenant's ID.
        limit: The threshold that was exceeded (requests per window).
        window_seconds: Duration of the rate-limit window in seconds.
    """

    def __init__(
        self,
        tenant_id: str,
        limit: int,
        window_seconds: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            f"Rate limit exceeded for tenant {tenant_id!r}: "
            f"{limit} requests per {window_seconds}s",
            details,
        )
        self.tenant_id = tenant_id
        self.limit = limit
        self.window_seconds = window_seconds


class TenantDataLeakageError(TenancyError):
    """Raised when a potential cross-tenant data leakage is detected.

    This is a **critical security exception**.  Any occurrence should trigger
    an immediate alert, halt the request, and trigger incident-response.

    Attributes:
        operation: The operation during which leakage was detected.
        expected_tenant: The tenant that *should* own the data.
        actual_tenant: The tenant whose data was encountered instead.
    """

    def __init__(
        self,
        operation: str,
        expected_tenant: str,
        actual_tenant: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            f"SECURITY: potential data leakage in {operation!r} — "
            f"expected tenant {expected_tenant!r}, got {actual_tenant!r}",
            details,
        )
        self.operation = operation
        self.expected_tenant = expected_tenant
        self.actual_tenant = actual_tenant


class TenantQuotaExceededError(TenancyError):
    """Raised when a tenant exceeds a resource quota.

    Attributes:
        tenant_id: The affected tenant's ID.
        quota_type: The type of quota exceeded (e.g. ``"users"``, ``"storage_gb"``).
        current: Current usage level.
        limit: The configured quota limit.
    """

    def __init__(
        self,
        tenant_id: str,
        quota_type: str,
        current: int | float,
        limit: int | float,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            f"Quota exceeded for tenant {tenant_id!r}: "
            f"{quota_type} usage {current} exceeds limit {limit}",
            details,
        )
        self.tenant_id = tenant_id
        self.quota_type = quota_type
        self.current = current
        self.limit = limit


class DatabaseConnectionError(TenancyError):
    """Raised when the library cannot establish a database connection for a tenant.

    Most common in ``DATABASE`` isolation mode where each tenant has a dedicated
    database that may be temporarily unavailable.

    Attributes:
        tenant_id: The affected tenant's ID.
        reason: A concise description of the connection failure.
    """

    def __init__(
        self,
        tenant_id: str,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            f"Database connection failed for tenant {tenant_id!r}: {reason}",
            details,
        )
        self.tenant_id = tenant_id
        self.reason = reason


__all__ = [
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
]
