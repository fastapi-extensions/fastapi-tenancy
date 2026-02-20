"""Configuration management for fastapi-tenancy.

:class:`TenancyConfig` is a :class:`~pydantic_settings.BaseSettings` model
that reads its values from environment variables (prefix ``TENANCY_``), an
optional ``.env`` file, or explicit keyword arguments.

All cross-field consistency checks run inside :meth:`model_post_init` so
a misconfigured :class:`TenancyConfig` raises immediately at construction
time rather than during the first request.

Environment variables
---------------------
Every field can be overridden by an environment variable named
``TENANCY_<FIELD_NAME_UPPER>``.  For example::

    TENANCY_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/myapp
    TENANCY_RESOLUTION_STRATEGY=header
    TENANCY_ISOLATION_STRATEGY=schema
    TENANCY_CACHE_ENABLED=true
    TENANCY_REDIS_URL=redis://localhost:6379/0
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastapi_tenancy.core.types import IsolationStrategy, ResolutionStrategy


class TenancyConfig(BaseSettings):
    """Central configuration for the fastapi-tenancy library.

    Instances are validated eagerly: invalid field values and inconsistent
    field combinations both raise :class:`ValueError` (or
    :class:`~pydantic.ValidationError`) at construction time.

    Example — programmatic::

        config = TenancyConfig(
            database_url="postgresql+asyncpg://user:pass@localhost/myapp",
            resolution_strategy="header",
            isolation_strategy="schema",
        )

    Example — environment variables::

        # .env
        TENANCY_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/myapp
        TENANCY_RESOLUTION_STRATEGY=subdomain
        TENANCY_DOMAIN_SUFFIX=.example.com
        TENANCY_ISOLATION_STRATEGY=rls

        config = TenancyConfig()  # reads from environment / .env
    """

    model_config = SettingsConfigDict(
        env_prefix="TENANCY_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def __str__(self) -> str:
        """Return a masked string representation safe for logging."""
        text = super().__repr__()
        # Mask passwords in connection URLs (postgresql://user:SECRET@host/db)
        text = re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", text)
        # Mask named secret fields
        text = re.sub(
            r"(jwt_secret|encryption_key|secret|password|key)=(?:'[^']*'|[^\s,)]+)",
            r"\1='***'",
            text,
            flags=re.IGNORECASE,
        )
        return text

    # ------------------------------------------------------------------
    # Core strategy
    # ------------------------------------------------------------------

    resolution_strategy: ResolutionStrategy = Field(
        default=ResolutionStrategy.HEADER,
        description="Strategy used to extract the tenant identifier from incoming requests.",
    )

    isolation_strategy: IsolationStrategy = Field(
        default=IsolationStrategy.SCHEMA,
        description="Strategy used to isolate each tenant's data at the database layer.",
    )

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

    database_url: str = Field(
        ...,
        description=(
            "Async SQLAlchemy connection URL.  Use an async driver: "
            "postgresql+asyncpg, sqlite+aiosqlite, mysql+aiomysql."
        ),
    )

    database_pool_size: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Number of persistent connections in the pool.",
    )

    database_max_overflow: int = Field(
        default=40,
        ge=0,
        le=200,
        description="Extra connections allowed beyond pool_size under peak load.",
    )

    database_pool_timeout: int = Field(
        default=30,
        ge=1,
        description="Seconds to wait for a pool connection before raising.",
    )

    database_pool_recycle: int = Field(
        default=3600,
        ge=60,
        description="Seconds after which a connection is proactively replaced.",
    )

    database_echo: bool = Field(
        default=False,
        description="Log every SQL statement.  Enable only during development.",
    )

    database_url_template: str | None = Field(
        default=None,
        description=(
            "URL template for per-tenant databases (DATABASE isolation). "
            "Supports ``{tenant_id}`` and ``{database_name}`` placeholders."
        ),
    )

    # ------------------------------------------------------------------
    # Cache / Redis
    # ------------------------------------------------------------------

    redis_url: str | None = Field(
        default=None,
        description="Redis connection URL.  Required when cache_enabled=True.",
    )

    cache_ttl: int = Field(
        default=3600,
        ge=0,
        description="Default time-to-live for cached tenant objects (seconds).",
    )

    cache_enabled: bool = Field(
        default=False,
        description="Enable the Redis write-through cache for tenant lookups.",
    )

    # ------------------------------------------------------------------
    # Resolution strategy parameters
    # ------------------------------------------------------------------

    tenant_header_name: str = Field(
        default="X-Tenant-ID",
        description="HTTP header name read by the HEADER resolution strategy.",
    )

    domain_suffix: str | None = Field(
        default=None,
        description=(
            "Base domain suffix for SUBDOMAIN resolution "
            "(e.g. ``'.example.com'``)."
        ),
    )

    path_prefix: str = Field(
        default="/tenants",
        description="URL path prefix for PATH resolution (e.g. ``'/tenants'``).",
    )

    jwt_secret: str | None = Field(
        default=None,
        description="Secret key for JWT verification.  Required for JWT resolution.",
    )

    jwt_algorithm: str = Field(
        default="HS256",
        description="JWT signing algorithm (e.g. ``'HS256'``, ``'RS256'``).",
    )

    jwt_tenant_claim: str = Field(
        default="tenant_id",
        description="JWT payload claim that carries the tenant identifier.",
    )

    # ------------------------------------------------------------------
    # Security
    # ------------------------------------------------------------------

    enable_rate_limiting: bool = Field(
        default=True,
        description="Enable per-tenant request rate limiting.",
    )

    rate_limit_per_minute: int = Field(
        default=100,
        ge=1,
        le=10_000,
        description="Default request rate limit per tenant per minute.",
    )

    rate_limit_window: int = Field(
        default=60,
        ge=1,
        description="Rate-limit window in seconds.",
    )

    enable_audit_logging: bool = Field(
        default=True,
        description="Record tenant operations in the audit log.",
    )

    enable_encryption: bool = Field(
        default=False,
        description="Encrypt sensitive tenant data at rest.",
    )

    encryption_key: str | None = Field(
        default=None,
        description=(
            "Base64-encoded 32-byte encryption key.  Required when "
            "enable_encryption=True."
        ),
    )

    # ------------------------------------------------------------------
    # Tenant management
    # ------------------------------------------------------------------

    allow_tenant_registration: bool = Field(
        default=False,
        description="Allow self-service tenant registration via the API.",
    )

    max_tenants: int | None = Field(
        default=None,
        ge=1,
        description="Hard cap on the number of tenants (None = unlimited).",
    )

    default_tenant_status: Literal["active", "suspended", "provisioning"] = Field(
        default="active",
        description="Initial status assigned to newly created tenants.",
    )

    # ------------------------------------------------------------------
    # Performance
    # ------------------------------------------------------------------

    enable_query_logging: bool = Field(
        default=False,
        description="Log slow database queries.",
    )

    slow_query_threshold_ms: int = Field(
        default=1000,
        ge=0,
        description="Query duration threshold for slow-query logging (ms).",
    )

    enable_metrics: bool = Field(
        default=True,
        description="Expose tenant usage metrics.",
    )

    # ------------------------------------------------------------------
    # Hybrid strategy
    # ------------------------------------------------------------------

    premium_tenants: list[str] = Field(
        default_factory=list,
        description="Tenant IDs that receive premium isolation treatment in HYBRID mode.",
    )

    premium_isolation_strategy: IsolationStrategy = Field(
        default=IsolationStrategy.SCHEMA,
        description="Isolation strategy applied to premium tenants in HYBRID mode.",
    )

    standard_isolation_strategy: IsolationStrategy = Field(
        default=IsolationStrategy.RLS,
        description="Isolation strategy applied to standard tenants in HYBRID mode.",
    )

    # ------------------------------------------------------------------
    # Schema naming
    # ------------------------------------------------------------------

    schema_prefix: str = Field(
        default="tenant_",
        description="Prefix prepended to every tenant schema name.",
    )

    public_schema: str = Field(
        default="public",
        description="Shared public schema name (PostgreSQL / MSSQL).",
    )

    # ------------------------------------------------------------------
    # Feature flags
    # ------------------------------------------------------------------

    enable_tenant_suspend: bool = Field(
        default=True,
        description="Allow tenants to be suspended via the management API.",
    )

    enable_soft_delete: bool = Field(
        default=True,
        description=(
            "Mark tenants as DELETED instead of removing their rows from "
            "the store (recommended for auditability)."
        ),
    )

    # ------------------------------------------------------------------
    # Field validators
    # ------------------------------------------------------------------

    @field_validator("database_url", mode="before")
    @classmethod
    def _validate_database_url(cls, v: str) -> str:
        """Normalise the database URL and warn when a sync driver is detected.

        Strips any trailing slash that URL normalisation may append.
        Emits a :class:`UserWarning` when the URL scheme uses a synchronous
        driver (e.g. ``postgresql://`` instead of ``postgresql+asyncpg://``).
        """
        import warnings

        from fastapi_tenancy.utils.db_compat import detect_dialect

        url = str(v).rstrip("/")
        detect_dialect(url)  # validates the scheme is recognised

        _SYNC_SCHEMES = ("postgresql://", "sqlite://", "mysql://", "mssql://")
        if any(url.startswith(s) for s in _SYNC_SCHEMES):
            warnings.warn(
                f"database_url uses a synchronous driver scheme ({url.split('://')[0]}://). "
                "Switch to an async driver — e.g. postgresql+asyncpg, "
                "sqlite+aiosqlite, mysql+aiomysql.",
                UserWarning,
                stacklevel=4,
            )
        return url

    @field_validator("jwt_secret")
    @classmethod
    def _validate_jwt_secret(cls, v: str | None, info: ValidationInfo) -> str | None:
        """Require a JWT secret when the JWT resolution strategy is active.

        Also enforces a minimum length of 32 characters to prevent trivially
        weak secrets from being accepted.
        """
        values = info.data
        if values.get("resolution_strategy") == ResolutionStrategy.JWT and not v:
            raise ValueError(
                "jwt_secret is required when resolution_strategy is 'jwt'."
            )
        if v is not None and len(v) < 32:
            raise ValueError("jwt_secret must be at least 32 characters long.")
        return v

    @field_validator("domain_suffix")
    @classmethod
    def _validate_domain_suffix(
        cls, v: str | None, info: ValidationInfo
    ) -> str | None:
        """Require domain_suffix when the SUBDOMAIN resolution strategy is active."""
        values = info.data
        if (
            values.get("resolution_strategy") == ResolutionStrategy.SUBDOMAIN
            and not v
        ):
            raise ValueError(
                "domain_suffix is required when resolution_strategy is 'subdomain'."
            )
        return v

    @field_validator("encryption_key")
    @classmethod
    def _validate_encryption_key(
        cls, v: str | None, info: ValidationInfo
    ) -> str | None:
        """Require an encryption key when data-at-rest encryption is enabled."""
        values = info.data
        if values.get("enable_encryption") and not v:
            raise ValueError(
                "encryption_key is required when enable_encryption is True."
            )
        if v is not None and len(v) < 32:
            raise ValueError(
                "encryption_key must be at least 32 characters (base64-encoded 32 bytes)."
            )
        return v

    @field_validator("schema_prefix")
    @classmethod
    def _validate_schema_prefix(cls, v: str) -> str:
        """Ensure schema_prefix is a valid PostgreSQL identifier fragment."""
        import re

        if not re.match(r"^[a-z][a-z0-9_]*$", v):
            raise ValueError(
                "schema_prefix must start with a lowercase letter and contain only "
                "lowercase letters, digits, and underscores."
            )
        return v

    # ------------------------------------------------------------------
    # Post-init cross-field validation
    # ------------------------------------------------------------------

    def model_post_init(self, __context: object) -> None:
        """Run cross-field consistency checks after model construction."""
        self._validate_configuration()

    def _validate_configuration(self) -> None:
        """Raise :class:`ValueError` if the configuration is internally inconsistent.

        Checks
        ------
        * ``cache_enabled`` requires ``redis_url``.
        * ``HYBRID`` isolation requires distinct premium and standard strategies.
        * ``DATABASE`` isolation requires ``database_url_template``.
        """
        if self.cache_enabled and not self.redis_url:
            raise ValueError(
                "cache_enabled=True requires redis_url to be set."
            )

        if self.isolation_strategy == IsolationStrategy.HYBRID:
            if self.premium_isolation_strategy == self.standard_isolation_strategy:
                raise ValueError(
                    "HYBRID isolation requires different strategies for premium and "
                    "standard tenants.  Set premium_isolation_strategy and "
                    "standard_isolation_strategy to different values."
                )

        if self.isolation_strategy == IsolationStrategy.DATABASE:
            if not self.database_url_template:
                raise ValueError(
                    "DATABASE isolation requires database_url_template to be set "
                    "(e.g. 'postgresql+asyncpg://user:pass@host/{database_name}')."
                )

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    def get_schema_name(self, tenant_identifier: str) -> str:
        """Compute the PostgreSQL schema name for *tenant_identifier*.

        The identifier is validated against the tenant slug rules before
        use to prevent SQL injection via schema names.

        Args:
            tenant_identifier: The tenant's human-readable slug.

        Returns:
            Schema name string (e.g. ``"tenant_acme_corp"``).

        Raises:
            ValueError: When *tenant_identifier* is invalid.

        Example::

            config.get_schema_name("acme-corp")  # → "tenant_acme_corp"
        """
        from fastapi_tenancy.utils.validation import validate_tenant_identifier

        if not validate_tenant_identifier(tenant_identifier):
            raise ValueError(
                f"Invalid tenant identifier for schema name: {tenant_identifier!r}"
            )
        sanitised = tenant_identifier.replace("-", "_").replace(".", "_")
        return f"{self.schema_prefix}{sanitised}"

    def get_database_url_for_tenant(self, tenant_id: str) -> str:
        """Build the database URL for *tenant_id* in DATABASE isolation mode.

        Args:
            tenant_id: The tenant's opaque unique ID.

        Returns:
            A fully-qualified async database URL string.
        """
        if self.database_url_template:
            db_name = tenant_id.replace("-", "_").replace(".", "_").lower()
            return self.database_url_template.format(
                tenant_id=tenant_id,
                database_name=f"tenant_{db_name}_db",
            )
        return str(self.database_url)

    def is_premium_tenant(self, tenant_id: str) -> bool:
        """Return ``True`` if *tenant_id* is in the premium-tenants list.

        Args:
            tenant_id: Tenant ID to check.

        Returns:
            ``True`` for premium tenants; ``False`` for standard tenants.
        """
        return tenant_id in self.premium_tenants

    def get_isolation_strategy_for_tenant(self, tenant_id: str) -> IsolationStrategy:
        """Return the effective isolation strategy for *tenant_id*.

        In ``HYBRID`` mode this dispatches to
        :attr:`premium_isolation_strategy` or :attr:`standard_isolation_strategy`
        based on the tenant's tier.  In all other modes it returns
        :attr:`isolation_strategy` unchanged.

        Args:
            tenant_id: Tenant ID to evaluate.

        Returns:
            The :class:`~fastapi_tenancy.core.types.IsolationStrategy` to apply.
        """
        if self.isolation_strategy != IsolationStrategy.HYBRID:
            return self.isolation_strategy
        return (
            self.premium_isolation_strategy
            if self.is_premium_tenant(tenant_id)
            else self.standard_isolation_strategy
        )


__all__ = ["TenancyConfig"]
