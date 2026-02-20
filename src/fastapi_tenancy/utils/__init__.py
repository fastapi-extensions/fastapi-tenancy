"""Utility functions — identifier validation, security helpers, and DB compatibility."""

from fastapi_tenancy.utils.db_compat import DbDialect, detect_dialect
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

__all__ = [
    # DB compatibility
    "DbDialect",
    "detect_dialect",
    # Security
    "constant_time_compare",
    "generate_api_key",
    "generate_secret_key",
    "generate_tenant_id",
    "generate_verification_token",
    "hash_value",
    "mask_sensitive_data",
    # Validation
    "assert_safe_database_name",
    "assert_safe_schema_name",
    "sanitize_identifier",
    "validate_database_name",
    "validate_email",
    "validate_json_serializable",
    "validate_schema_name",
    "validate_tenant_identifier",
    "validate_url",
]
