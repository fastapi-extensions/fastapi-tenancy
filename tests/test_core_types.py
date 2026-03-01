"""Tests for fastapi_tenancy.core.types — domain models, enums, protocols."""

from __future__ import annotations

import pytest
from datetime import UTC, datetime

from fastapi_tenancy.core.types import (
    AuditLog,
    IsolationStrategy,
    ResolutionStrategy,
    Tenant,
    TenantConfig,
    TenantMetrics,
    TenantStatus,
    TenantResolver,
    IsolationProvider,
)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class TestTenantStatus:
    def test_values(self):
        assert TenantStatus.ACTIVE == "active"
        assert TenantStatus.SUSPENDED == "suspended"
        assert TenantStatus.DELETED == "deleted"
        assert TenantStatus.PROVISIONING == "provisioning"

    def test_is_str(self):
        assert isinstance(TenantStatus.ACTIVE, str)

    def test_all_members(self):
        members = {s.value for s in TenantStatus}
        assert members == {"active", "suspended", "deleted", "provisioning"}


class TestIsolationStrategy:
    def test_values(self):
        assert IsolationStrategy.SCHEMA == "schema"
        assert IsolationStrategy.DATABASE == "database"
        assert IsolationStrategy.RLS == "rls"
        assert IsolationStrategy.HYBRID == "hybrid"

    def test_is_str(self):
        assert isinstance(IsolationStrategy.SCHEMA, str)


class TestResolutionStrategy:
    def test_values(self):
        assert ResolutionStrategy.HEADER == "header"
        assert ResolutionStrategy.SUBDOMAIN == "subdomain"
        assert ResolutionStrategy.PATH == "path"
        assert ResolutionStrategy.JWT == "jwt"
        assert ResolutionStrategy.CUSTOM == "custom"


# ---------------------------------------------------------------------------
# Tenant model
# ---------------------------------------------------------------------------

class TestTenant:
    def test_minimal_construction(self):
        t = Tenant(id="t1", identifier="acme-corp", name="Acme")
        assert t.id == "t1"
        assert t.identifier == "acme-corp"
        assert t.name == "Acme"
        assert t.status == TenantStatus.ACTIVE
        assert t.metadata == {}
        assert t.isolation_strategy is None
        assert t.database_url is None
        assert t.schema_name is None

    def test_full_construction(self):
        now = datetime.now(UTC)
        t = Tenant(
            id="t2",
            identifier="globex-inc",
            name="Globex",
            status=TenantStatus.SUSPENDED,
            isolation_strategy=IsolationStrategy.SCHEMA,
            metadata={"plan": "pro"},
            created_at=now,
            updated_at=now,
            database_url="postgresql+asyncpg://user:pass@host/db",
            schema_name="tenant_globex",
        )
        assert t.status == TenantStatus.SUSPENDED
        assert t.isolation_strategy == IsolationStrategy.SCHEMA
        assert t.metadata == {"plan": "pro"}
        assert t.database_url == "postgresql+asyncpg://user:pass@host/db"
        assert t.schema_name == "tenant_globex"

    def test_frozen(self):
        t = Tenant(id="t1", identifier="acme-corp", name="Acme")
        with pytest.raises(Exception):  # ValidationError or FrozenInstanceError
            t.name = "Changed"  # type: ignore[misc]

    def test_equality_based_on_id(self):
        t1 = Tenant(id="same", identifier="acme", name="Acme")
        t2 = Tenant(id="same", identifier="other", name="Other")
        assert t1 == t2

    def test_inequality_different_id(self):
        t1 = Tenant(id="id-1", identifier="acme", name="Acme")
        t2 = Tenant(id="id-2", identifier="acme", name="Acme")
        assert t1 != t2

    def test_equality_non_tenant(self):
        t = Tenant(id="t1", identifier="acme", name="Acme")
        assert t.__eq__("not a tenant") is NotImplemented

    def test_hash_based_on_id(self):
        t1 = Tenant(id="same", identifier="acme", name="Acme")
        t2 = Tenant(id="same", identifier="other", name="Other")
        assert hash(t1) == hash(t2)
        s = {t1, t2}
        assert len(s) == 1

    def test_is_active(self):
        t = Tenant(id="t1", identifier="acme", name="Acme", status=TenantStatus.ACTIVE)
        assert t.is_active() is True
        assert t.is_suspended() is False
        assert t.is_deleted() is False
        assert t.is_provisioning() is False

    def test_is_suspended(self):
        t = Tenant(id="t1", identifier="acme", name="Acme", status=TenantStatus.SUSPENDED)
        assert t.is_active() is False
        assert t.is_suspended() is True

    def test_is_deleted(self):
        t = Tenant(id="t1", identifier="acme", name="Acme", status=TenantStatus.DELETED)
        assert t.is_deleted() is True

    def test_is_provisioning(self):
        t = Tenant(id="t1", identifier="acme", name="Acme", status=TenantStatus.PROVISIONING)
        assert t.is_provisioning() is True

    def test_model_dump_safe_masks_database_url(self):
        t = Tenant(
            id="t1",
            identifier="acme",
            name="Acme",
            database_url="postgresql+asyncpg://user:secret@host/db",
        )
        safe = t.model_dump_safe()
        assert safe["database_url"] == "***masked***"
        assert safe["id"] == "t1"
        assert safe["name"] == "Acme"

    def test_model_dump_safe_no_database_url(self):
        t = Tenant(id="t1", identifier="acme", name="Acme")
        safe = t.model_dump_safe()
        assert safe.get("database_url") is None

    def test_repr(self):
        t = Tenant(id="t1", identifier="acme", name="Acme")
        r = repr(t)
        assert "t1" in r
        assert "acme" in r
        assert "active" in r

    def test_model_copy(self):
        t = Tenant(id="t1", identifier="acme", name="Acme")
        updated = t.model_copy(update={"status": TenantStatus.SUSPENDED})
        assert updated.status == TenantStatus.SUSPENDED
        assert t.status == TenantStatus.ACTIVE  # original unchanged

    def test_id_min_length(self):
        with pytest.raises(Exception):
            Tenant(id="", identifier="acme", name="Acme")

    def test_identifier_min_length(self):
        with pytest.raises(Exception):
            Tenant(id="t1", identifier="ab", name="Acme")

    def test_name_min_length(self):
        with pytest.raises(Exception):
            Tenant(id="t1", identifier="acme", name="")


# ---------------------------------------------------------------------------
# TenantConfig model
# ---------------------------------------------------------------------------

class TestTenantConfig:
    def test_defaults(self):
        c = TenantConfig()
        assert c.max_users is None
        assert c.max_storage_gb is None
        assert c.features_enabled == []
        assert c.rate_limit_per_minute == 100
        assert c.custom_settings == {}

    def test_custom_values(self):
        c = TenantConfig(
            max_users=50,
            max_storage_gb=100,
            features_enabled=["analytics", "exports"],
            rate_limit_per_minute=500,
            custom_settings={"theme": "dark"},
        )
        assert c.max_users == 50
        assert c.features_enabled == ["analytics", "exports"]
        assert c.rate_limit_per_minute == 500

    def test_frozen(self):
        c = TenantConfig()
        with pytest.raises(Exception):
            c.max_users = 10  # type: ignore[misc]

    def test_rate_limit_bounds(self):
        with pytest.raises(Exception):
            TenantConfig(rate_limit_per_minute=0)
        with pytest.raises(Exception):
            TenantConfig(rate_limit_per_minute=99_999)


# ---------------------------------------------------------------------------
# AuditLog model
# ---------------------------------------------------------------------------

class TestAuditLog:
    def test_minimal_construction(self):
        log = AuditLog(tenant_id="t1", action="create", resource="user")
        assert log.tenant_id == "t1"
        assert log.action == "create"
        assert log.resource == "user"
        assert log.user_id is None
        assert log.resource_id is None
        assert log.ip_address is None
        assert log.user_agent is None
        assert log.metadata == {}

    def test_full_construction(self):
        now = datetime.now(UTC)
        log = AuditLog(
            tenant_id="t1",
            user_id="u42",
            action="delete",
            resource="order",
            resource_id="ord-999",
            metadata={"reason": "cancelled"},
            ip_address="1.2.3.4",
            user_agent="pytest/1.0",
            timestamp=now,
        )
        assert log.user_id == "u42"
        assert log.resource_id == "ord-999"
        assert log.ip_address == "1.2.3.4"
        assert log.timestamp == now

    def test_frozen(self):
        log = AuditLog(tenant_id="t1", action="create", resource="user")
        with pytest.raises(Exception):
            log.action = "delete"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TenantMetrics model
# ---------------------------------------------------------------------------

class TestTenantMetrics:
    def test_defaults(self):
        m = TenantMetrics(tenant_id="t1")
        assert m.requests_count == 0
        assert m.storage_bytes == 0
        assert m.users_count == 0
        assert m.api_calls_today == 0
        assert m.last_activity is None

    def test_non_negative_constraints(self):
        with pytest.raises(Exception):
            TenantMetrics(tenant_id="t1", requests_count=-1)
        with pytest.raises(Exception):
            TenantMetrics(tenant_id="t1", storage_bytes=-1)


# ---------------------------------------------------------------------------
# Structural protocols
# ---------------------------------------------------------------------------

class TestTenantResolverProtocol:
    def test_duck_typing_satisfies_protocol(self):
        """Any object with async resolve() satisfies TenantResolver protocol."""
        class MyResolver:
            async def resolve(self, request):
                pass  # pragma: no cover

        assert isinstance(MyResolver(), TenantResolver)

    def test_object_without_resolve_does_not_satisfy(self):
        class NotAResolver:
            pass

        assert not isinstance(NotAResolver(), TenantResolver)


class TestIsolationProviderProtocol:
    def test_duck_typing_satisfies_protocol(self):
        class MyProvider:
            def get_session(self, tenant): pass  # pragma: no cover
            async def apply_filters(self, query, tenant): pass  # pragma: no cover
            async def initialize_tenant(self, tenant): pass  # pragma: no cover
            async def destroy_tenant(self, tenant, **kw): pass  # pragma: no cover

        assert isinstance(MyProvider(), IsolationProvider)


# ---------------------------------------------------------------------------
# Lazy __getattr__ re-exports from core.types
# ---------------------------------------------------------------------------

class TestLazyReexports:
    def test_base_tenant_resolver_importable(self):
        from fastapi_tenancy.core.types import BaseTenantResolver
        assert BaseTenantResolver is not None

    def test_base_isolation_provider_importable(self):
        from fastapi_tenancy.core.types import BaseIsolationProvider
        assert BaseIsolationProvider is not None

    def test_unknown_attribute_raises(self):
        import fastapi_tenancy.core.types as types_mod
        with pytest.raises(AttributeError):
            _ = types_mod.NonExistentClass
