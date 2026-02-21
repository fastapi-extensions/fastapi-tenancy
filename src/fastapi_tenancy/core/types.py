"""Domain types, enumerations, generics, and structural protocols.

This module is the **single source of truth** for the library's public domain
vocabulary.  Every other module imports *from* this module — never the reverse —
keeping the dependency graph acyclic and import order deterministic.

Design decisions
----------------
``StrEnum``
    Python 3.11+ ``StrEnum`` values serialise to plain strings in JSON, logs,
    and database rows without extra conversion or custom encoders.

``Tenant`` / ``TenantConfig``
    Pydantic ``frozen=True`` models.  Immutability eliminates an entire class
    of accidental mutation bugs and makes instances safe to share across async
    tasks without copying or locking.

``TenantResolver`` / ``IsolationProvider``
    ``@runtime_checkable`` structural protocols (PEP 544).  Any object whose
    class provides the required methods satisfies the protocol, enabling full
    duck-typing without mandatory inheritance from library base classes.
    Concrete base classes (``BaseTenantResolver``, ``BaseIsolationProvider``)
    are still provided for convenience, but they are *not required*.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.sql import Select
    from starlette.requests import Request

##################
# Type variables #
##################

#: Covariant ``Tenant`` type-variable used in generic stores and resolvers.
TenantT = TypeVar("TenantT", bound="Tenant")

#: Bound to ``Select`` so ``apply_filters`` preserves the concrete query type.
SelectT = TypeVar("SelectT", bound="Select[Any]")

################
# Enumerations #
################


class TenantStatus(StrEnum):
    """Lifecycle status of a tenant.

    State transitions::

        PROVISIONING → ACTIVE → SUSPENDED → ACTIVE  (reinstated)
        ACTIVE       → DELETED                      (soft-delete)
        SUSPENDED    → DELETED                      (soft-delete)
    """

    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"
    PROVISIONING = "provisioning"


class IsolationStrategy(StrEnum):
    """Data-isolation strategy applied to tenant requests.

    Strategies:
        - ``SCHEMA``: Each tenant owns a dedicated PostgreSQL/MSSQL schema.
            ``search_path`` is set per-connection so unqualified table
            references resolve to the correct schema automatically.
        - ``DATABASE``: Each tenant owns a separate database (or SQLite file).
            Strongest isolation; highest resource overhead.
        - ``RLS``: All tenants share the same schema and tables.  PostgreSQL
            Row-Level Security policies enforce isolation at the engine
            level, with ``WHERE tenant_id = :id`` as defence-in-depth.
        - ``HYBRID``: Premium tenants use one strategy; standard tenants use
            another.  Controlled via ``TenancyConfig.premium_tenants``.
    """

    SCHEMA = "schema"
    DATABASE = "database"
    RLS = "rls"
    HYBRID = "hybrid"


class ResolutionStrategy(StrEnum):
    """Method used to extract the tenant identifier from an HTTP request.

    Strategies:
        - ``HEADER``: Read a dedicated HTTP header (default: ``X-Tenant-ID``).
        - ``SUBDOMAIN``: Extract the leftmost subdomain from the ``Host`` header.
        - ``PATH``: Parse a fixed URL path prefix.
        - ``JWT``: Decode a Bearer JWT and read a configured claim.
        - ``CUSTOM``: Inject a user-supplied ``TenantResolver`` via ``TenancyManager``.
    """

    HEADER = "header"
    SUBDOMAIN = "subdomain"
    PATH = "path"
    JWT = "jwt"
    CUSTOM = "custom"


#################
# Domain models #
#################


class Tenant(BaseModel):
    """Immutable tenant domain object.

    All instances are frozen (``ConfigDict(frozen=True)``).  To produce a
    modified copy use Pydantic's ``model_copy``::

        updated = tenant.model_copy(update={"status": TenantStatus.SUSPENDED})

    Attributes:
        id: Opaque unique identifier (UUID or any stable string).  This is
            the internal primary key and should be opaque to end-users.
        identifier: Human-readable slug used in URLs, headers, and subdomains
            (e.g. ``"acme-corp"``).  Must satisfy the tenant slug rules.
        name: Display name shown in UIs and reports.
        status: Current ``TenantStatus``.
        isolation_strategy: Per-tenant override; ``None`` means use the
            global strategy from ``TenancyConfig``.
        metadata: Arbitrary key-value store for application-specific
            configuration (plan, quotas, feature flags, …).
        created_at: Creation timestamp in UTC.
        updated_at: Last-modification timestamp in UTC.
        database_url: Connection URL used in ``DATABASE`` isolation mode.
            **Always masked** in safe serialisation methods.
        schema_name: Schema name override used in ``SCHEMA`` isolation mode.
    """

    model_config = ConfigDict(
        frozen=True,
        arbitrary_types_allowed=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "tenant-aB3xYz9mQp",
                    "identifier": "acme-corp",
                    "name": "Acme Corporation",
                    "status": "active",
                    "isolation_strategy": None,
                    "metadata": {"plan": "enterprise", "max_users": 500},
                }
            ]
        },
    )

    id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Opaque unique tenant identifier (internal primary key).",
    )
    identifier: str = Field(
        ...,
        min_length=3,
        max_length=63,
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
        description="Per-tenant database URL (DATABASE isolation only). Always masked in logs.",
    )
    schema_name: str | None = Field(
        default=None,
        description="Per-tenant schema name override (SCHEMA isolation only).",
    )

    ####################
    # Identity helpers #
    ####################

    def __eq__(self, other: object) -> bool:
        """Equality is based solely on ``id``."""
        if not isinstance(other, Tenant):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        """Hash based on ``id`` to allow use in sets and dict keys."""
        return hash(self.id)

    def __repr__(self) -> str:
        """Return ``__repr__`` string for debugging purpose."""
        return f"Tenant(id={self.id!r}, identifier={self.identifier!r}, status={self.status!r})"

    #####################
    # Domain predicates #
    #####################

    def is_active(self) -> bool:
        """Return ``True`` when status is ``ACTIVE``."""
        return self.status == TenantStatus.ACTIVE

    def is_suspended(self) -> bool:
        """Return ``True`` when status is ``SUSPENDED``."""
        return self.status == TenantStatus.SUSPENDED

    def is_deleted(self) -> bool:
        """Return ``True`` when status is ``DELETED``."""
        return self.status == TenantStatus.DELETED

    def is_provisioning(self) -> bool:
        """Return ``True`` when status is ``PROVISIONING``."""
        return self.status == TenantStatus.PROVISIONING

    ######################
    # Safe serialisation #
    ######################

    def model_dump_safe(self) -> dict[str, Any]:
        """Return a serialisable dict with ``database_url`` masked.

        Use this method when including tenant data in logs, error responses,
        or audit trails to avoid leaking connection-string credentials.

        Returns:
            Plain dictionary with ``database_url`` replaced by ``"***"``
            when the field is set.
        """
        data = self.model_dump()
        if data.get("database_url"):
            data["database_url"] = "***masked***"
        return data


class TenantConfig(BaseModel):
    """Per-tenant quota and feature configuration.

    Built from ``Tenant.metadata`` by the ``get_tenant_config`` dependency.
    All fields have sensible defaults so the dependency never raises even
    when a tenant has an empty metadata dict.

    Attributes:
        max_users: Maximum number of users allowed (``None`` = unlimited).
        max_storage_gb: Maximum storage in gigabytes (``None`` = unlimited).
        features_enabled: List of feature-flag strings enabled for this tenant.
        rate_limit_per_minute: Per-tenant API rate limit.
        custom_settings: Arbitrary extra configuration for application use.
    """

    model_config = ConfigDict(frozen=True)

    max_users: int | None = Field(default=None, ge=0, description="Max users (None = unlimited).")
    max_storage_gb: int | None = Field(
        default=None, ge=0, description="Max storage in GB (None = unlimited)."
    )
    features_enabled: list[str] = Field(
        default_factory=list, description="Active feature flags."
    )
    rate_limit_per_minute: int = Field(
        default=100, ge=1, le=10_000, description="API rate limit per minute."
    )
    custom_settings: dict[str, Any] = Field(
        default_factory=dict, description="Application-defined extra settings."
    )


class AuditLog(BaseModel):
    """Immutable audit-log entry for a tenant operation.

    Attributes:
        tenant_id: Tenant whose resource was affected.
        user_id: Authenticated user (``None`` for system-initiated actions).
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
        last_activity: Timestamp of the most recent request (``None`` if none).
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., description="Tenant ID.")
    requests_count: int = Field(default=0, ge=0, description="Total requests.")
    storage_bytes: int = Field(default=0, ge=0, description="Storage in bytes.")
    users_count: int = Field(default=0, ge=0, description="Active users.")
    api_calls_today: int = Field(default=0, ge=0, description="API calls today.")
    last_activity: datetime | None = Field(default=None, description="Last activity timestamp.")


################################################
# Structural protocols (duck-typing / PEP 544) #
################################################


@runtime_checkable
class TenantResolver(Protocol):
    """Structural protocol for tenant resolution strategies.

    Any object that exposes an ``async def resolve(request) -> Tenant``
    method satisfies this protocol and can be used as a custom resolver
    without inheriting from any library class.

    Example — custom cookie resolver::

        class CookieTenantResolver:
            def __init__(self, store: TenantStore) -> None:
                self._store = store

            async def resolve(self, request: Request) -> Tenant:
                slug = request.cookies.get("X-Tenant")
                if not slug:
                    raise TenantResolutionError("Cookie missing", strategy="cookie")
                return await self._store.get_by_identifier(slug)

        # Works without inheriting from BaseTenantResolver:
        assert isinstance(CookieTenantResolver(store), TenantResolver)
    """

    async def resolve(self, request: Request) -> Tenant:
        """Resolve the current tenant from *request*.

        Args:
            request: A FastAPI / Starlette ``Request`` instance.

        Returns:
            The resolved ``Tenant``.

        Raises:
            TenantResolutionError: When the request does not carry enough
                information to identify a tenant.
            TenantNotFoundError: When the identifier matches no known tenant.
        """
        ...


@runtime_checkable
class IsolationProvider(Protocol):
    """Structural protocol for data isolation strategies.

    Any object that exposes the four required async methods satisfies this
    protocol and can be injected into ``TenancyManager`` without inheriting
    from ``BaseIsolationProvider``.

    Example — Redis keyspace isolation::

        class RedisIsolationProvider:
            async def get_session(self, tenant: Tenant): ...  # yields RedisClient
            async def apply_filters(self, query, tenant): ...
            async def initialize_tenant(self, tenant): ...
            async def destroy_tenant(self, tenant, **kw): ...
    """

    def get_session(self, tenant: Tenant) -> AsyncIterator[Any]:
        """Yield a session scoped to *tenant*'s namespace."""
        ...

    async def apply_filters(self, query: SelectT, tenant: Tenant) -> SelectT:
        """Return *query* filtered to only expose *tenant*'s data."""
        ...

    async def initialize_tenant(self, tenant: Tenant) -> None:
        """Provision database structures for a newly created *tenant*."""
        ...

    async def destroy_tenant(self, tenant: Tenant, **kwargs: Any) -> None:
        """Deprovision and permanently delete all data for *tenant*."""
        ...


__all__ = [ # noqa
    # Type variables
    "SelectT",
    "TenantT",
    # Enumerations
    "IsolationStrategy",
    "ResolutionStrategy",
    "TenantStatus",
    # Domain models
    "AuditLog",
    "Tenant",
    "TenantConfig",
    "TenantMetrics",
    # Protocols
    "IsolationProvider",
    "TenantResolver",
]
