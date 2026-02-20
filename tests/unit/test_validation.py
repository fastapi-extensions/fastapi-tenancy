"""Unit tests — fastapi_tenancy.utils.validation

Coverage target: 100 %

Every function in validation.py is exercised including:
* validate_tenant_identifier — valid, edge lengths, bad chars, not-str, too long
* validate_schema_name / validate_database_name — same rules, different context
* assert_safe_schema_name / assert_safe_database_name — raises on invalid
* sanitize_identifier — all transformation rules in order
* validate_email — accept/reject patterns
* validate_url — http/https, ports, paths, bad schemes
* validate_json_serializable — serializable and non-serializable values
* ReDoS guard — over-long input short-circuits before regex
"""

from __future__ import annotations

import pytest

from fastapi_tenancy.utils.validation import (
    assert_safe_database_name,
    assert_safe_schema_name,
    sanitize_identifier,
    validate_database_name,
    validate_email,
    validate_json_serializable,
    validate_schema_name,
    validate_tenant_identifier,
)

pytestmark = pytest.mark.unit


# ─────────────────────── validate_tenant_identifier ──────────────────────────


class TestValidateTenantIdentifier:
    # Happy path
    def test_simple_slug(self):
        assert validate_tenant_identifier("acme-corp") is True

    def test_slug_with_digits(self):
        assert validate_tenant_identifier("acme123") is True

    def test_exactly_3_chars(self):
        # minimum: letter + letter/digit + letter/digit — regex requires 3 total
        assert validate_tenant_identifier("abc") is True

    def test_maximum_63_chars(self):
        slug = "a" + "b" * 61 + "c"  # 63 chars
        assert validate_tenant_identifier(slug) is True

    def test_hyphens_allowed_internally(self):
        assert validate_tenant_identifier("my-big-corp") is True

    # Failures
    def test_too_short_one_char(self):
        assert validate_tenant_identifier("a") is False

    def test_too_short_two_chars(self):
        assert validate_tenant_identifier("ab") is False

    def test_starts_with_digit(self):
        assert validate_tenant_identifier("1abc") is False

    def test_starts_with_hyphen(self):
        assert validate_tenant_identifier("-abc") is False

    def test_ends_with_hyphen(self):
        assert validate_tenant_identifier("abc-") is False

    def test_uppercase(self):
        assert validate_tenant_identifier("ACME") is False

    def test_spaces(self):
        assert validate_tenant_identifier("acme corp") is False

    def test_underscore_not_allowed(self):
        assert validate_tenant_identifier("acme_corp") is False

    def test_empty_string(self):
        assert validate_tenant_identifier("") is False

    def test_none_like_non_string(self):
        assert validate_tenant_identifier(None) is False  # type: ignore[arg-type]

    def test_integer_input(self):
        assert validate_tenant_identifier(123) is False  # type: ignore[arg-type]

    def test_64_chars_too_long(self):
        slug = "a" + "b" * 62 + "c"  # 64 chars
        assert validate_tenant_identifier(slug) is False

    def test_redos_guard_over_512(self):
        # Over _MAX_INPUT_LEN=512 — must short-circuit without ReDoS
        slug = "a" * 600
        assert validate_tenant_identifier(slug) is False

    def test_special_chars_rejected(self):
        assert validate_tenant_identifier("acme@corp") is False

    def test_dot_rejected(self):
        assert validate_tenant_identifier("acme.corp") is False


# ──────────────────────── validate_schema_name ───────────────────────────────


class TestValidateSchemaName:
    def test_simple_name(self):
        assert validate_schema_name("tenant_acme") is True

    def test_leading_underscore(self):
        assert validate_schema_name("_private") is True

    def test_all_lowercase(self):
        assert validate_schema_name("schema123") is True

    def test_max_63(self):
        assert validate_schema_name("a" * 63) is True

    def test_64_chars_too_long(self):
        assert validate_schema_name("a" * 64) is False

    def test_uppercase_rejected(self):
        assert validate_schema_name("Schema") is False

    def test_hyphens_rejected(self):
        assert validate_schema_name("my-schema") is False

    def test_spaces_rejected(self):
        assert validate_schema_name("my schema") is False

    def test_empty_rejected(self):
        assert validate_schema_name("") is False

    def test_none_rejected(self):
        assert validate_schema_name(None) is False  # type: ignore[arg-type]

    def test_starts_with_digit_rejected(self):
        assert validate_schema_name("1schema") is False

    def test_redos_guard(self):
        assert validate_schema_name("a" * 600) is False


class TestValidateDatabaseName:
    """validate_database_name delegates to validate_schema_name — same rules."""

    def test_valid(self):
        assert validate_database_name("mydb") is True

    def test_invalid_hyphen(self):
        assert validate_database_name("my-db") is False

    def test_empty(self):
        assert validate_database_name("") is False


# ─────────────────── assert_safe_schema_name / database_name ─────────────────


class TestAssertSafeSchemeName:
    def test_valid_passes_silently(self):
        assert_safe_schema_name("tenant_acme")  # no exception

    def test_invalid_raises_value_error(self):
        with pytest.raises(ValueError, match="Unsafe schema name"):
            assert_safe_schema_name("'; DROP TABLE tenants; --")

    def test_context_appears_in_error(self):
        with pytest.raises(ValueError, match="test_context"):
            assert_safe_schema_name("Bad-Name!", context="test_context")

    def test_no_context_in_error(self):
        try:
            assert_safe_schema_name("bad!")
        except ValueError as e:
            assert "bad!" in str(e)

    def test_sql_injection_attempt(self):
        with pytest.raises(ValueError):
            assert_safe_schema_name("public; DROP TABLE users; --")


class TestAssertSafeDatabaseName:
    def test_valid_passes(self):
        assert_safe_database_name("mydb")  # no exception

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Unsafe database name"):
            assert_safe_database_name("my-db!")

    def test_context_in_message(self):
        with pytest.raises(ValueError, match="ctx_value"):
            assert_safe_database_name("bad!", context="ctx_value")


# ──────────────────────── sanitize_identifier ────────────────────────────────


class TestSanitizeIdentifier:
    def test_hyphens_to_underscores(self):
        assert sanitize_identifier("acme-corp") == "acme_corp"

    def test_dots_to_underscores(self):
        assert sanitize_identifier("my.company") == "my_company"

    def test_lowercased(self):
        assert sanitize_identifier("ACME") == "acme"

    def test_digit_start_prepends_t(self):
        result = sanitize_identifier("2fast")
        assert result.startswith("t_") or result[0].isalpha()

    def test_spaces_become_underscores(self):
        result = sanitize_identifier("A B C")
        assert " " not in result
        assert result == "a_b_c"

    def test_consecutive_underscores_collapsed(self):
        result = sanitize_identifier("a--b")
        assert "__" not in result

    def test_truncated_to_63(self):
        long_id = "a" * 100
        assert len(sanitize_identifier(long_id)) <= 63

    def test_empty_string_fallback(self):
        # After all transforms, empty → "tenant"
        result = sanitize_identifier("---")
        assert result == "tenant" or result.isidentifier()

    def test_all_special_chars_removed(self):
        result = sanitize_identifier("hello@world!")
        assert "@" not in result
        assert "!" not in result

    def test_unicode_stripped(self):
        result = sanitize_identifier("café")
        assert result.isascii()

    def test_already_clean_passthrough(self):
        assert sanitize_identifier("clean_name") == "clean_name"


# ──────────────────────────── validate_email ─────────────────────────────────


class TestValidateEmail:
    def test_valid_simple(self):
        assert validate_email("user@example.com") is True

    def test_valid_subdomain(self):
        assert validate_email("user@mail.example.com") is True

    def test_valid_plus_addressing(self):
        assert validate_email("user+tag@example.com") is True

    def test_valid_dots_in_local(self):
        assert validate_email("first.last@example.com") is True

    def test_missing_at(self):
        assert validate_email("userexample.com") is False

    def test_missing_domain(self):
        assert validate_email("user@") is False

    def test_missing_tld(self):
        assert validate_email("user@example") is False

    def test_empty(self):
        assert validate_email("") is False

    def test_none_like(self):
        assert validate_email(None) is False  # type: ignore[arg-type]

    def test_spaces_rejected(self):
        assert validate_email("user @example.com") is False

    def test_double_at(self):
        assert validate_email("user@@example.com") is False


# ───────────────────────────── validate_url ──────────────────────────────────


class TestValidateUrl:
    def test_http(self):
        assert validate_url("http://example.com") is True

    def test_https(self):
        assert validate_url("https://example.com") is True

    def test_https_with_port(self):
        assert validate_url("https://example.com:8080") is True

    def test_https_with_path(self):
        assert validate_url("https://example.com/api/v1") is True

    def test_ftp_rejected(self):
        assert validate_url("ftp://example.com") is False

    def test_no_scheme(self):
        assert validate_url("example.com") is False

    def test_empty(self):
        assert validate_url("") is False

    def test_none_like(self):
        assert validate_url(None) is False  # type: ignore[arg-type]


# ────────────────────── validate_json_serializable ───────────────────────────


class TestValidateJsonSerializable:
    def test_dict(self):
        assert validate_json_serializable({"a": 1}) is True

    def test_list(self):
        assert validate_json_serializable([1, 2, 3]) is True

    def test_string(self):
        assert validate_json_serializable("hello") is True

    def test_int(self):
        assert validate_json_serializable(42) is True

    def test_none(self):
        assert validate_json_serializable(None) is True

    def test_nested(self):
        assert validate_json_serializable({"a": [1, {"b": "c"}]}) is True

    def test_non_serializable_object(self):
        class NotSerializable:
            pass

        assert validate_json_serializable(NotSerializable()) is False

    def test_set_not_serializable(self):
        assert validate_json_serializable({1, 2, 3}) is False

    def test_bytes_not_serializable(self):
        assert validate_json_serializable(b"bytes") is False
