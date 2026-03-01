"""Tests for fastapi_tenancy.utils — validation, sanitize, db_compat, security."""

from __future__ import annotations

import pytest

from fastapi_tenancy.utils._sanitize import core_sanitize_identifier
from fastapi_tenancy.utils.db_compat import (
    DbDialect,
    detect_dialect,
    get_schema_set_sql,
    get_set_tenant_sql,
    make_table_prefix,
    requires_static_pool,
    supports_native_rls,
    supports_native_schemas,
)
from fastapi_tenancy.utils.security import (
    constant_time_compare,
    generate_api_key,
    generate_secret_key,
    generate_tenant_id,
    generate_verification_token,
    hash_value,
    mask_sensitive_data,
)
from fastapi_tenancy.utils.validation import (
    assert_safe_database_name,
    assert_safe_schema_name,
    sanitize_identifier,
    validate_database_name,
    validate_email,
    validate_json_serializable,
    validate_schema_name,
    validate_tenant_identifier,
    validate_url,
)


# ---------------------------------------------------------------------------
# core_sanitize_identifier
# ---------------------------------------------------------------------------


class TestCoreSanitizeIdentifier:
    @pytest.mark.parametrize(
        "inp,expected",
        [
            ("acme-corp", "acme_corp"),
            ("UPPER", "upper"),
            ("my.company", "my_company"),
            ("2fast", "t_2fast"),
            ("A B C", "a_b_c"),
            ("", "tenant"),
            ("  spaces  ", "spaces"),
            ("__leading__", "leading"),
            ("a" * 70, "a" * 63),
        ],
    )
    def test_transformation(self, inp, expected):
        assert core_sanitize_identifier(inp) == expected

    def test_consecutive_underscores_collapsed(self):
        result = core_sanitize_identifier("a--b")
        assert "__" not in result

    def test_digit_start_gets_t_prefix(self):
        result = core_sanitize_identifier("3com")
        assert result.startswith("t_")

    def test_max_63_chars(self):
        result = core_sanitize_identifier("a" * 100)
        assert len(result) <= 63

    def test_empty_falls_back_to_tenant(self):
        assert core_sanitize_identifier("!@#$%") == "tenant"


# ---------------------------------------------------------------------------
# validate_tenant_identifier
# ---------------------------------------------------------------------------


class TestValidateTenantIdentifier:
    @pytest.mark.parametrize(
        "ident",
        [
            "acme-corp",
            "globex-inc",
            "my-company-1",
            "ab1",  # 3 chars, valid
            "a" + "b" * 61 + "c",  # 63 chars, valid
        ],
    )
    def test_valid(self, ident):
        assert validate_tenant_identifier(ident) is True

    @pytest.mark.parametrize(
        "ident",
        [
            "",
            "ab",        # too short (2 chars)
            "a" * 64,   # too long
            "ACME",     # uppercase
            "-bad",     # starts with hyphen
            "bad-",     # ends with hyphen
            "bad.dot",  # contains dot
            "bad_under",  # contains underscore
            "123abc",   # starts with digit
            None,       # not a string
            123,        # not a string
        ],
    )
    def test_invalid(self, ident):
        assert validate_tenant_identifier(ident) is False

    def test_very_long_input_handled_safely(self):
        # 513-char string exceeds _MAX_INPUT_LEN (512), must return False without blowing up
        assert validate_tenant_identifier("a" * 513) is False


# ---------------------------------------------------------------------------
# validate_schema_name / validate_database_name
# ---------------------------------------------------------------------------


class TestValidateSchemaName:
    @pytest.mark.parametrize(
        "name",
        [
            "tenant_acme",
            "public",
            "_private",
            "t1",
            "a" * 63,
        ],
    )
    def test_valid(self, name):
        assert validate_schema_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "UPPER",
            "tenant-hyphen",
            "1start",
            "a" * 64,
        ],
    )
    def test_invalid(self, name):
        assert validate_schema_name(name) is False

    def test_validate_database_name_same_rules(self):
        assert validate_database_name("tenant_acme") is True
        assert validate_database_name("UPPER") is False


# ---------------------------------------------------------------------------
# assert_safe_schema_name / assert_safe_database_name
# ---------------------------------------------------------------------------


class TestAssertSafeNames:
    def test_safe_schema_passes(self):
        assert_safe_schema_name("tenant_acme")  # no exception

    def test_unsafe_schema_raises(self):
        with pytest.raises(ValueError, match="Unsafe schema name"):
            assert_safe_schema_name("'; DROP TABLE tenants; --")

    def test_context_included_in_error(self):
        with pytest.raises(ValueError, match="my_context"):
            assert_safe_schema_name("BAD", context="my_context")

    def test_safe_database_passes(self):
        assert_safe_database_name("mydb")

    def test_unsafe_database_raises(self):
        with pytest.raises(ValueError, match="Unsafe database name"):
            assert_safe_database_name("BAD-DB!")


# ---------------------------------------------------------------------------
# sanitize_identifier
# ---------------------------------------------------------------------------


class TestSanitizeIdentifier:
    def test_delegates_to_core(self):
        assert sanitize_identifier("acme-corp") == "acme_corp"
        assert sanitize_identifier("2bad") == "t_2bad"


# ---------------------------------------------------------------------------
# validate_email / validate_url / validate_json_serializable
# ---------------------------------------------------------------------------


class TestMiscValidators:
    @pytest.mark.parametrize(
        "email",
        ["user@example.com", "a+tag@sub.domain.io", "first.last@company.co.uk"],
    )
    def test_valid_email(self, email):
        assert validate_email(email) is True

    @pytest.mark.parametrize("email", ["", "notanemail", "@domain.com", "user@", None])
    def test_invalid_email(self, email):
        assert validate_email(email) is False

    @pytest.mark.parametrize(
        "url",
        ["http://example.com", "https://api.example.com/v1/path", "http://localhost:8080"],
    )
    def test_valid_url(self, url):
        assert validate_url(url) is True

    @pytest.mark.parametrize("url", ["", "ftp://bad.com", "not-a-url", None])
    def test_invalid_url(self, url):
        assert validate_url(url) is False

    def test_json_serializable_valid(self):
        assert validate_json_serializable({"key": [1, 2, None]}) is True
        assert validate_json_serializable("string") is True
        assert validate_json_serializable(42) is True

    def test_json_serializable_invalid(self):
        import datetime
        assert validate_json_serializable(datetime.datetime.now()) is False
        assert validate_json_serializable(object()) is False


# ---------------------------------------------------------------------------
# DbDialect + detect_dialect
# ---------------------------------------------------------------------------


class TestDetectDialect:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("postgresql+asyncpg://user:pass@host/db", DbDialect.POSTGRESQL),
            ("postgresql://user:pass@host/db", DbDialect.POSTGRESQL),
            ("postgresql+psycopg://host/db", DbDialect.POSTGRESQL),
            ("sqlite+aiosqlite:///:memory:", DbDialect.SQLITE),
            ("sqlite:///test.db", DbDialect.SQLITE),
            ("mysql+aiomysql://host/db", DbDialect.MYSQL),
            ("mysql://host/db", DbDialect.MYSQL),
            ("mariadb+aiomysql://host/db", DbDialect.MYSQL),
            ("mssql+aioodbc://host/db", DbDialect.MSSQL),
            ("mssql://host/db", DbDialect.MSSQL),
            ("unknown://host/db", DbDialect.UNKNOWN),
            ("no-scheme-at-all", DbDialect.UNKNOWN),
        ],
    )
    def test_detection(self, url, expected):
        assert detect_dialect(url) == expected


class TestCapabilityPredicates:
    def test_supports_native_schemas_postgresql(self):
        assert supports_native_schemas(DbDialect.POSTGRESQL) is True

    def test_supports_native_schemas_mssql(self):
        assert supports_native_schemas(DbDialect.MSSQL) is True

    def test_supports_native_schemas_sqlite(self):
        assert supports_native_schemas(DbDialect.SQLITE) is False

    def test_supports_native_schemas_mysql(self):
        assert supports_native_schemas(DbDialect.MYSQL) is False

    def test_supports_native_rls_postgresql_only(self):
        assert supports_native_rls(DbDialect.POSTGRESQL) is True
        assert supports_native_rls(DbDialect.SQLITE) is False
        assert supports_native_rls(DbDialect.MYSQL) is False
        assert supports_native_rls(DbDialect.MSSQL) is False

    def test_requires_static_pool_sqlite_only(self):
        assert requires_static_pool(DbDialect.SQLITE) is True
        assert requires_static_pool(DbDialect.POSTGRESQL) is False

    def test_get_set_tenant_sql_postgresql(self):
        sql = get_set_tenant_sql(DbDialect.POSTGRESQL)
        assert sql is not None
        assert ":tenant_id" in sql

    def test_get_set_tenant_sql_sqlite_none(self):
        assert get_set_tenant_sql(DbDialect.SQLITE) is None

    def test_get_schema_set_sql_postgresql(self):
        sql = get_schema_set_sql(DbDialect.POSTGRESQL)
        assert sql is not None
        assert "{schema}" in sql

    def test_get_schema_set_sql_sqlite_none(self):
        assert get_schema_set_sql(DbDialect.SQLITE) is None


class TestMakeTablePrefix:
    def test_acme_corp(self):
        result = make_table_prefix("acme-corp")
        assert result.startswith("t_")
        assert result.endswith("_")

    def test_digit_start_no_double_t(self):
        result = make_table_prefix("2fast")
        # Should not produce "t_t_..."
        assert not result.startswith("t_t_")

    def test_max_length_respected(self):
        # Table prefix + table name should remain manageable
        prefix = make_table_prefix("a" * 50)
        assert len(prefix) <= 25  # 20 char base + "t_" + "_"


# ---------------------------------------------------------------------------
# Security utilities
# ---------------------------------------------------------------------------


class TestSecurityUtils:
    def test_generate_tenant_id_format(self):
        tid = generate_tenant_id()
        assert tid.startswith("tenant-")
        assert len(tid) > 10

    def test_generate_tenant_id_custom_prefix(self):
        tid = generate_tenant_id(prefix="org")
        assert tid.startswith("org-")

    def test_generate_tenant_ids_are_unique(self):
        ids = {generate_tenant_id() for _ in range(100)}
        assert len(ids) == 100

    def test_generate_api_key_length(self):
        key = generate_api_key(32)
        assert len(key) == 32

    def test_generate_api_key_alphanumeric(self):
        key = generate_api_key(64)
        assert key.isalnum()

    def test_generate_secret_key_hex(self):
        key = generate_secret_key(32)
        assert len(key) == 64  # 32 bytes * 2 hex chars
        int(key, 16)  # must be valid hex

    def test_generate_verification_token(self):
        token = generate_verification_token()
        assert len(token) > 0

    def test_constant_time_compare_equal(self):
        assert constant_time_compare("abc", "abc") is True

    def test_constant_time_compare_not_equal(self):
        assert constant_time_compare("abc", "xyz") is False

    def test_hash_value_deterministic(self):
        assert hash_value("test") == hash_value("test")
        assert len(hash_value("test")) == 64  # SHA-256

    def test_hash_value_with_salt(self):
        h1 = hash_value("test", salt="s1")
        h2 = hash_value("test", salt="s2")
        assert h1 != h2

    def test_mask_sensitive_data_masks_password(self):
        data = {"username": "alice", "password": "s3cr3t"}
        result = mask_sensitive_data(data)
        assert result["username"] == "alice"
        assert result["password"] == "***MASKED***"

    def test_mask_sensitive_data_masks_database_url(self):
        data = {"database_url": "postgresql://user:pass@host/db"}
        result = mask_sensitive_data(data)
        assert "pass" not in result["database_url"]

    def test_mask_sensitive_data_case_insensitive(self):
        data = {"PASSWORD": "secret", "Api_Key": "key123"}
        result = mask_sensitive_data(data)
        assert result["PASSWORD"] == "***MASKED***"
        assert result["Api_Key"] == "***MASKED***"

    def test_mask_sensitive_data_custom_keys(self):
        data = {"phone": "555-1234", "normal": "value"}
        result = mask_sensitive_data(data, sensitive_keys=["phone"])
        assert result["phone"] == "***MASKED***"
        assert result["normal"] == "value"

    def test_mask_sensitive_data_does_not_mutate_original(self):
        data = {"password": "secret"}
        mask_sensitive_data(data)
        assert data["password"] == "secret"
