"""Tests for fastapi_tenancy.core.exceptions — full hierarchy coverage."""

from __future__ import annotations

import pytest

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


class TestTenancyError:
    def test_basic(self) -> None:
        e = TenancyError("something broke")
        assert e.message == "something broke"
        assert e.details == {}
        assert str(e) == "something broke"

    def test_with_details(self) -> None:
        e = TenancyError("broke", details={"key": "val"})
        assert e.details == {"key": "val"}
        assert "details=" in str(e)

    def test_repr(self) -> None:
        e = TenancyError("msg")
        assert "TenancyError" in repr(e)
        assert "msg" in repr(e)

    def test_is_exception(self) -> None:
        e = TenancyError("msg")
        assert isinstance(e, Exception)

    def test_all_subclasses_inherit(self) -> None:
        subclasses = [
            TenantNotFoundError,
            TenantResolutionError,
            TenantInactiveError,
            IsolationError,
            ConfigurationError,
            MigrationError,
            RateLimitExceededError,
            TenantDataLeakageError,
            TenantQuotaExceededError,
            DatabaseConnectionError,
        ]
        for cls in subclasses:
            assert issubclass(cls, TenancyError)


class TestTenantNotFoundError:
    def test_with_identifier(self) -> None:
        e = TenantNotFoundError(identifier="acme-corp")
        assert "acme-corp" in str(e)
        assert e.identifier == "acme-corp"

    def test_without_identifier(self) -> None:
        e = TenantNotFoundError()
        assert "Tenant not found" in str(e)
        assert e.identifier is None

    def test_with_details(self) -> None:
        e = TenantNotFoundError(identifier="x", details={"hint": "check slug"})
        assert e.details == {"hint": "check slug"}

    def test_raiseable(self) -> None:
        with pytest.raises(TenantNotFoundError) as exc_info:
            raise TenantNotFoundError(identifier="missing")
        assert exc_info.value.identifier == "missing"

    def test_catchable_as_tenancy_error(self) -> None:
        with pytest.raises(TenancyError):
            raise TenantNotFoundError(identifier="x")


class TestTenantResolutionError:
    def test_basic(self) -> None:
        e = TenantResolutionError(reason="header missing", strategy="header")
        assert e.reason == "header missing"
        assert e.strategy == "header"
        assert "header" in str(e)
        assert "header missing" in str(e)

    def test_without_strategy(self) -> None:
        e = TenantResolutionError(reason="unknown")
        assert e.strategy is None
        assert "unknown" in str(e)

    def test_with_details(self) -> None:
        e = TenantResolutionError(
            reason="bad jwt", strategy="jwt", details={"jwt_error": "ExpiredSignature"}
        )
        assert e.details["jwt_error"] == "ExpiredSignature"


class TestTenantInactiveError:
    def test_basic(self) -> None:
        e = TenantInactiveError(tenant_id="t1", status="suspended")
        assert e.tenant_id == "t1"
        assert e.status == "suspended"
        assert "t1" in str(e)
        assert "suspended" in str(e)


class TestIsolationError:
    def test_with_tenant(self) -> None:
        e = IsolationError(operation="get_session", tenant_id="t1")
        assert e.operation == "get_session"
        assert e.tenant_id == "t1"
        assert "get_session" in str(e)
        assert "t1" in str(e)

    def test_without_tenant(self) -> None:
        e = IsolationError(operation="initialize")
        assert e.tenant_id is None


class TestConfigurationError:
    def test_basic(self) -> None:
        e = ConfigurationError(parameter="jwt_secret", reason="too short")
        assert e.parameter == "jwt_secret"
        assert e.reason == "too short"
        assert "jwt_secret" in str(e)
        assert "too short" in str(e)


class TestMigrationError:
    def test_basic(self) -> None:
        e = MigrationError(tenant_id="t1", operation="upgrade", reason="revision not found")
        assert e.tenant_id == "t1"
        assert e.operation == "upgrade"
        assert e.reason == "revision not found"
        assert "t1" in str(e)
        assert "upgrade" in str(e)


class TestRateLimitExceededError:
    def test_basic(self) -> None:
        e = RateLimitExceededError(tenant_id="t1", limit=100, window_seconds=60)
        assert e.tenant_id == "t1"
        assert e.limit == 100
        assert e.window_seconds == 60
        assert "100" in str(e)
        assert "60" in str(e)


class TestTenantDataLeakageError:
    def test_basic(self) -> None:
        e = TenantDataLeakageError(
            operation="query",
            expected_tenant="t1",
            actual_tenant="t2",
        )
        assert e.operation == "query"
        assert e.expected_tenant == "t1"
        assert e.actual_tenant == "t2"
        assert "SECURITY" in str(e)
        assert "t1" in str(e)
        assert "t2" in str(e)


class TestTenantQuotaExceededError:
    def test_basic(self) -> None:
        e = TenantQuotaExceededError(
            tenant_id="t1",
            quota_type="users",
            current=51,
            limit=50,
        )
        assert e.tenant_id == "t1"
        assert e.quota_type == "users"
        assert e.current == 51
        assert e.limit == 50
        assert "users" in str(e)


class TestDatabaseConnectionError:
    def test_basic(self) -> None:
        e = DatabaseConnectionError(tenant_id="t1", reason="connection refused")
        assert e.tenant_id == "t1"
        assert e.reason == "connection refused"
        assert "t1" in str(e)
        assert "connection refused" in str(e)
