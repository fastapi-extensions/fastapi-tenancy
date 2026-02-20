"""Unit tests — fastapi_tenancy.core.types

Coverage target: 100 %

Verified:
* TenantStatus / IsolationStrategy / ResolutionStrategy enum values, coercion, JSON
* Tenant — construction, frozen, is_active() for all statuses, model_dump,
  model_dump_safe, timestamps UTC, copy-with-update, equality
* TenantConfig — defaults, custom values, frozen
* AuditLog — construction, optional details
* TenantMetrics — construction, optional fields
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from fastapi_tenancy.core.types import (
    AuditLog,
    IsolationStrategy,
    ResolutionStrategy,
    Tenant,
    TenantConfig,
    TenantMetrics,
    TenantStatus,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2024, 1, 1, tzinfo=UTC)


def _tenant(**kw: Any) -> Tenant:
    defaults: dict[str, Any] = {
        "id": "t-001",
        "identifier": "acme-corp",
        "name": "Acme Corp",
        "status": TenantStatus.ACTIVE,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(kw)
    return Tenant(**defaults)


# ──────────────────────────── TenantStatus ───────────────────────────────────


class TestTenantStatus:
    def test_active_value(self):
        assert TenantStatus.ACTIVE == "active"

    def test_suspended_value(self):
        assert TenantStatus.SUSPENDED == "suspended"

    def test_deleted_value(self):
        assert TenantStatus.DELETED == "deleted"

    def test_provisioning_value(self):
        assert TenantStatus.PROVISIONING == "provisioning"

    def test_str_coercion(self):
        assert str(TenantStatus.ACTIVE) == "active"

    def test_all_four_members(self):
        assert {s.value for s in TenantStatus} == {"active", "suspended", "deleted", "provisioning"}

    def test_from_string(self):
        assert TenantStatus("active") is TenantStatus.ACTIVE

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            TenantStatus("unknown_status")

    def test_json_serializable(self):
        data = json.dumps({"status": TenantStatus.ACTIVE})
        assert '"active"' in data


# ──────────────────────────── IsolationStrategy ───────────────────────────────


class TestIsolationStrategy:
    def test_values(self):
        assert IsolationStrategy.SCHEMA == "schema"
        assert IsolationStrategy.DATABASE == "database"
        assert IsolationStrategy.RLS == "rls"
        assert IsolationStrategy.HYBRID == "hybrid"

    def test_all_four_members(self):
        assert {s.value for s in IsolationStrategy} == {"schema", "database", "rls", "hybrid"}

    def test_json_serializable(self):
        data = json.dumps({"iso": IsolationStrategy.SCHEMA})
        assert '"schema"' in data


# ──────────────────────────── ResolutionStrategy ─────────────────────────────


class TestResolutionStrategy:
    def test_values(self):
        assert ResolutionStrategy.HEADER == "header"
        assert ResolutionStrategy.SUBDOMAIN == "subdomain"
        assert ResolutionStrategy.PATH == "path"
        assert ResolutionStrategy.JWT == "jwt"
        assert ResolutionStrategy.CUSTOM == "custom"

    def test_all_five_members(self):
        assert {s.value for s in ResolutionStrategy} == {
            "header", "subdomain", "path", "jwt", "custom"
        }


# ─────────────────────────────── Tenant ──────────────────────────────────────


class TestTenant:
    def test_basic_construction(self):
        t = _tenant()
        assert t.id == "t-001"
        assert t.identifier == "acme-corp"
        assert t.name == "Acme Corp"
        assert t.status == TenantStatus.ACTIVE

    def test_frozen_id_raises(self):
        t = _tenant()
        with pytest.raises(Exception):
            t.id = "new-id"  # type: ignore[misc]

    def test_frozen_status_raises(self):
        t = _tenant()
        with pytest.raises(Exception):
            t.status = TenantStatus.DELETED  # type: ignore[misc]

    def test_metadata_default_empty(self):
        assert _tenant().metadata == {}

    def test_metadata_populated(self):
        t = _tenant(metadata={"plan": "pro", "max_users": 50})
        assert t.metadata["plan"] == "pro"

    def test_optional_isolation_none_by_default(self):
        assert _tenant().isolation_strategy is None

    def test_optional_database_url_none(self):
        assert _tenant().database_url is None

    def test_optional_schema_name_none(self):
        assert _tenant().schema_name is None

    def test_isolation_strategy_set(self):
        t = _tenant(isolation_strategy=IsolationStrategy.SCHEMA)
        assert t.isolation_strategy == IsolationStrategy.SCHEMA

    def test_is_active_true(self):
        assert _tenant(status=TenantStatus.ACTIVE).is_active() is True

    def test_is_active_false_suspended(self):
        assert _tenant(status=TenantStatus.SUSPENDED).is_active() is False

    def test_is_active_false_deleted(self):
        assert _tenant(status=TenantStatus.DELETED).is_active() is False

    def test_is_active_false_provisioning(self):
        assert _tenant(status=TenantStatus.PROVISIONING).is_active() is False

    def test_model_dump_contains_fields(self):
        d = _tenant().model_dump()
        assert d["id"] == "t-001"
        assert d["identifier"] == "acme-corp"

    def test_model_dump_safe_masks_database_url(self):
        t = _tenant(database_url="postgresql://user:secret@host/db")
        safe = t.model_dump_safe()
        assert "secret" not in str(safe.get("database_url", ""))

    def test_model_dump_safe_none_database_url(self):
        t = _tenant()
        safe = t.model_dump_safe()
        assert safe.get("database_url") is None

    def test_created_at_utc_aware(self):
        t = _tenant()
        assert t.created_at.tzinfo is not None

    def test_updated_at_utc_aware(self):
        t = _tenant()
        assert t.updated_at.tzinfo is not None

    def test_model_copy_with_status(self):
        t = _tenant(status=TenantStatus.ACTIVE)
        updated = t.model_copy(update={"status": TenantStatus.SUSPENDED})
        assert updated.status == TenantStatus.SUSPENDED
        assert updated.id == t.id  # unchanged

    def test_equality_same(self):
        t1 = _tenant()
        t2 = _tenant()
        assert t1 == t2

    def test_equality_different_id(self):
        t1 = _tenant(id="t-001")
        t2 = _tenant(id="t-002")
        assert t1 != t2

    def test_database_url_and_schema_set(self):
        t = _tenant(
            database_url="postgresql://host/db",
            schema_name="tenant_acme",
        )
        assert t.database_url == "postgresql://host/db"
        assert t.schema_name == "tenant_acme"


# ────────────────────────── TenantConfig ─────────────────────────────────────


class TestTenantConfig:
    def test_defaults(self):
        cfg = TenantConfig()
        assert cfg.max_users is None
        assert cfg.max_storage_gb is None
        assert cfg.features_enabled == []
        assert cfg.rate_limit_per_minute == 100
        assert cfg.custom_settings == {}

    def test_custom_max_users(self):
        cfg = TenantConfig(max_users=50)
        assert cfg.max_users == 50

    def test_custom_features(self):
        cfg = TenantConfig(features_enabled=["sso", "audit"])
        assert "sso" in cfg.features_enabled

    def test_custom_rate_limit(self):
        cfg = TenantConfig(rate_limit_per_minute=200)
        assert cfg.rate_limit_per_minute == 200

    def test_custom_settings(self):
        cfg = TenantConfig(custom_settings={"theme": "dark"})
        assert cfg.custom_settings["theme"] == "dark"

    def test_frozen(self):
        cfg = TenantConfig()
        with pytest.raises(Exception):
            cfg.max_users = 99  # type: ignore[misc]


# ────────────────────────────── AuditLog ─────────────────────────────────────


class TestAuditLog:
    def test_construction(self):
        log = AuditLog(tenant_id="t-001", action="login", actor="user@acme.com", timestamp=_NOW)
        assert log.tenant_id == "t-001"
        assert log.action == "login"
        assert log.actor == "user@acme.com"
        assert log.timestamp == _NOW

    def test_details_default_empty(self):
        log = AuditLog(tenant_id="t", action="a", actor="u", timestamp=_NOW)
        assert log.details == {}

    def test_details_populated(self):
        log = AuditLog(
            tenant_id="t", action="a", actor="u", timestamp=_NOW,
            details={"ip": "1.2.3.4", "user_agent": "curl/7"},
        )
        assert log.details["ip"] == "1.2.3.4"


# ──────────────────────────── TenantMetrics ──────────────────────────────────


class TestTenantMetrics:
    def test_construction(self):
        m = TenantMetrics(
            tenant_id="t-001", request_count=500, error_count=5, last_seen=_NOW
        )
        assert m.tenant_id == "t-001"
        assert m.request_count == 500
        assert m.error_count == 5
        assert m.last_seen == _NOW

    def test_optional_active_users_none(self):
        m = TenantMetrics(tenant_id="t", request_count=0, error_count=0, last_seen=_NOW)
        assert m.active_users is None

    def test_optional_storage_used_none(self):
        m = TenantMetrics(tenant_id="t", request_count=0, error_count=0, last_seen=_NOW)
        assert m.storage_used_gb is None

    def test_active_users_set(self):
        m = TenantMetrics(
            tenant_id="t", request_count=100, error_count=1,
            last_seen=_NOW, active_users=42
        )
        assert m.active_users == 42

    def test_storage_used_set(self):
        m = TenantMetrics(
            tenant_id="t", request_count=100, error_count=1,
            last_seen=_NOW, storage_used_gb=3.5
        )
        assert m.storage_used_gb == 3.5
