"""Unit tests — fastapi_tenancy.core.exceptions

Coverage target: 100 %

Verified:
* Full exception hierarchy (every class is a TenancyError / Exception)
* Constructor arguments, attribute assignment, defaults
* __str__ with and without details
* __repr__ shape
* Raises from within normal Python code (catch semantics)
* details dict: default empty, None → empty, populated, nested values
* All unique per-class attributes
* Numeric and float quota values
* Security message presence in TenantDataLeakageError
* Hierarchy iteration — catch-all pattern works for every subclass
"""

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

pytestmark = pytest.mark.unit


# ─────────────────────────────── TenancyError ────────────────────────────────


class TestTenancyErrorBase:
    def test_message_stored(self):
        e = TenancyError("base error")
        assert e.message == "base error"

    def test_str_no_details(self):
        e = TenancyError("plain")
        assert str(e) == "plain"

    def test_str_with_details_includes_them(self):
        e = TenancyError("with details", details={"code": 42})
        s = str(e)
        assert "with details" in s
        assert "details=" in s

    def test_details_none_becomes_empty_dict(self):
        e = TenancyError("msg", details=None)
        assert e.details == {}

    def test_details_populated(self):
        e = TenancyError("msg", details={"a": 1, "b": "x"})
        assert e.details["a"] == 1
        assert e.details["b"] == "x"

    def test_details_default_is_empty(self):
        e = TenancyError("msg")
        assert e.details == {}

    def test_repr_class_name(self):
        e = TenancyError("msg")
        r = repr(e)
        assert "TenancyError" in r
        assert "msg" in r

    def test_is_exception(self):
        assert isinstance(TenancyError("x"), Exception)

    def test_can_raise_and_catch(self):
        with pytest.raises(TenancyError, match="boom"):
            raise TenancyError("boom")

    def test_details_nested_dict(self):
        e = TenancyError("msg", details={"nested": {"key": "val"}})
        assert e.details["nested"]["key"] == "val"


# ─────────────────────────── TenantNotFoundError ─────────────────────────────


class TestTenantNotFoundError:
    def test_with_identifier(self):
        e = TenantNotFoundError(identifier="acme-corp")
        assert e.identifier == "acme-corp"
        assert "acme-corp" in e.message

    def test_message_format_with_identifier(self):
        e = TenantNotFoundError(identifier="my-org")
        assert e.message == "Tenant not found: 'my-org'"

    def test_without_identifier_defaults_to_none(self):
        e = TenantNotFoundError()
        assert e.identifier is None
        assert "Tenant not found" in e.message

    def test_details_forwarded(self):
        e = TenantNotFoundError("x", details={"strategy": "header"})
        assert e.details["strategy"] == "header"

    def test_is_tenancy_error(self):
        assert isinstance(TenantNotFoundError("x"), TenancyError)

    def test_catch_as_base(self):
        with pytest.raises(TenancyError):
            raise TenantNotFoundError("missing")

    def test_subclass_of_exception(self):
        assert isinstance(TenantNotFoundError(), Exception)


# ─────────────────────────── TenantResolutionError ───────────────────────────


class TestTenantResolutionError:
    def test_reason_only(self):
        e = TenantResolutionError(reason="header missing")
        assert e.reason == "header missing"
        assert e.strategy is None
        assert "header missing" in e.message

    def test_strategy_appended_to_message(self):
        e = TenantResolutionError(reason="bad format", strategy="header")
        assert "header" in e.message
        assert e.strategy == "header"

    def test_details_stored(self):
        e = TenantResolutionError("x", details={"expected_header": "X-Tenant-ID"})
        assert e.details["expected_header"] == "X-Tenant-ID"

    def test_is_tenancy_error(self):
        assert isinstance(TenantResolutionError("x"), TenancyError)

    def test_reason_in_str_output(self):
        e = TenantResolutionError(reason="JWT expired")
        assert "JWT expired" in str(e)


# ─────────────────────────── TenantInactiveError ─────────────────────────────


class TestTenantInactiveError:
    def test_attributes(self):
        e = TenantInactiveError(tenant_id="t-001", status="suspended")
        assert e.tenant_id == "t-001"
        assert e.status == "suspended"

    def test_message_contains_both(self):
        e = TenantInactiveError(tenant_id="t-001", status="suspended")
        assert "t-001" in e.message
        assert "suspended" in e.message

    def test_details_forwarded(self):
        e = TenantInactiveError("t-1", "deleted", details={"identifier": "acme"})
        assert e.details["identifier"] == "acme"

    def test_is_tenancy_error(self):
        assert isinstance(TenantInactiveError("t", "s"), TenancyError)


# ─────────────────────────────── IsolationError ──────────────────────────────


class TestIsolationError:
    def test_operation_only(self):
        e = IsolationError(operation="get_session")
        assert e.operation == "get_session"
        assert e.tenant_id is None

    def test_with_tenant_id(self):
        e = IsolationError(operation="initialize_tenant", tenant_id="t-001")
        assert e.tenant_id == "t-001"
        assert "t-001" in e.message

    def test_operation_in_message(self):
        e = IsolationError(operation="destroy_tenant")
        assert "destroy_tenant" in e.message

    def test_is_tenancy_error(self):
        assert isinstance(IsolationError("op"), TenancyError)

    def test_details_context(self):
        e = IsolationError("op", details={"schema": "tenant_acme", "err": "timeout"})
        assert e.details["schema"] == "tenant_acme"


# ─────────────────────────── ConfigurationError ──────────────────────────────


class TestConfigurationError:
    def test_parameter_and_reason(self):
        e = ConfigurationError(parameter="jwt_secret", reason="must be >= 32 chars")
        assert e.parameter == "jwt_secret"
        assert e.reason == "must be >= 32 chars"

    def test_both_in_message(self):
        e = ConfigurationError(parameter="database_url", reason="missing async driver")
        assert "database_url" in e.message
        assert "missing async driver" in e.message

    def test_fail_fast_pattern(self):
        def bad_init():
            raise ConfigurationError("database_url", "invalid scheme")

        with pytest.raises(ConfigurationError) as exc:
            bad_init()
        assert exc.value.parameter == "database_url"

    def test_is_tenancy_error(self):
        assert isinstance(ConfigurationError("p", "r"), TenancyError)


# ─────────────────────────────── MigrationError ──────────────────────────────


class TestMigrationError:
    def test_all_attributes(self):
        e = MigrationError(tenant_id="t-001", operation="upgrade", reason="table exists")
        assert e.tenant_id == "t-001"
        assert e.operation == "upgrade"
        assert e.reason == "table exists"

    def test_all_in_message(self):
        e = MigrationError(tenant_id="t-001", operation="upgrade", reason="table exists")
        assert "t-001" in e.message
        assert "upgrade" in e.message
        assert "table exists" in e.message

    def test_details(self):
        e = MigrationError("t", "op", "r", details={"revision": "abc123"})
        assert e.details["revision"] == "abc123"

    def test_is_tenancy_error(self):
        assert isinstance(MigrationError("t", "op", "r"), TenancyError)


# ──────────────────────────── RateLimitExceededError ─────────────────────────


class TestRateLimitExceededError:
    def test_attributes(self):
        e = RateLimitExceededError(tenant_id="t-001", limit=100, window="60 seconds")
        assert e.tenant_id == "t-001"
        assert e.limit == 100
        assert e.window == "60 seconds"

    def test_values_in_message(self):
        e = RateLimitExceededError(tenant_id="t-001", limit=100, window="60 seconds")
        assert "100" in e.message
        assert "60 seconds" in e.message
        assert "t-001" in e.message

    def test_is_tenancy_error(self):
        assert isinstance(RateLimitExceededError("t", 10, "1m"), TenancyError)


# ─────────────────────────── TenantDataLeakageError ──────────────────────────


class TestTenantDataLeakageError:
    def test_attributes(self):
        e = TenantDataLeakageError(
            operation="list_orders",
            expected_tenant="acme-corp",
            actual_tenant="globex",
        )
        assert e.operation == "list_orders"
        assert e.expected_tenant == "acme-corp"
        assert e.actual_tenant == "globex"

    def test_security_keyword_in_message(self):
        e = TenantDataLeakageError("op", "a", "b")
        assert "SECURITY" in e.message

    def test_both_tenant_ids_in_message(self):
        e = TenantDataLeakageError("op", "expected-tenant", "actual-tenant")
        assert "expected-tenant" in e.message
        assert "actual-tenant" in e.message

    def test_is_tenancy_error(self):
        assert isinstance(TenantDataLeakageError("op", "a", "b"), TenancyError)


# ─────────────────────────── TenantQuotaExceededError ────────────────────────


class TestTenantQuotaExceededError:
    def test_integer_quotas(self):
        e = TenantQuotaExceededError(tenant_id="t-001", quota_type="users", current=150, limit=100)
        assert e.current == 150
        assert e.limit == 100
        assert e.quota_type == "users"
        assert e.tenant_id == "t-001"

    def test_float_quotas(self):
        e = TenantQuotaExceededError("t", "storage_gb", current=10.5, limit=10.0)
        assert e.current == 10.5
        assert e.limit == 10.0

    def test_values_in_message(self):
        e = TenantQuotaExceededError("t-001", "users", 150, 100)
        assert "150" in e.message
        assert "100" in e.message

    def test_is_tenancy_error(self):
        assert isinstance(TenantQuotaExceededError("t", "q", 1, 0), TenancyError)


# ─────────────────────────── DatabaseConnectionError ─────────────────────────


class TestDatabaseConnectionError:
    def test_attributes(self):
        e = DatabaseConnectionError(tenant_id="t-001", reason="connection refused")
        assert e.tenant_id == "t-001"
        assert e.reason == "connection refused"

    def test_both_in_message(self):
        e = DatabaseConnectionError(tenant_id="t-001", reason="connection refused")
        assert "t-001" in e.message
        assert "connection refused" in e.message

    def test_is_tenancy_error(self):
        assert isinstance(DatabaseConnectionError("t", "r"), TenancyError)


# ─────────────────────────────── Hierarchy ───────────────────────────────────


class TestExceptionHierarchy:
    ALL = [
        TenantNotFoundError("x"),
        TenantResolutionError("x"),
        TenantInactiveError("x", "suspended"),
        IsolationError("op"),
        ConfigurationError("param", "reason"),
        MigrationError("t", "op", "reason"),
        RateLimitExceededError("t", 10, "1m"),
        TenantDataLeakageError("op", "a", "b"),
        TenantQuotaExceededError("t", "q", 1, 0),
        DatabaseConnectionError("t", "reason"),
    ]

    def test_all_subclass_tenancy_error(self):
        for exc in self.ALL:
            assert isinstance(exc, TenancyError), f"{type(exc).__name__}"

    def test_all_are_exceptions(self):
        for exc in self.ALL:
            assert isinstance(exc, Exception), f"{type(exc).__name__}"

    def test_catch_all_with_base_class(self):
        for exc in self.ALL:
            with pytest.raises(TenancyError):
                raise exc

    def test_all_have_non_empty_message(self):
        for exc in self.ALL:
            assert exc.message, f"{type(exc).__name__}.message empty"

    def test_all_details_are_dict(self):
        for exc in self.ALL:
            assert isinstance(exc.details, dict), f"{type(exc).__name__}"

    def test_distinct_class_names(self):
        names = {type(e).__name__ for e in self.ALL}
        assert len(names) == len(self.ALL)
