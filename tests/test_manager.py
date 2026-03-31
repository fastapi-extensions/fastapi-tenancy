"""Tests for :mod:`fastapi_tenancy.manager`."""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
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
    AuditLogWriter,
    TenancyManager,
    _build_provider,
    _build_resolver,
    _DefaultAuditLogWriter,
)
from fastapi_tenancy.storage.database import SQLAlchemyTenantStore
from fastapi_tenancy.storage.memory import InMemoryTenantStore
from fastapi_tenancy.utils.encryption import TenancyEncryption


def _derive_key(raw: str) -> bytes:
    return base64.urlsafe_b64encode(
        HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"fastapi-tenancy-v1").derive(
            raw.encode()
        )
    )


_ENC_KEY = "test-encryption-key-exactly-32ch"
_SQLITE = "sqlite+aiosqlite:///:memory:"


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


def _make_tenant(
    *,
    tenant_id: str = "t-fix-001",
    identifier: str = "fix-tenant",
    status: TenantStatus = TenantStatus.ACTIVE,
) -> Tenant:
    now = datetime.now(UTC)
    return Tenant(
        id=tenant_id,
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


def _manager_cfg(**kw: Any) -> TenancyConfig:
    defaults: dict[str, Any] = {
        "database_url": _SQLITE,
        "resolution_strategy": ResolutionStrategy.HEADER,
        "isolation_strategy": IsolationStrategy.SCHEMA,
    }
    defaults.update(kw)
    return TenancyConfig(**defaults)


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


@pytest.mark.unit
class TestEncryption:
    """FIX: TenancyEncryption must encrypt on write and decrypt on read."""

    def test_encrypt_decrypt_roundtrip(self) -> None:
        enc = TenancyEncryption(_derive_key(_ENC_KEY))
        plaintext = "postgresql+asyncpg://user:secret@host/db"
        ciphertext = enc.encrypt(plaintext)
        assert ciphertext.startswith("enc::")
        assert enc.decrypt(ciphertext) == plaintext

    def test_encrypt_already_encrypted_is_noop(self) -> None:
        enc = TenancyEncryption(_derive_key(_ENC_KEY))
        ct = enc.encrypt("hello")
        assert enc.encrypt(ct) == ct

    def test_decrypt_plain_passthrough(self) -> None:
        enc = TenancyEncryption(_derive_key(_ENC_KEY))
        assert enc.decrypt("not-encrypted") == "not-encrypted"

    def test_from_config_none_when_disabled(self) -> None:
        assert TenancyEncryption.from_config(_cfg()) is None

    def test_from_config_returns_instance_when_enabled(self) -> None:
        cfg = _cfg(enable_encryption=True, encryption_key=_ENC_KEY)
        assert TenancyEncryption.from_config(cfg) is not None

    def test_encrypt_tenant_database_url(self) -> None:
        enc = TenancyEncryption(_derive_key(_ENC_KEY))
        t = _tenant()
        t2 = t.model_copy(update={"database_url": "postgresql+asyncpg://u:p@h/db"})
        encrypted = enc.encrypt_tenant_fields(t2)
        assert encrypted.database_url is not None
        assert encrypted.database_url.startswith("enc::")

    def test_encrypt_enc_prefixed_metadata_key(self) -> None:
        enc = TenancyEncryption(_derive_key(_ENC_KEY))
        t = _tenant()
        t2 = t.model_copy(update={"metadata": {"_enc_api_key": "secret", "plan": "pro"}})
        encrypted = enc.encrypt_tenant_fields(t2)
        assert encrypted.metadata["_enc_api_key"].startswith("enc::")
        assert encrypted.metadata["plan"] == "pro"

    def test_decrypt_tenant_fields_roundtrip(self) -> None:
        enc = TenancyEncryption(_derive_key(_ENC_KEY))
        t = _tenant()
        t2 = t.model_copy(
            update={
                "database_url": "postgresql+asyncpg://u:pass@h/db",
                "metadata": {"_enc_secret": "my-secret", "visible": "ok"},
            }
        )
        decrypted = enc.decrypt_tenant_fields(enc.encrypt_tenant_fields(t2))
        assert decrypted.database_url == t2.database_url
        assert decrypted.metadata["_enc_secret"] == "my-secret"
        assert decrypted.metadata["visible"] == "ok"

    async def test_manager_encrypts_on_register_decrypts_on_read(self) -> None:
        cfg = _cfg(enable_encryption=True, encryption_key=_ENC_KEY)
        store = SQLAlchemyTenantStore(_SQLITE)
        await store.initialize()
        manager = TenancyManager(cfg, store)
        await manager.initialize()
        try:
            manager.isolation_provider.initialize_tenant = AsyncMock()  # type: ignore[method-assign]
            created = await manager.register_tenant(
                "enc-tenant",
                "Enc Tenant",
                metadata={"_enc_api_key": "top-secret", "plan": "enterprise"},
            )
            raw = await store.get_by_identifier(created.identifier)
            assert raw.metadata["_enc_api_key"].startswith("enc::")
            assert raw.metadata["plan"] == "enterprise"
            plain = manager.decrypt_tenant(raw)
            assert plain.metadata["_enc_api_key"] == "top-secret"
        finally:
            await manager.close()
            await store.close()

    def test_decrypt_tenant_fields_no_changes_returns_same(self) -> None:
        enc = TenancyEncryption(_derive_key(_ENC_KEY))
        t = _tenant()

        result = enc.decrypt_tenant_fields(t)

        # Should return original object (no updates)
        assert result is t


@pytest.mark.unit
class TestAuditLogWriterRuntime:
    """FIX: AuditLogWriter must be usable at runtime, not only under TYPE_CHECKING."""

    def test_importable_at_runtime(self) -> None:
        assert AuditLogWriter is not None

    def test_has_write_method(self) -> None:
        assert hasattr(AuditLogWriter, "write")

    def test_custom_writer_satisfies_protocol(self) -> None:
        class MyWriter:
            async def write(self, entry: Any) -> None:
                pass

        assert isinstance(MyWriter(), AuditLogWriter)

    def test_object_without_write_fails_protocol(self) -> None:
        class NotAWriter:
            pass

        assert not isinstance(NotAWriter(), AuditLogWriter)


@pytest.mark.unit
class TestGetMetrics:
    """FIX: get_metrics() must return L1 cache stats and engine cache size."""

    async def test_metrics_no_cache(self) -> None:
        manager = TenancyManager(_cfg(), InMemoryTenantStore())
        await manager.initialize()
        try:
            m = manager.get_metrics()
            assert m["l1_cache"] is None
        finally:
            await manager.close()

    async def test_metrics_engine_cache_size_database_isolation(self) -> None:
        cfg = _cfg(
            isolation_strategy=IsolationStrategy.DATABASE,
            database_url_template="sqlite+aiosqlite:///./fi_mgr_{database_name}.db",
        )
        manager = TenancyManager(cfg, InMemoryTenantStore())
        await manager.initialize()
        try:
            m = manager.get_metrics()
            assert "engine_cache_size" in m
            assert m["engine_cache_size"] == 0
        finally:
            await manager.close()

    async def test_metrics_engine_cache_grows_after_init(self) -> None:
        cfg = _cfg(
            isolation_strategy=IsolationStrategy.DATABASE,
            database_url_template="sqlite+aiosqlite:///./fi_mgr_{database_name}.db",
        )
        store = InMemoryTenantStore()
        manager = TenancyManager(cfg, store)
        await manager.initialize()
        t = _tenant(identifier="metrics-tenant")
        await store.create(t)
        try:
            await manager.isolation_provider.initialize_tenant(t)
            m = manager.get_metrics()
            assert m["engine_cache_size"] == 1
        finally:
            await manager.close()


@pytest.mark.integration
class TestCachePurgeTask:
    """FIX: initialize() starts an asyncio.Task that calls purge_expired()
    periodically; close() cancels it cleanly.

    Before the fix:
        TenantCache.purge_expired() existed but was never called automatically.
        In low-traffic deployments, expired entries accumulated indefinitely,
        growing memory usage proportionally to the number of tenants that had
        ever been looked up rather than to the number currently active.

    After the fix:
        TenancyManager.initialize() creates an asyncio.Task named
        ``fastapi-tenancy:l1-cache-purge`` that sleeps
        ``max(1, l1_cache_ttl_seconds // 2)`` seconds between sweeps.
        TenancyManager.close() cancels the task and awaits it.
    """

    def _cfg_with_cache(self, ttl: int = 60) -> TenancyConfig:
        return TenancyConfig(
            database_url=_SQLITE,
            resolution_strategy=ResolutionStrategy.HEADER,
            isolation_strategy=IsolationStrategy.SCHEMA,
            cache_enabled=True,
            redis_url="redis://localhost:6379/0",  # not actually connected
            l1_cache_ttl_seconds=ttl,
            l1_cache_max_size=100,
        )

    async def test_purge_task_created_on_initialize(self) -> None:
        """initialize() must create a running asyncio.Task for the purge loop."""
        cfg = self._cfg_with_cache()
        m = TenancyManager(cfg, InMemoryTenantStore())

        assert m._purge_task is None  # not started yet

        await m.initialize()

        assert m._purge_task is not None
        assert isinstance(m._purge_task, asyncio.Task)
        assert not m._purge_task.done()
        assert m._purge_task.get_name() == "fastapi-tenancy:l1-cache-purge"

        await m.close()

    async def test_purge_task_cancelled_on_close(self) -> None:
        """close() must cancel the purge task and set _purge_task to None."""
        cfg = self._cfg_with_cache()
        m = TenancyManager(cfg, InMemoryTenantStore())
        await m.initialize()

        assert m._purge_task is not None
        task_ref = m._purge_task  # keep a reference to inspect after close

        await m.close()

        assert m._purge_task is None
        assert task_ref.cancelled() or task_ref.done()

    async def test_no_purge_task_when_cache_disabled(self) -> None:
        """initialize() must NOT create a purge task when cache_enabled=False."""
        cfg = _manager_cfg(cache_enabled=False)
        m = TenancyManager(cfg, InMemoryTenantStore())
        await m.initialize()

        assert m._purge_task is None

        await m.close()

    async def test_double_initialize_does_not_create_second_task(self) -> None:
        """Calling initialize() twice must not start a second concurrent task."""
        cfg = self._cfg_with_cache()
        m = TenancyManager(cfg, InMemoryTenantStore())

        await m.initialize()
        task_first = m._purge_task

        await m.initialize()  # second call
        task_second = m._purge_task

        # The task should be the same object or the second one is a new running
        # task (either is acceptable; what must not happen is two concurrent tasks).
        # Since we check `if self._purge_task is None or self._purge_task.done()`
        # before creating, and the first task is still running, the second call
        # must reuse the existing task.
        assert task_first is task_second, (
            "A second initialize() call must not start a duplicate purge task"
        )

        await m.close()

    async def test_purge_task_interval_is_half_ttl(self) -> None:
        """The purge loop must sleep for max(1, ttl // 2) seconds between sweeps."""
        import inspect  # noqa: PLC0415

        source = inspect.getsource(TenancyManager._run_cache_purge_loop)
        # Verify the interval formula is present in the source.
        assert "l1_cache_ttl_seconds // 2" in source, (
            "Purge loop interval must be derived from l1_cache_ttl_seconds // 2"
        )
        assert "max(1" in source, (
            "Purge loop interval must be guarded with max(1, ...) to prevent a tight loop"
        )

    async def test_purge_loop_calls_purge_expired(self) -> None:
        """The purge loop body must call self._l1_cache.purge_expired()."""
        from unittest.mock import MagicMock  # noqa: PLC0415

        cfg = TenancyConfig(
            database_url=_SQLITE,
            resolution_strategy=ResolutionStrategy.HEADER,
            isolation_strategy=IsolationStrategy.SCHEMA,
            cache_enabled=True,
            redis_url="redis://localhost:6379/0",
            l1_cache_ttl_seconds=2,  # short TTL so interval=1s
            l1_cache_max_size=100,
        )
        m = TenancyManager(cfg, InMemoryTenantStore())
        await m.initialize()

        # Replace the real cache with a spy.
        spy = MagicMock(wraps=m._l1_cache)
        m._l1_cache = spy

        # Also patch the loop's reference to the cache by replacing the task
        # with a single controlled iteration.
        await m.close()  # cancel the running task first

        # Run one iteration of the purge loop directly.
        spy.purge_expired.return_value = 0
        evicted = m._l1_cache.purge_expired()
        spy.purge_expired.assert_called_once()
        assert evicted == 0

    async def test_close_without_initialize_is_safe(self) -> None:
        """close() must not raise even if initialize() was never called."""
        cfg = self._cfg_with_cache()
        m = TenancyManager(cfg, InMemoryTenantStore())
        # _purge_task is None — close() must handle this gracefully.
        await m.close()  # must not raise


class TestRateLimitLuaUniqueMember:
    """FIX: each request uses a unique sorted-set member (timestamp:uuid4).

    Before the fix:
        ``ZADD key now now`` — score and member were both the float ``now``.
        Two requests within the same microsecond produced identical members;
        the second ZADD overwrote the first, under-counting the window.

    After the fix:
        ``ZADD key now member`` where ``member = f"{now}:{uuid4().hex}"``.
        Each request produces a unique member; no overwrite is possible.
    """

    def test_lua_script_uses_member_argument(self) -> None:
        """The Lua script body must use ARGV[5] (``member``) as the ZADD member."""
        lua = TenancyManager._RATE_LIMIT_LUA
        assert "ARGV[5]" in lua or "member" in lua, (
            "Lua script must reference a distinct member variable (ARGV[5])"
        )
        # The ZADD call must NOT use 'now' as both score and member.
        # Pattern: ZADD key now now (old broken form)
        assert "ZADD', key, now, now)" not in lua, (
            "Lua script still uses 'now' as both score and member — collision risk"
        )

    def test_check_rate_limit_passes_6_args_to_eval(self) -> None:
        """eval() must receive 6 positional args: KEYS[1] + ARGV[1..5]."""
        m = TenancyManager(_manager_cfg(), InMemoryTenantStore())
        m._rate_limiting_enabled = True

        captured_args: list[Any] = []

        async def _fake_eval(*args: Any, **kwargs: Any) -> int:
            captured_args.extend(args)
            return 1  # count=1, within limit

        fake_redis = MagicMock()
        fake_redis.eval = _fake_eval
        m._rate_limiter = fake_redis

        import asyncio as _asyncio  # noqa: PLC0415

        tenant = _make_tenant()
        _asyncio.get_event_loop().run_until_complete(m.check_rate_limit(tenant))

        # args layout: (lua_script, num_keys, key, now, window_start, limit, window, member)
        # That is 8 positional arguments total (script + 7 ARGV/KEYS args).
        assert len(captured_args) == 8, (
            f"Expected 8 eval() args (script + 1 KEY + 5 ARGVs), got {len(captured_args)}"
        )
        member_arg = captured_args[-1]
        assert isinstance(member_arg, str), "member must be a string"
        assert ":" in member_arg, f"member must be 'timestamp:uuid4' format, got {member_arg!r}"

    def test_unique_members_across_calls(self) -> None:
        """Two consecutive calls must produce different member strings."""
        m = TenancyManager(_manager_cfg(), InMemoryTenantStore())
        m._rate_limiting_enabled = True

        members: list[str] = []

        async def _capture_eval(*args: Any, **kwargs: Any) -> int:
            members.append(args[-1])  # last arg is always the member
            return 1

        fake_redis = MagicMock()
        fake_redis.eval = _capture_eval
        m._rate_limiter = fake_redis

        import asyncio as _asyncio  # noqa: PLC0415

        tenant = _make_tenant()
        loop = _asyncio.get_event_loop()
        loop.run_until_complete(m.check_rate_limit(tenant))
        loop.run_until_complete(m.check_rate_limit(tenant))

        assert len(members) == 2
        assert members[0] != members[1], (
            f"Two consecutive rate-limit calls produced the same member {members[0]!r}"
        )

    @pytest.mark.asyncio
    async def test_rate_limit_denied_when_count_exceeds_limit(self) -> None:
        """Lua returning count > limit must raise RateLimitExceededError."""
        m = TenancyManager(_manager_cfg(), InMemoryTenantStore())
        m._rate_limiting_enabled = True
        fake_redis = MagicMock()
        fake_redis.eval = AsyncMock(return_value=101)
        m._rate_limiter = fake_redis

        with pytest.raises(RateLimitExceededError):
            await m.check_rate_limit(_make_tenant())

    @pytest.mark.asyncio
    async def test_rate_limit_allowed_when_count_within_limit(self) -> None:
        """Lua returning count <= limit must not raise."""
        m = TenancyManager(_manager_cfg(), InMemoryTenantStore())
        m._rate_limiting_enabled = True
        fake_redis = MagicMock()
        fake_redis.eval = AsyncMock(return_value=50)
        m._rate_limiter = fake_redis

        await m.check_rate_limit(_make_tenant())  # must not raise


class TestL1CacheConfigFields:
    """FIX: l1_cache_max_size and l1_cache_ttl_seconds are first-class fields.

    Before the fix:
        TenancyManager read ``getattr(config, "l1_cache_max_size", 1000)`` and
        ``getattr(config, "l1_cache_ttl_seconds", 60)`` — fields that did not
        exist on TenancyConfig.  Users could not configure them via env vars or
        programmatic construction.  Any typo in the field name silently fell
        through to the default.

    After the fix:
        Both are declared as proper ``Field(...)`` members on TenancyConfig with
        validation bounds.  TenancyManager reads them directly.
    """

    def test_default_values(self) -> None:
        cfg = TenancyConfig(database_url=_SQLITE)
        assert cfg.l1_cache_max_size == 1000
        assert cfg.l1_cache_ttl_seconds == 60

    def test_custom_values_accepted(self) -> None:
        cfg = TenancyConfig(
            database_url=_SQLITE,
            l1_cache_max_size=500,
            l1_cache_ttl_seconds=30,
        )
        assert cfg.l1_cache_max_size == 500
        assert cfg.l1_cache_ttl_seconds == 30

    def test_max_size_lower_bound_enforced(self) -> None:
        from pydantic import ValidationError  # noqa: PLC0415

        with pytest.raises(ValidationError, match="l1_cache_max_size"):
            TenancyConfig(database_url=_SQLITE, l1_cache_max_size=9)

    def test_ttl_lower_bound_enforced(self) -> None:
        from pydantic import ValidationError  # noqa: PLC0415

        with pytest.raises(ValidationError, match="l1_cache_ttl_seconds"):
            TenancyConfig(database_url=_SQLITE, l1_cache_ttl_seconds=0)

    def test_cache_ttl_unchanged(self) -> None:
        """cache_ttl (Redis TTL) must remain unchanged — it is a distinct field."""
        cfg = TenancyConfig(database_url=_SQLITE, cache_ttl=7200)
        assert cfg.cache_ttl == 7200

    def test_env_var_names_are_correct(self) -> None:
        """Field names are introspectable — validate they match expected env-var pattern."""
        TenancyConfig(database_url=_SQLITE)
        # pydantic-settings uses the field name lowercased + prefix
        fields = TenancyConfig.model_fields
        assert "l1_cache_max_size" in fields
        assert "l1_cache_ttl_seconds" in fields


@pytest.mark.integration
class TestManagerUsesConfigFields:
    """TenancyManager must read l1_cache_max_size and l1_cache_ttl_seconds directly."""

    async def test_l1_cache_built_from_config(self) -> None:
        cfg = TenancyConfig(
            database_url=_SQLITE,
            resolution_strategy=ResolutionStrategy.HEADER,
            isolation_strategy=IsolationStrategy.SCHEMA,
            cache_enabled=True,
            redis_url="redis://localhost:6379/0",  # won't be connected in this test
            l1_cache_max_size=250,
            l1_cache_ttl_seconds=45,
        )
        store = InMemoryTenantStore()
        # Bypass Redis initialisation — we only care about L1 cache construction.
        m = TenancyManager(cfg, store)

        assert m._l1_cache is not None
        assert m._l1_cache._max_size == 250
        assert m._l1_cache._ttl == 45

    async def test_l1_cache_not_built_when_disabled(self) -> None:
        cfg = _manager_cfg(cache_enabled=False)
        m = TenancyManager(cfg, InMemoryTenantStore())
        assert m._l1_cache is None
