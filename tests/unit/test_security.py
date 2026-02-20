"""Unit tests — fastapi_tenancy.utils.security

Coverage target: 100 %

Verified:
* generate_tenant_id — prefix, format, uniqueness, length
* generate_api_key — length, alphabet, uniqueness
* generate_secret_key — hex, byte length
* generate_verification_token — URL-safe chars, length
* constant_time_compare — equality / inequality, timing-safe wrapper
* hash_value — determinism, salt changes digest, different inputs differ
* mask_sensitive_data — default keys masked, custom keys, non-string values,
  non-sensitive keys untouched, nested not recursed (by design)
"""

from __future__ import annotations

import string

import pytest

from fastapi_tenancy.utils.security import (
    constant_time_compare,
    generate_api_key,
    generate_secret_key,
    generate_tenant_id,
    generate_verification_token,
    hash_value,
    mask_sensitive_data,
)

pytestmark = pytest.mark.unit

_URLSAFE_CHARS = string.ascii_letters + string.digits + "-_"


# ──────────────────────────── generate_tenant_id ─────────────────────────────


class TestGenerateTenantId:
    def test_default_prefix(self):
        tid = generate_tenant_id()
        assert tid.startswith("tenant-")

    def test_custom_prefix(self):
        tid = generate_tenant_id(prefix="org")
        assert tid.startswith("org-")

    def test_unique_each_call(self):
        ids = {generate_tenant_id() for _ in range(20)}
        assert len(ids) == 20

    def test_no_spaces(self):
        assert " " not in generate_tenant_id()

    def test_reasonable_length(self):
        tid = generate_tenant_id()
        assert 10 <= len(tid) <= 50


# ────────────────────────── generate_api_key ─────────────────────────────────


class TestGenerateApiKey:
    def test_default_length(self):
        key = generate_api_key()
        assert len(key) == 32

    def test_custom_length(self):
        key = generate_api_key(length=64)
        assert len(key) == 64

    def test_alphanumeric_only(self):
        key = generate_api_key(length=100)
        for ch in key:
            assert ch in string.ascii_letters + string.digits

    def test_unique_each_call(self):
        keys = {generate_api_key() for _ in range(20)}
        assert len(keys) == 20

    def test_length_1(self):
        key = generate_api_key(length=1)
        assert len(key) == 1


# ────────────────────────── generate_secret_key ──────────────────────────────


class TestGenerateSecretKey:
    def test_default_is_128_hex_chars(self):
        # default byte_length=64 → 128 hex chars
        key = generate_secret_key()
        assert len(key) == 128

    def test_custom_byte_length(self):
        key = generate_secret_key(byte_length=32)
        assert len(key) == 64  # 32 bytes = 64 hex chars

    def test_hex_only(self):
        key = generate_secret_key()
        assert all(c in string.hexdigits for c in key)

    def test_unique_each_call(self):
        keys = {generate_secret_key() for _ in range(10)}
        assert len(keys) == 10


# ─────────────────────── generate_verification_token ─────────────────────────


class TestGenerateVerificationToken:
    def test_returns_string(self):
        t = generate_verification_token()
        assert isinstance(t, str)

    def test_url_safe_chars_only(self):
        t = generate_verification_token(byte_length=64)
        assert all(c in _URLSAFE_CHARS for c in t)

    def test_minimum_length(self):
        t = generate_verification_token(byte_length=32)
        # base64url of 32 bytes ≈ 43 chars
        assert len(t) >= 30

    def test_unique_each_call(self):
        tokens = {generate_verification_token() for _ in range(10)}
        assert len(tokens) == 10


# ─────────────────────────── constant_time_compare ───────────────────────────


class TestConstantTimeCompare:
    def test_equal_strings(self):
        assert constant_time_compare("secret", "secret") is True

    def test_different_strings(self):
        assert constant_time_compare("secret", "different") is False

    def test_empty_equal(self):
        assert constant_time_compare("", "") is True

    def test_empty_vs_non_empty(self):
        assert constant_time_compare("", "x") is False

    def test_case_sensitive(self):
        assert constant_time_compare("Secret", "secret") is False

    def test_long_equal_strings(self):
        s = "x" * 1000
        assert constant_time_compare(s, s) is True

    def test_nearly_equal_strings(self):
        assert constant_time_compare("abcde", "abcdf") is False


# ─────────────────────────────── hash_value ──────────────────────────────────


class TestHashValue:
    def test_deterministic(self):
        h1 = hash_value("my-api-key")
        h2 = hash_value("my-api-key")
        assert h1 == h2

    def test_sha256_length(self):
        h = hash_value("anything")
        assert len(h) == 64  # SHA-256 = 32 bytes = 64 hex chars

    def test_hex_output(self):
        h = hash_value("anything")
        assert all(c in string.hexdigits for c in h)

    def test_different_inputs_different_hashes(self):
        h1 = hash_value("input-a")
        h2 = hash_value("input-b")
        assert h1 != h2

    def test_salt_changes_output(self):
        base = hash_value("key")
        salted = hash_value("key", salt="random-salt")
        assert base != salted

    def test_salt_deterministic(self):
        h1 = hash_value("key", salt="s")
        h2 = hash_value("key", salt="s")
        assert h1 == h2

    def test_different_salts_differ(self):
        h1 = hash_value("key", salt="salt1")
        h2 = hash_value("key", salt="salt2")
        assert h1 != h2

    def test_no_salt_none(self):
        h = hash_value("key", salt=None)
        assert len(h) == 64


# ─────────────────────────── mask_sensitive_data ─────────────────────────────


class TestMaskSensitiveData:
    MASK = "***MASKED***"

    def test_password_masked(self):
        result = mask_sensitive_data({"password": "s3cr3t"})
        assert result["password"] == self.MASK

    def test_secret_masked(self):
        result = mask_sensitive_data({"jwt_secret": "abc"})
        assert result["jwt_secret"] == self.MASK

    def test_token_masked(self):
        result = mask_sensitive_data({"token": "xyz"})
        assert result["token"] == self.MASK

    def test_api_key_masked(self):
        result = mask_sensitive_data({"api_key": "key123"})
        assert result["api_key"] == self.MASK

    def test_database_url_masked(self):
        result = mask_sensitive_data({"database_url": "postgresql://user:pass@host/db"})
        assert result["database_url"] == self.MASK

    def test_non_sensitive_key_preserved(self):
        result = mask_sensitive_data({"username": "alice", "password": "s3cr3t"})
        assert result["username"] == "alice"
        assert result["password"] == self.MASK

    def test_empty_dict(self):
        result = mask_sensitive_data({})
        assert result == {}

    def test_non_string_value_masked(self):
        result = mask_sensitive_data({"token": 12345})
        assert result["token"] == self.MASK

    def test_original_dict_not_mutated(self):
        original = {"password": "s3cr3t", "user": "alice"}
        mask_sensitive_data(original)
        assert original["password"] == "s3cr3t"  # original unchanged

    def test_custom_sensitive_keys(self):
        result = mask_sensitive_data({"my_field": "val"}, sensitive_keys=["my_field"])
        assert result["my_field"] == self.MASK

    def test_custom_mask_string(self):
        result = mask_sensitive_data({"password": "s3cr3t"}, mask="[REDACTED]")
        assert result["password"] == "[REDACTED]"

    def test_case_insensitive_key_matching(self):
        # key "PASSWORD" should match sensitive keyword "password"
        result = mask_sensitive_data({"PASSWORD": "s3cr3t"})
        assert result["PASSWORD"] == self.MASK

    def test_partial_key_match(self):
        # "db_password" contains "password"
        result = mask_sensitive_data({"db_password": "secret"})
        assert result["db_password"] == self.MASK

    def test_access_token_masked(self):
        result = mask_sensitive_data({"access_token": "tok"})
        assert result["access_token"] == self.MASK

    def test_refresh_token_masked(self):
        result = mask_sensitive_data({"refresh_token": "rtok"})
        assert result["refresh_token"] == self.MASK

    def test_multiple_keys_in_one_call(self):
        data = {
            "username": "bob",
            "password": "pass",
            "api_key": "key",
            "count": 5,
        }
        result = mask_sensitive_data(data)
        assert result["username"] == "bob"
        assert result["password"] == self.MASK
        assert result["api_key"] == self.MASK
        assert result["count"] == 5
