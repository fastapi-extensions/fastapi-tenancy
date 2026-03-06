"""Tests for :mod:`fastapi_tenancy.manager`."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.exceptions import (
    ConfigurationError,
    RateLimitExceededError,
    TenancyError,
    TenantNotFoundError,
)
from fastapi_tenancy.core.types import (
    AuditLog,
    IsolationStrategy,
    ResolutionStrategy,
    Tenant,
    TenantStatus,
)
from fastapi_tenancy.manager import (
    TenancyManager,
    _build_provider,
    _build_resolver,
    _DefaultAuditLogWriter,
)
from fastapi_tenancy.storage.memory import InMemoryTenantStore


def _cfg(**kw: Any) -> TenancyConfig:
    defaults: dict[str, Any] = {
        "database_url": "sqlite+aiosqlite:///:memory:",
        "resolution_strategy": ResolutionStrategy.HEADER,
        "isolation_strategy": IsolationStrategy.SCHEMA,
    }
    defaults.update(kw)
    return TenancyConfig(**defaults)


def _tenant(identifier: str = "acme-corp", status: TenantStatus = TenantStatus.ACTIVE) -> Tenant:
    now = datetime.now(UTC)
    return Tenant(
        id=f"t-{identifier}",
        identifier=identifier,
        name=identifier.title(),
        status=status,
        created_at=now,
        updated_at=now,
    )


async def _store_with(*tenants: Tenant) -> InMemoryTenantStore:
    store = InMemoryTenantStore()
    for t in tenants:
        await store.create(t)
    return store


class TestBuildResolver:
    def test_header_strategy(self) -> None:
        from fastapi_tenancy.resolution.header import HeaderTenantResolver  # noqa: PLC0415

        resolver = _build_resolver(_cfg(), InMemoryTenantStore())
        assert isinstance(resolver, HeaderTenantResolver)

    def test_subdomain_strategy(self) -> None:
        from fastapi_tenancy.resolution.subdomain import SubdomainTenantResolver  # noqa: PLC0415

        cfg = _cfg(
            resolution_strategy=ResolutionStrategy.SUBDOMAIN,
            domain_suffix=".example.com",
        )
        resolver = _build_resolver(cfg, InMemoryTenantStore())
        assert isinstance(resolver, SubdomainTenantResolver)

    def test_path_strategy(self) -> None:
        from fastapi_tenancy.resolution.path import PathTenantResolver  # noqa: PLC0415

        cfg = _cfg(resolution_strategy=ResolutionStrategy.PATH)
        resolver = _build_resolver(cfg, InMemoryTenantStore())
        assert isinstance(resolver, PathTenantResolver)

    def test_jwt_strategy_with_secret(self) -> None:
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver  # noqa: PLC0415

        secret = "x" * 32
        cfg = _cfg(
            resolution_strategy=ResolutionStrategy.JWT,
            jwt_secret=secret,
        )
        resolver = _build_resolver(cfg, InMemoryTenantStore())
        assert isinstance(resolver, JWTTenantResolver)

    def test_jwt_strategy_without_secret_raises(self) -> None:
        # We can't pass jwt_secret=None to _cfg when resolution_strategy=JWT
        # because TenancyConfig validates it. So test _build_resolver directly
        # by building a HEADER config then patching the strategy and secret.
        cfg = _cfg(resolution_strategy=ResolutionStrategy.HEADER)
        # Manually override the validated config attributes
        object.__setattr__(cfg, "resolution_strategy", ResolutionStrategy.JWT)
        object.__setattr__(cfg, "jwt_secret", None)
        with pytest.raises(ConfigurationError, match="jwt_secret"):
            _build_resolver(cfg, InMemoryTenantStore())

    def test_custom_strategy_with_resolver(self) -> None:
        cfg = _cfg(resolution_strategy=ResolutionStrategy.CUSTOM)
        custom = MagicMock()
        result = _build_resolver(cfg, InMemoryTenantStore(), custom_resolver=custom)
        assert result is custom

    def test_custom_strategy_without_resolver_raises(self) -> None:
        cfg = _cfg(resolution_strategy=ResolutionStrategy.CUSTOM)
        with pytest.raises(ConfigurationError, match="custom_resolver"):
            _build_resolver(cfg, InMemoryTenantStore())

    def test_non_custom_with_custom_resolver_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg = _cfg(resolution_strategy=ResolutionStrategy.HEADER)
        custom = MagicMock()
        with caplog.at_level(logging.WARNING, logger="fastapi_tenancy.manager"):
            _build_resolver(cfg, InMemoryTenantStore(), custom_resolver=custom)
        assert "ignored" in caplog.text.lower() or "custom_resolver" in caplog.text.lower()


class TestBuildProvider:
    def test_schema_provider(self) -> None:
        from fastapi_tenancy.isolation.schema import SchemaIsolationProvider  # noqa: PLC0415

        provider = _build_provider(_cfg(isolation_strategy=IsolationStrategy.SCHEMA))
        assert isinstance(provider, SchemaIsolationProvider)

    def test_rls_provider(self) -> None:
        from fastapi_tenancy.isolation.rls import RLSIsolationProvider  # noqa: PLC0415

        # RLS requires PostgreSQL, so we need to use a PostgreSQL URL
        provider = _build_provider(
            _cfg(
                isolation_strategy=IsolationStrategy.RLS,
                database_url="postgresql+asyncpg://user:pass@localhost/db",
            )
        )
        assert isinstance(provider, RLSIsolationProvider)

    def test_database_provider(self) -> None:
        from fastapi_tenancy.isolation.database import DatabaseIsolationProvider  # noqa: PLC0415

        cfg = _cfg(
            isolation_strategy=IsolationStrategy.DATABASE,
            database_url_template="sqlite+aiosqlite:///./test_{database_name}.db",
        )
        provider = _build_provider(cfg)
        assert isinstance(provider, DatabaseIsolationProvider)

    def test_hybrid_provider(self) -> None:
        from fastapi_tenancy.isolation.hybrid import HybridIsolationProvider  # noqa: PLC0415

        cfg = _cfg(
            isolation_strategy=IsolationStrategy.HYBRID,
            premium_isolation_strategy=IsolationStrategy.SCHEMA,
            standard_isolation_strategy=IsolationStrategy.RLS,
            database_url="postgresql+asyncpg://user:pass@localhost/db",  # RLS requires PostgreSQL
        )
        provider = _build_provider(cfg)
        assert isinstance(provider, HybridIsolationProvider)


class TestManagerInit:
    def test_default_audit_writer(self) -> None:
        m = TenancyManager(_cfg(), InMemoryTenantStore())
        assert isinstance(m._audit_writer, _DefaultAuditLogWriter)

    def test_custom_audit_writer_stored(self) -> None:
        writer = MagicMock()
        m = TenancyManager(_cfg(), InMemoryTenantStore(), audit_writer=writer)
        assert m._audit_writer is writer

    def test_custom_isolation_provider_stored(self) -> None:
        provider = MagicMock()
        m = TenancyManager(_cfg(), InMemoryTenantStore(), isolation_provider=provider)
        assert m.isolation_provider is provider

    def test_rate_limiting_disabled_flag(self) -> None:
        m = TenancyManager(_cfg(enable_rate_limiting=False), InMemoryTenantStore())
        assert m._rate_limiting_enabled is False


class TestManagerInitialize:
    @pytest.mark.asyncio
    async def test_calls_store_initialize(self) -> None:
        store = MagicMock()
        store.initialize = AsyncMock()
        store.close = AsyncMock()
        m = TenancyManager(_cfg(), store)
        await m.initialize()
        store.initialize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_warms_cache_when_enabled(self) -> None:
        # cache_enabled=True requires redis_url per config validator
        # Use a mock store with warm_cache and mock the config
        store = MagicMock()
        store.initialize = AsyncMock()
        store.warm_cache = AsyncMock()
        store.close = AsyncMock()
        cfg = _cfg()
        # Bypass config validator by monkeypatching after construction
        object.__setattr__(cfg, "cache_enabled", True)
        m = TenancyManager(cfg, store)
        await m.initialize()
        store.warm_cache.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_warm_cache_when_disabled(self) -> None:
        store = MagicMock()
        store.initialize = AsyncMock()
        store.warm_cache = AsyncMock()
        store.close = AsyncMock()
        m = TenancyManager(_cfg(cache_enabled=False), store)
        await m.initialize()
        store.warm_cache.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_initialize_when_store_lacks_method(self) -> None:
        store = InMemoryTenantStore()
        m = TenancyManager(_cfg(), store)
        await m.initialize()  # InMemoryTenantStore has no initialize() — must not raise

    @pytest.mark.asyncio
    async def test_no_rate_limiter_init_when_disabled(self) -> None:
        cfg = _cfg(enable_rate_limiting=False)
        m = TenancyManager(cfg, InMemoryTenantStore())
        m._init_rate_limiter = AsyncMock()  # type: ignore[method-assign]
        await m.initialize()
        m._init_rate_limiter.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_rate_limiter_init_when_no_redis_url(self) -> None:
        # enable_rate_limiting=True requires redis_url per config validator.
        # Use patching to simulate the condition.
        cfg = _cfg()
        object.__setattr__(cfg, "enable_rate_limiting", True)
        object.__setattr__(cfg, "redis_url", None)
        m = TenancyManager(cfg, InMemoryTenantStore())
        m._init_rate_limiter = AsyncMock()  # type: ignore[method-assign]
        await m.initialize()
        m._init_rate_limiter.assert_not_awaited()


class TestManagerClose:
    @pytest.mark.asyncio
    async def test_closes_isolation_provider(self) -> None:
        provider = MagicMock()
        provider.close = AsyncMock()
        store = MagicMock()
        store.close = AsyncMock()
        m = TenancyManager(_cfg(), store, isolation_provider=provider)
        await m.close()
        provider.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_closes_store(self) -> None:
        store = MagicMock()
        store.close = AsyncMock()
        m = TenancyManager(_cfg(), store)
        await m.close()
        store.close.assert_awaited_once()


class TestCreateLifespan:
    @pytest.mark.asyncio
    async def test_lifespan_calls_initialize_and_close(self) -> None:
        m = TenancyManager(_cfg(), InMemoryTenantStore())
        m.initialize = AsyncMock()  # type: ignore[method-assign]
        m.close = AsyncMock()  # type: ignore[method-assign]

        lifespan = m.create_lifespan()
        async with lifespan(MagicMock()):
            pass

        m.initialize.assert_awaited_once()
        m.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lifespan_calls_close_on_exception(self) -> None:
        m = TenancyManager(_cfg(), InMemoryTenantStore())
        m.initialize = AsyncMock()  # type: ignore[method-assign]
        m.close = AsyncMock()  # type: ignore[method-assign]

        lifespan = m.create_lifespan()
        with pytest.raises(ValueError):
            async with lifespan(MagicMock()):
                raise ValueError("boom")

        m.close.assert_awaited_once()


class TestRegisterTenant:
    @pytest.mark.asyncio
    async def test_invalid_identifier_raises_value_error(self) -> None:
        m = TenancyManager(_cfg(), InMemoryTenantStore())
        with pytest.raises(ValueError, match="Invalid tenant identifier"):
            await m.register_tenant("INVALID!!!", "Test Tenant")

    @pytest.mark.asyncio
    async def test_valid_registration_creates_tenant(self) -> None:
        store = InMemoryTenantStore()
        m = TenancyManager(_cfg(), store)
        await m.initialize()
        m.isolation_provider.initialize_tenant = AsyncMock()  # type: ignore[method-assign]

        tenant = await m.register_tenant("new-tenant", "New Tenant")
        assert tenant.identifier == "new-tenant"
        assert tenant.name == "New Tenant"

    @pytest.mark.asyncio
    async def test_isolation_failure_rolls_back_store(self) -> None:
        """If initialize_tenant raises, the tenant is deleted from store."""
        store = InMemoryTenantStore()
        m = TenancyManager(_cfg(), store)
        await m.initialize()
        m.isolation_provider.initialize_tenant = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("DB unreachable")
        )

        with pytest.raises(TenancyError, match="Failed to initialise"):
            await m.register_tenant("rollback-tenant", "Rollback Me")

        # Tenant should not be findable after rollback
        with pytest.raises(TenantNotFoundError):
            await store.get_by_identifier("rollback-tenant")

    @pytest.mark.asyncio
    async def test_with_explicit_isolation_strategy(self) -> None:
        store = InMemoryTenantStore()
        m = TenancyManager(_cfg(), store)
        await m.initialize()
        m.isolation_provider.initialize_tenant = AsyncMock()  # type: ignore[method-assign]

        tenant = await m.register_tenant(
            "explicit-strat",
            "Explicit",
            isolation_strategy=IsolationStrategy.SCHEMA,
        )
        assert tenant.isolation_strategy == IsolationStrategy.SCHEMA

    @pytest.mark.asyncio
    async def test_with_metadata(self) -> None:
        store = InMemoryTenantStore()
        m = TenancyManager(_cfg(), store)
        await m.initialize()
        m.isolation_provider.initialize_tenant = AsyncMock()  # type: ignore[method-assign]

        tenant = await m.register_tenant(
            "meta-tenant",
            "Meta",
            metadata={"plan": "premium"},
        )
        assert tenant.metadata["plan"] == "premium"


class TestSuspendActivate:
    @pytest.mark.asyncio
    async def test_suspend_tenant(self) -> None:
        t = _tenant()
        store = await _store_with(t)
        m = TenancyManager(_cfg(), store)

        result = await m.suspend_tenant(t.id)
        assert result.status == TenantStatus.SUSPENDED

    @pytest.mark.asyncio
    async def test_activate_tenant(self) -> None:
        t = _tenant(status=TenantStatus.SUSPENDED)
        store = await _store_with(t)
        m = TenancyManager(_cfg(), store)

        result = await m.activate_tenant(t.id)
        assert result.status == TenantStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_suspend_not_found_raises(self) -> None:
        m = TenancyManager(_cfg(), InMemoryTenantStore())
        with pytest.raises(TenantNotFoundError):
            await m.suspend_tenant("nonexistent-id")


class TestDeleteTenant:
    @pytest.mark.asyncio
    async def test_not_found_raises(self) -> None:
        m = TenancyManager(_cfg(), InMemoryTenantStore())
        with pytest.raises(TenantNotFoundError):
            await m.delete_tenant("ghost-id")

    @pytest.mark.asyncio
    async def test_soft_delete(self) -> None:
        t = _tenant()
        store = await _store_with(t)
        # enable_soft_delete=True is the default
        m = TenancyManager(_cfg(enable_soft_delete=True), store)

        await m.delete_tenant(t.id)
        result = await store.get_by_id(t.id)
        assert result.status == TenantStatus.DELETED

    @pytest.mark.asyncio
    async def test_hard_delete(self) -> None:
        t = _tenant()
        store = await _store_with(t)
        m = TenancyManager(_cfg(enable_soft_delete=False), store)

        await m.delete_tenant(t.id, destroy_data=False)
        with pytest.raises(TenantNotFoundError):
            await store.get_by_id(t.id)

    @pytest.mark.asyncio
    async def test_destroy_data_calls_provider(self) -> None:
        t = _tenant()
        store = await _store_with(t)
        m = TenancyManager(_cfg(enable_soft_delete=False), store)
        m.isolation_provider.destroy_tenant = AsyncMock()  # type: ignore[method-assign]

        await m.delete_tenant(t.id, destroy_data=True)
        m.isolation_provider.destroy_tenant.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_soft_delete_does_not_call_destroy(self) -> None:
        """Soft delete must NOT call destroy_tenant even if destroy_data=True."""
        t = _tenant()
        store = await _store_with(t)
        m = TenancyManager(_cfg(enable_soft_delete=True), store)
        m.isolation_provider.destroy_tenant = AsyncMock()  # type: ignore[method-assign]

        await m.delete_tenant(t.id, destroy_data=True)
        m.isolation_provider.destroy_tenant.assert_not_awaited()


class TestCheckRateLimit:
    @pytest.mark.asyncio
    async def test_disabled_returns_immediately(self) -> None:
        m = TenancyManager(_cfg(), InMemoryTenantStore())
        m._rate_limiting_enabled = False
        await m.check_rate_limit(_tenant())  # must not raise

    @pytest.mark.asyncio
    async def test_no_limiter_returns_immediately(self) -> None:
        m = TenancyManager(_cfg(), InMemoryTenantStore())
        m._rate_limiting_enabled = True
        m._rate_limiter = None
        await m.check_rate_limit(_tenant())  # must not raise

    @pytest.mark.asyncio
    async def test_count_within_limit_does_not_raise(self) -> None:
        m = TenancyManager(_cfg(), InMemoryTenantStore())
        m._rate_limiting_enabled = True
        fake_redis = MagicMock()
        fake_redis.eval = AsyncMock(return_value=50)
        m._rate_limiter = fake_redis
        # Default rate_limit_per_minute = 100, count=50 ≤ 100
        await m.check_rate_limit(_tenant())

    @pytest.mark.asyncio
    async def test_count_exceeds_limit_raises(self) -> None:
        # Build config with rate_limit_per_minute=100 (default)
        m = TenancyManager(_cfg(), InMemoryTenantStore())
        m._rate_limiting_enabled = True
        fake_redis = MagicMock()
        # Lua script returns count > limit → RateLimitExceededError
        fake_redis.eval = AsyncMock(return_value=101)
        m._rate_limiter = fake_redis

        with pytest.raises(RateLimitExceededError):
            await m.check_rate_limit(_tenant())

    @pytest.mark.asyncio
    async def test_redis_exception_is_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        m = TenancyManager(_cfg(), InMemoryTenantStore())
        m._rate_limiting_enabled = True
        fake_redis = MagicMock()
        fake_redis.eval = AsyncMock(side_effect=ConnectionError("Redis down"))
        m._rate_limiter = fake_redis

        with caplog.at_level(logging.WARNING):
            await m.check_rate_limit(_tenant())  # must not raise

        # Should log a warning
        assert len(caplog.records) >= 1

    @pytest.mark.asyncio
    async def test_count_exactly_at_limit_does_not_raise(self) -> None:
        """The Lua script returns count+1 for requests that pass.
        So count==limit means the request was already at capacity and denied
        (script returns limit without incrementing). But when the return value
        equals limit it means count WAS limit, so > limit check passes.
        Actually: the Lua script returns limit (not limit+1) when at capacity.
        So count == limit means denied. count < limit means count+1 was returned.
        We test with count=100 (== limit) → should raise.
        We test with count=99 (< limit, Lua returned 100 which == limit but actually
        this is the new count after increment so 100 == limit, NOT > limit).
        The check is: if count > limit → raise. So count=100 with limit=100 → no raise.
        """
        m = TenancyManager(_cfg(), InMemoryTenantStore())
        m._rate_limiting_enabled = True
        fake_redis = MagicMock()
        # When Lua returns limit (100) after adding request (count was 99), count == limit
        # The check is count > limit, so 100 > 100 is False → no raise
        fake_redis.eval = AsyncMock(return_value=100)
        m._rate_limiter = fake_redis

        await m.check_rate_limit(_tenant())  # 100 is not > 100, so no raise


class TestWriteAuditLog:
    @pytest.mark.asyncio
    async def test_delegates_to_audit_writer(self) -> None:
        writer = MagicMock()
        writer.write = AsyncMock()
        m = TenancyManager(_cfg(), InMemoryTenantStore(), audit_writer=writer)

        entry = AuditLog(tenant_id="t-1", action="create", resource="order")
        await m.write_audit_log(entry)
        writer.write.assert_awaited_once_with(entry)


class TestDefaultAuditLogWriter:
    @pytest.mark.asyncio
    async def test_logs_at_info_level(self, caplog: pytest.LogCaptureFixture) -> None:
        writer = _DefaultAuditLogWriter()
        entry = AuditLog(
            tenant_id="t-test",
            action="delete",
            resource="user",
            resource_id="u-123",
            user_id="admin",
        )
        with caplog.at_level(logging.INFO):
            await writer.write(entry)

        assert "t-test" in caplog.text
        assert "delete" in caplog.text


class TestManagerIntegration:
    @pytest.mark.asyncio
    async def test_full_lifecycle(self) -> None:
        cfg = _cfg(enable_soft_delete=False)
        store = InMemoryTenantStore()
        m = TenancyManager(cfg, store)
        await m.initialize()
        m.isolation_provider.initialize_tenant = AsyncMock()  # type: ignore[method-assign]
        m.isolation_provider.destroy_tenant = AsyncMock()  # type: ignore[method-assign]

        tenant = await m.register_tenant("lifecycle-tenant", "Lifecycle")
        assert tenant.is_active()

        suspended = await m.suspend_tenant(tenant.id)
        assert suspended.is_suspended()

        activated = await m.activate_tenant(tenant.id)
        assert activated.is_active()

        await m.delete_tenant(tenant.id, destroy_data=False)
        with pytest.raises(TenantNotFoundError):
            await store.get_by_id(tenant.id)

        await m.close()
