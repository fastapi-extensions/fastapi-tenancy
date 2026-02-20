"""Domain types, enumerations, and data models for fastapi-tenancy.

This module is the single source of truth for the library's public domain
vocabulary.  All other modules import *from* this module — never the reverse —
to keep the dependency graph acyclic.

Design notes
------------
* Enumerations use :class:`~enum.StrEnum` (Python 3.11+) so values serialise
  to plain strings in JSON, logs, and database rows without extra conversion.
* :class:`Tenant` and :class:`TenantConfig` are Pydantic ``frozen=True``
  models.  Immutability eliminates an entire class of accidental mutation bugs
  and makes instances safe to share across async tasks.
* ``BaseTenantResolver`` and ``BaseIsolationProvider`` are imported lazily via
  :func:`__getattr__` to prevent circular imports at module load time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class TenantStatus(StrEnum):
    """Lifecycle status of a tenant.

    Transitions
    -----------
    * ``PROVISIONING`` → ``ACTIVE``:  provisioning completed successfully.
    * ``ACTIVE`` → ``SUSPENDED``:  operator suspends the tenant.
    * ``SUSPENDED`` → ``ACTIVE``:  operator reinstates the tenant.
    * ``ACTIVE`` / ``SUSPENDED`` → ``DELETED``:  soft-delete (if enabled).
    """

    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"
    PROVISIONING = "provisioning"


class IsolationStrategy(StrEnum):
    """Data-isolation strategy applied to tenant requests.

    Strategies
    ----------
    SCHEMA
        Each tenant gets a dedicated PostgreSQL/MSSQL schema.
        ``search_path`` is set per-session so unqualified table references
        resolve to the correct schema automatically.

    DATABASE
        Each tenant owns a separate database instance (or file for SQLite).
        Strongest isolation; highest resource overhead.

    RLS
        All tenants share the same schema and tables.  PostgreSQL Row-Level
        Security policies (keyed on ``app.current_tenant``) enforce isolation
        at the database engine level, with ``WHERE tenant_id = :id`` applied
        as defence-in-depth.

    HYBRID
        Premium tenants use one strategy (e.g. ``SCHEMA``); standard tenants
        use another (e.g. ``RLS``).  Controlled via
        :attr:`~fastapi_tenancy.core.config.TenancyConfig.premium_tenants`.
    """

    SCHEMA = "schema"
    DATABASE = "database"
    RLS = "rls"
    HYBRID = "hybrid"


class ResolutionStrategy(StrEnum):
    """Method used to extract the tenant identifier from an HTTP request.

    Strategies
    ----------
    HEADER
        Read a dedicated HTTP header (default: ``X-Tenant-ID``).
    SUBDOMAIN
        Extract the leftmost subdomain component of the ``Host`` header
        (e.g. ``acme-corp.example.com`` → ``acme-corp``).
    PATH
        Parse a fixed URL path prefix (e.g. ``/tenants/{id}/resource``).
    JWT
        Decode a Bearer JWT token and read a configured claim.
    CUSTOM
        Inject a user-supplied :class:`~fastapi_tenancy.resolution.base.BaseTenantResolver`
        via :class:`~fastapi_tenancy.manager.TenancyManager`.
    """

    HEADER = "header"
    SUBDOMAIN = "subdomain"
    PATH = "path"
    JWT = "jwt"
    CUSTOM = "custom"


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


class Tenant(BaseModel):
    """Immutable tenant domain model.

    All instances are frozen (``ConfigDict(frozen=True)``).  To produce a
    modified copy use :meth:`model_copy`::

        updated = tenant.model_copy(update={"status": TenantStatus.SUSPENDED})

    Attributes:
        id: Opaque unique identifier (UUID or any stable string).
        identifier: Human-readable slug used in URLs, headers, and subdomains
            (e.g. ``"acme-corp"``).
        name: Display name shown in UIs and reports.
        status: Current :class:`TenantStatus`.
        isolation_strategy: Per-tenant override; ``None`` means use the global
            strategy from :class:`~fastapi_tenancy.core.config.TenancyConfig`.
        metadata: Arbitrary key-value store for application-specific
            configuration (plan, quotas, feature flags, …).
        created_at: Creation timestamp in UTC.
        updated_at: Last-modification timestamp in UTC.
        database_url: Connection URL used in ``DATABASE`` isolation mode.
            **Masked** in safe serialisation methods.
        schema_name: Schema name override used in ``SCHEMA`` isolation mode.
    """

    model_config = ConfigDict(
        frozen=True,
        arbitrary_types_allowed=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "tenant-123",
                    "identifier": "acme-corp",
                    "name": "Acme Corporation",
                    "status": "active",
                    "isolation_strategy": "schema",
                    "metadata": {"plan": "enterprise", "max_users": 500},
                }
            ]
        },
    )

    id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Unique opaque tenant identifier.",
    )
    identifier: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Human-readable slug (lowercase letters, digits, hyphens).",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Display name.",
    )
    status: TenantStatus = Field(
        default=TenantStatus.ACTIVE,
        description="Lifecycle status.",
    )
    isolation_strategy: IsolationStrategy | None = Field(
        default=None,
        description="Per-tenant isolation override (None = use global config).",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Application-defined key-value store.",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Creation timestamp (UTC).",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Last-modification timestamp (UTC).",
    )
    database_url: str | None = Field(
        default=None,
        description="Per-tenant database URL (DATABASE isolation only).",
    )
    schema_name: str | None = Field(
        default=None,
        description="Per-tenant schema name override (SCHEMA isolation only).",
    )

    # ------------------------------------------------------------------
    # Identity helpers
    # ------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Tenant):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    # ------------------------------------------------------------------
    # Domain logic
    # ------------------------------------------------------------------

    def is_active(self) -> bool:
        """Return ``True`` if this tenant's status is :attr:`TenantStatus.ACTIVE`."""
        return self.status == TenantStatus.ACTIVE

    def model_dump_safe(self) -> dict[str, Any]:
        """Return a serialisable dict with ``database_url`` masked.

        Use this method when including tenant data in logs, error responses,
        or audit trails to avoid leaking connection-string credentials.

        Returns:
            A plain dictionary with ``database_url`` replaced by
            ``"***masked***"`` when set.
        """
        data = self.model_dump()
        if data.get("database_url"):
            data["database_url"] = "***masked***"
        return data


class TenantConfig(BaseModel):
    """Per-tenant quota and feature configuration.

    Instances are built from the tenant's :attr:`Tenant.metadata` blob by the
    :func:`~fastapi_tenancy.dependencies.get_tenant_config` dependency.  All
    fields have sensible defaults so the dependency never raises even when a
    tenant has an empty metadata dict.

    Attributes:
        max_users: Maximum number of users allowed (``None`` = unlimited).
        max_storage_gb: Maximum storage in gigabytes (``None`` = unlimited).
        features_enabled: List of feature-flag strings enabled for this tenant.
        rate_limit_per_minute: Per-tenant API rate limit.
        custom_settings: Arbitrary extra configuration for application use.
    """

    model_config = ConfigDict(frozen=True)

    max_users: int | None = Field(
        default=None,
        ge=0,
        description="Maximum users (None = unlimited).",
    )
    max_storage_gb: int | None = Field(
        default=None,
        ge=0,
        description="Maximum storage in GB (None = unlimited).",
    )
    features_enabled: list[str] = Field(
        default_factory=list,
        description="Active feature flags.",
    )
    rate_limit_per_minute: int = Field(
        default=100,
        ge=1,
        le=10_000,
        description="API rate limit per minute.",
    )
    custom_settings: dict[str, Any] = Field(
        default_factory=dict,
        description="Application-defined extra settings.",
    )


class AuditLog(BaseModel):
    """Immutable audit-log entry for tenant operations.

    Attributes:
        tenant_id: Tenant whose resource was affected.
        user_id: Authenticated user performing the action (``None`` for
            system-initiated actions).
        action: Verb describing the operation (e.g. ``"create"``, ``"delete"``).
        resource: Resource type (e.g. ``"user"``, ``"order"``).
        resource_id: Identifier of the specific resource (``None`` for
            collection-level actions).
        metadata: Supplementary context (diff, old values, …).
        ip_address: Client IP address.
        user_agent: Client user-agent string.
        timestamp: Event timestamp in UTC.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., description="Owning tenant ID.")
    user_id: str | None = Field(default=None, description="Authenticated user ID.")
    action: str = Field(..., min_length=1, description="Operation verb.")
    resource: str = Field(..., min_length=1, description="Resource type.")
    resource_id: str | None = Field(default=None, description="Resource ID.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Supplementary context.")
    ip_address: str | None = Field(default=None, description="Client IP.")
    user_agent: str | None = Field(default=None, description="Client user-agent.")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Event timestamp (UTC).",
    )


class TenantMetrics(BaseModel):
    """Snapshot of a tenant's usage metrics.

    Attributes:
        tenant_id: Owning tenant ID.
        requests_count: Total number of HTTP requests handled.
        storage_bytes: Storage consumed in bytes.
        users_count: Number of active users.
        api_calls_today: API calls in the current calendar day (UTC).
        last_activity: Timestamp of the most recent request (``None`` if no
            activity has been recorded yet).
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., description="Tenant ID.")
    requests_count: int = Field(default=0, ge=0, description="Total requests.")
    storage_bytes: int = Field(default=0, ge=0, description="Storage in bytes.")
    users_count: int = Field(default=0, ge=0, description="Active users.")
    api_calls_today: int = Field(default=0, ge=0, description="API calls today.")
    last_activity: datetime | None = Field(default=None, description="Last activity timestamp.")


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class TenantResolver(Protocol):
    """Structural type for tenant resolution strategies.

    Any object that implements an ``async def resolve(request) -> Tenant``
    method satisfies this protocol and can be used as a custom resolver.
    """

    async def resolve(self, request: Any) -> Tenant:
        """Resolve the current tenant from *request*.

        Args:
            request: A FastAPI / Starlette ``Request`` instance.

        Returns:
            The resolved :class:`Tenant`.

        Raises:
            TenantResolutionError: When the request does not carry enough
                information to identify a tenant.
            TenantNotFoundError: When the extracted identifier matches no
                known tenant.
        """
        ...


# ---------------------------------------------------------------------------
# Lazy public re-exports to avoid circular imports
# ---------------------------------------------------------------------------


def __getattr__(name: str) -> Any:
    """Lazy-import extension points to break circular dependency chains.

    ``BaseTenantResolver`` lives in ``resolution/base.py`` and
    ``BaseIsolationProvider`` lives in ``isolation/base.py``.  Both modules
    import from ``core/types.py``, so importing them at module level here
    would create a cycle.  Deferring to first attribute access breaks the
    cycle while still making both names accessible as
    ``fastapi_tenancy.core.types.BaseTenantResolver``.
    """
    if name == "BaseTenantResolver":
        from fastapi_tenancy.resolution.base import BaseTenantResolver

        return BaseTenantResolver

    if name == "BaseIsolationProvider":
        from fastapi_tenancy.isolation.base import BaseIsolationProvider

        return BaseIsolationProvider

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AuditLog",
    "IsolationStrategy",
    "ResolutionStrategy",
    "Tenant",
    "TenantConfig",
    "TenantMetrics",
    "TenantResolver",
    "TenantStatus",
]
