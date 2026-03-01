"""Configuration management for fastapi-tenancy.

``TenancyConfig`` is a ``pydantic_settings.BaseSettings`` model that reads its
values from environment variables (prefix ``TENANCY_``), an optional ``.env``
file, or explicit keyword arguments.

All cross-field consistency checks run inside ``model_post_init`` so a
misconfigured ``TenancyConfig`` raises immediately at construction time rather
than silently producing wrong behaviour at the first request.

Environment variables
---------------------
Every field can be overridden with ``TENANCY_<FIELD_NAME_UPPER>``::

    TENANCY_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/myapp
    TENANCY_RESOLUTION_STRATEGY=header
    TENANCY_ISOLATION_STRATEGY=schema
    TENANCY_CACHE_ENABLED=true
    TENANCY_REDIS_URL=redis://localhost:6379/0
"""

from __future__ import annotations

import re
from typing import Literal
import warnings

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastapi_tenancy.core.types import IsolationStrategy, ResolutionStrategy


class TenancyConfig(BaseSettings):
    """Central configuration for the fastapi-tenancy library.

    Instances are validated eagerly: invalid field values and inconsistent field
    combinations both raise ``ValidationError`` at construction time.

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
        # Mask passwords in connection URLs (scheme://user:SECRET@host/db).
        text = re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", text)
        # Mask named secret fields regardless of their position.
        text = re.sub(
            r"(jwt_secret|encryption_key|secret|password|key)=(?:'[^']*'|[^\s,)]+)",
            r"\1='***'",
            text,
            flags=re.IGNORECASE,
        )
        return text

    #################
    # Core strategy #
    #################

    resolution_strategy: ResolutionStrategy = Field(
        default=ResolutionStrategy.HEADER,
        description="Strategy used to extract the tenant identifier from incoming requests.",
    )

    isolation_strategy: IsolationStrategy = Field(
        default=IsolationStrategy.SCHEMA,
        description="Strategy used to isolate each tenant's data at the database layer.",
    )

    ############
    # Database #
    ############

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
        description="Extra connections allowed beyond pool_size under burst load.",
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

    database_pool_pre_ping: bool = Field(
        default=True,
        description="Verify connections before checkout.  Recommended for all production deployments.",  # noqa: E501
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

    #####################################
    # Engine cache (DATABASE isolation) #
    #####################################

    max_cached_engines: int = Field(
        default=100,
        ge=10,
        le=10_000,
        description=(
            "Maximum number of per-tenant engines cached by DatabaseIsolationProvider. "
            "Least-recently-used engines are evicted and disposed when this limit is reached. "
            "Set this >= your expected concurrent-tenant count."
        ),
    )

    #################
    # Cache / Redis #
    #################

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

    #################
    # Rate limiting #
    #################

    enable_rate_limiting: bool = Field(
        default=False,
        description="Enable per-tenant sliding-window request rate limiting (requires Redis).",
    )

    rate_limit_per_minute: int = Field(
        default=100,
        ge=1,
        le=10_000,
        description="Default request rate limit per tenant per minute.",
    )

    rate_limit_window_seconds: int = Field(
        default=60,
        ge=1,
        description="Rate-limit sliding window duration in seconds.",
    )

    ##################################
    # Resolution strategy parameters #
    ##################################

    tenant_header_name: str = Field(
        default="X-Tenant-ID",
        description="HTTP header name read by the HEADER resolution strategy.",
    )

    domain_suffix: str | None = Field(
        default=None,
        description=(
            "Base domain suffix for SUBDOMAIN resolution (e.g. ``'.example.com'``)."
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

    ############
    # Security #
    ############

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

    #####################
    # Tenant management #
    #####################

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

    enable_soft_delete: bool = Field(
        default=True,
        description=(
            "Mark tenants as DELETED instead of removing their rows from "
            "the store (recommended for auditability)."
        ),
    )

    ###############################
    # Performance / observability #
    ###############################

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

    ###################
    # Hybrid strategy #
    ###################

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

    #################
    # Schema naming #
    #################

    schema_prefix: str = Field(
        default="tenant_",
        description="Prefix prepended to every tenant schema name.",
    )

    public_schema: str = Field(
        default="public",
        description="Shared public schema name (PostgreSQL).",
    )

    ####################
    # Field validators #
    ####################

    @field_validator("database_url", mode="before")
    @classmethod
    def _validate_database_url(cls, v: str) -> str:
        """Normalise the URL and warn when a synchronous driver is detected.

        Args:
            v: Raw database URL string.

        Returns:
            Normalised URL (trailing slash stripped).

        Raises:
            ValueError: When the URL scheme is unrecognised.
        """
        from fastapi_tenancy.utils.db_compat import detect_dialect  # noqa: PLC0415

        url = str(v).rstrip("/")
        detect_dialect(url)  # validates the scheme is recognised

        _sync_schemes = ("postgresql://", "sqlite://", "mysql://", "mssql://")
        if any(url.startswith(s) for s in _sync_schemes):
            warnings.warn(
                f"database_url uses a synchronous driver ({url.split('://')[0]}://). "
                "Switch to an async driver — e.g. postgresql+asyncpg, "
                "sqlite+aiosqlite, mysql+aiomysql.",
                UserWarning,
                stacklevel=4,
            )
        return url

    @field_validator("jwt_secret")
    @classmethod
    def _validate_jwt_secret(cls, v: str | None, info: ValidationInfo) -> str | None:
        """Require a strong JWT secret when JWT resolution is active.

        Args:
            v: JWT secret value.
            info: Validation context providing access to other field values.

        Returns:
            The validated secret, or ``None``.

        Raises:
            ValueError: When the JWT strategy is active without a secret, or
                when the secret is shorter than 32 characters.
        """
        values = info.data
        if values.get("resolution_strategy") == ResolutionStrategy.JWT and not v:
            msg = "jwt_secret is required when resolution_strategy is 'jwt'."
            raise ValueError(msg)
        if v is not None and len(v) < 32:
            msg = "jwt_secret must be at least 32 characters long."
            raise ValueError(msg)
        return v

    @field_validator("domain_suffix")
    @classmethod
    def _validate_domain_suffix(cls, v: str | None, info: ValidationInfo) -> str | None:
        """Require domain_suffix when SUBDOMAIN resolution is active.

        Args:
            v: Domain suffix value.
            info: Validation context.

        Returns:
            The validated suffix, or ``None``.

        Raises:
            ValueError: When SUBDOMAIN strategy is active without a suffix.
        """
        values = info.data
        if values.get("resolution_strategy") == ResolutionStrategy.SUBDOMAIN and not v:
            msg = "domain_suffix is required when resolution_strategy is 'subdomain'."
            raise ValueError(msg)
        return v

    @field_validator("encryption_key")
    @classmethod
    def _validate_encryption_key(cls, v: str | None, info: ValidationInfo) -> str | None:
        """Require a strong encryption key when at-rest encryption is enabled.

        Args:
            v: Encryption key value.
            info: Validation context.

        Returns:
            The validated key, or ``None``.

        Raises:
            ValueError: When encryption is enabled without a key, or when
                the key is shorter than 32 characters.
        """
        values = info.data
        if values.get("enable_encryption") and not v:
            msg = "encryption_key is required when enable_encryption is True."
            raise ValueError(msg)
        if v is not None and len(v) < 32:
            msg = "encryption_key must be at least 32 characters."
            raise ValueError(msg)
        return v

    @field_validator("schema_prefix")
    @classmethod
    def _validate_schema_prefix(cls, v: str) -> str:
        """Ensure schema_prefix is a valid PostgreSQL identifier fragment.

        Args:
            v: Schema prefix value.

        Returns:
            The validated prefix.

        Raises:
            ValueError: When the prefix contains invalid characters.
        """
        if not re.match(r"^[a-z][a-z0-9_]*$", v):
            msg = (
                "schema_prefix must start with a lowercase letter and contain only "
                "lowercase letters, digits, and underscores."
            )
            raise ValueError(msg)
        return v

    ##########################
    # Cross-field validation #
    ##########################

    @model_validator(mode="after")
    def _validate_cross_field_consistency(self) -> TenancyConfig:
        """Raise ``ValueError`` if the configuration is internally inconsistent.

        Checks:
            - ``cache_enabled`` requires ``redis_url``.
            - ``enable_rate_limiting`` requires ``redis_url``.
            - ``HYBRID`` isolation requires distinct premium and standard strategies.
            - ``DATABASE`` isolation requires ``database_url_template`` with a
              valid placeholder (``{tenant_id}`` or ``{database_name}``).

        Also builds ``_premium_set`` — a ``frozenset`` of premium tenant IDs for
        O(1) lookup, replacing the O(n) ``in list`` check that runs on every request.

        Returns:
            The validated model instance.

        Raises:
            ValueError: On inconsistency.
        """
        if self.cache_enabled and not self.redis_url:
            msg = "cache_enabled=True requires redis_url to be set."
            raise ValueError(msg)

        if self.enable_rate_limiting and not self.redis_url:
            msg = "enable_rate_limiting=True requires redis_url to be set."
            raise ValueError(msg)

        if self.isolation_strategy == IsolationStrategy.HYBRID:  # noqa: SIM102
            if self.premium_isolation_strategy == self.standard_isolation_strategy:
                msg = (
                    "HYBRID isolation requires different strategies for premium and "
                    "standard tenants.  Set premium_isolation_strategy and "
                    "standard_isolation_strategy to different values."
                )
                raise ValueError(msg)

        if self.isolation_strategy == IsolationStrategy.DATABASE:
            if not self.database_url_template:
                msg = (
                    "DATABASE isolation requires database_url_template to be set "
                    "(e.g. 'postgresql+asyncpg://user:pass@host/{database_name}')."
                )
                raise ValueError(msg)
            # Ensure the template actually routes per-tenant; a template without
            # any placeholder silently sends every tenant to the same database.
            if "{tenant_id}" not in self.database_url_template and \
               "{database_name}" not in self.database_url_template:
                msg = (
                    "database_url_template must contain at least one placeholder: "
                    "'{tenant_id}' or '{database_name}'.  A template without a "
                    "placeholder routes all tenants to the same database."
                )
                raise ValueError(msg)

        # Build an O(1) lookup set from the list field.  BaseSettings is not
        # frozen by default, so normal attribute assignment is correct here.
        # (object.__setattr__ is only needed for frozen Pydantic models.)
        self._premium_set: frozenset[str] = frozenset(self.premium_tenants)

        return self

    ##################
    # Helper methods #
    ##################

    def get_schema_name(self, tenant_identifier: str) -> str:
        """Compute the PostgreSQL schema name for *tenant_identifier*.

        The identifier is validated against tenant slug rules before use to
        prevent SQL injection via schema names.

        Args:
            tenant_identifier: The tenant's human-readable slug.

        Returns:
            Schema name string (e.g. ``"tenant_acme_corp"``).

        Raises:
            ValueError: When *tenant_identifier* fails validation.

        Example::

            config.get_schema_name("acme-corp")  # → "tenant_acme_corp"
        """
        from fastapi_tenancy.utils.validation import validate_tenant_identifier  # noqa: PLC0415

        if not validate_tenant_identifier(tenant_identifier):
            msg = f"Invalid tenant identifier for schema name: {tenant_identifier!r}"
            raise ValueError(msg)
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

        Uses an internal ``frozenset`` built at construction time for O(1)
        lookup — the ``premium_tenants`` list may contain thousands of IDs and
        this method is called on every request in HYBRID mode.

        Args:
            tenant_id: Tenant ID to check.

        Returns:
            ``True`` for premium tenants; ``False`` for standard tenants.
        """
        # _premium_set is built by _validate_cross_field_consistency at model
        # construction time.  Fallback to a fresh frozenset in case the method
        # is called on a partially-initialised instance (e.g. in unit tests
        # that bypass the normal constructor path).
        premium_set: frozenset[str] = getattr(self, "_premium_set", frozenset(self.premium_tenants))
        return tenant_id in premium_set

    def get_isolation_strategy_for_tenant(self, tenant_id: str) -> IsolationStrategy:
        """Return the effective isolation strategy for *tenant_id*.

        In ``HYBRID`` mode this dispatches to ``premium_isolation_strategy``
        or ``standard_isolation_strategy`` based on the tenant's tier.  In
        all other modes it returns ``isolation_strategy`` unchanged.

        Args:
            tenant_id: Tenant ID to evaluate.

        Returns:
            The ``IsolationStrategy`` to apply.
        """
        if self.isolation_strategy != IsolationStrategy.HYBRID:
            return self.isolation_strategy
        return (
            self.premium_isolation_strategy
            if self.is_premium_tenant(tenant_id)
            else self.standard_isolation_strategy
        )


__all__ = ["TenancyConfig"]
