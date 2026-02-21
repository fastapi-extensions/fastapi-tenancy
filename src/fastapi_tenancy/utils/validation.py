"""Identifier validation and sanitisation utilities.

Every identifier that flows into a DDL statement (``CREATE SCHEMA``,
``CREATE DATABASE``, ``SET search_path``, table-name prefixes, …) **must**
pass through these validators before interpolation.  They are the primary
defence against SQL injection via tenant slugs and schema names.

Security model
--------------
- Input length is capped *before* the regex runs to prevent ReDoS attacks
  on pathologically long strings.
- All patterns are compiled once at module load time.
- ``assert_safe_schema_name`` and ``assert_safe_database_name`` raise
  immediately on invalid input — never silently truncate or sanitise.
  Callers that *want* sanitisation should use ``sanitize_identifier``.
"""

from __future__ import annotations

import json
import re
from typing import Any

################################
# Compiled regular expressions #
################################

# Tenant slug: lowercase letter → 1-61 letters/digits/hyphens → alphanumeric.
# Total length: 3-63 characters.
_TENANT_ID_RE = re.compile(r"^[a-z][a-z0-9\-]{1,61}[a-z0-9]$")

# PostgreSQL/SQLite/MySQL identifier: lowercase letter or underscore,
# then up to 62 letters/digits/underscores — max 63 characters.
_PG_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

# Simple e-mail pattern (not RFC 5321 complete — intentionally conservative).
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

# HTTP/HTTPS URL.
_URL_RE = re.compile(r"^https?://[a-zA-Z0-9.\-]+(:[0-9]{1,5})?(/.*)?$")

# Hard cap applied before any regex to prevent ReDoS.
_MAX_INPUT_LEN: int = 512


##########################
# Tenant slug validation #
##########################


def validate_tenant_identifier(identifier: str) -> bool:
    """Return ``True`` if *identifier* is a valid tenant slug.

    A valid tenant slug:
        - Is a ``str`` instance.
        - Contains between 3 and 63 characters.
        - Starts with a lowercase ASCII letter.
        - Ends with a lowercase ASCII letter or digit.
        - Contains only lowercase letters, digits, and hyphens in between.

    The length is checked *before* the regular expression to prevent ReDoS
    on adversarially crafted inputs.

    Args:
        identifier: The value to validate.

    Returns:
        ``True`` when valid; ``False`` otherwise.

    Examples::

        validate_tenant_identifier("acme-corp")   # True
        validate_tenant_identifier("ACME")        # False  (uppercase)
        validate_tenant_identifier("a")           # False  (too short)
        validate_tenant_identifier("-bad")        # False  (starts with hyphen)
    """
    if not identifier or not isinstance(identifier, str):
        return False
    if len(identifier) > _MAX_INPUT_LEN:
        return False
    return bool(_TENANT_ID_RE.match(identifier))


#####################################
# Schema / database name validation #
#####################################


def validate_schema_name(schema_name: str) -> bool:
    """Return ``True`` if *schema_name* is a safe PostgreSQL identifier.

    A safe identifier:
        - Is a non-empty ``str``.
        - Does not exceed 63 characters.
        - Starts with a lowercase letter or underscore.
        - Contains only lowercase letters, digits, and underscores.

    Args:
        schema_name: The schema name to validate.

    Returns:
        ``True`` when safe; ``False`` otherwise.
    """
    if not schema_name or not isinstance(schema_name, str):
        return False
    if len(schema_name) > _MAX_INPUT_LEN:
        return False
    return bool(_PG_IDENT_RE.match(schema_name))


def validate_database_name(database_name: str) -> bool:
    """Return ``True`` if *database_name* is a safe PostgreSQL identifier.

    Uses identical rules to ``validate_schema_name`` since PostgreSQL applies
    the same grammar to both schema and database names.

    Args:
        database_name: The database name to validate.

    Returns:
        ``True`` when safe; ``False`` otherwise.
    """
    return validate_schema_name(database_name)


def assert_safe_schema_name(schema_name: str, *, context: str = "") -> None:
    """Raise ``ValueError`` if *schema_name* is not a safe identifier.

    Call this immediately before any DDL statement that interpolates a
    schema name.  Raising is the correct behaviour — never silently truncate
    or modify the value.

    Args:
        schema_name: The schema name to assert is safe.
        context: Optional human-readable call-site description included in the
            error message for easier debugging.

    Raises:
        ValueError: When *schema_name* fails validation.

    Example::

        assert_safe_schema_name("tenant_acme_corp")          # OK
        assert_safe_schema_name("'; DROP TABLE tenants; --") # raises
    """
    if not validate_schema_name(schema_name):
        ctx = f" ({context})" if context else ""
        msg = (
            f"Unsafe schema name{ctx}: {schema_name!r}. "
            "Only lowercase letters, digits, and underscores are allowed "
            "(max 63 characters)."
        )
        raise ValueError(msg)


def assert_safe_database_name(database_name: str, *, context: str = "") -> None:
    """Raise ``ValueError`` if *database_name* is not a safe identifier.

    Args:
        database_name: The database name to assert is safe.
        context: Optional call-site description for error messages.

    Raises:
        ValueError: When *database_name* fails validation.
    """
    if not validate_database_name(database_name):
        ctx = f" ({context})" if context else ""
        msg = (
            f"Unsafe database name{ctx}: {database_name!r}. "
            "Only lowercase letters, digits, and underscores are allowed "
            "(max 63 characters)."
        )
        raise ValueError(msg)


##########################################################
# Sanitisation (lossy — for prefix/slug generation only) #
##########################################################


def sanitize_identifier(identifier: str) -> str:
    """Convert an arbitrary string into a valid, safe PostgreSQL identifier.

    This function is *lossy* — it normalises the input rather than rejecting
    invalid values.  Use ``assert_safe_schema_name`` when you need a strict
    guard; use this function only when producing derived values such as
    schema-name prefixes and database names from user-supplied slugs.

    Transformation rules:
        1. Lowercase.
        2. Hyphens and dots replaced with underscores.
        3. All remaining non-alphanumeric, non-underscore characters removed.
        4. Consecutive underscores collapsed to one.
        5. Leading/trailing underscores stripped.
        6. If the first character is a digit, prepend ``"t_"``.
        7. Truncate to 63 characters.
        8. Fall back to ``"tenant"`` for empty results.

    Args:
        identifier: Input string (e.g. a tenant slug or UUID).

    Returns:
        A valid PostgreSQL identifier derived from *identifier*.

    Examples::

        sanitize_identifier("acme-corp")   # "acme_corp"
        sanitize_identifier("2fast")       # "t_2fast"
        sanitize_identifier("A B C")       # "a_b_c"
    """
    s = identifier.lower().replace("-", "_").replace(".", "_")
    s = re.sub(r"[^a-z0-9_]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if s and not s[0].isalpha():
        s = f"t_{s}"
    return (s or "tenant")[:63]


############################
# Miscellaneous validators #
############################


def validate_email(email: str) -> bool:
    """Return ``True`` if *email* matches a basic e-mail pattern.

    Args:
        email: The string to validate.

    Returns:
        ``True`` when the value looks like a valid e-mail address.
    """
    if not email or not isinstance(email, str):
        return False
    return bool(_EMAIL_RE.match(email))


def validate_url(url: str) -> bool:
    """Return ``True`` if *url* is a well-formed HTTP or HTTPS URL.

    Args:
        url: The string to validate.

    Returns:
        ``True`` when the value looks like a valid HTTP/HTTPS URL.
    """
    if not url or not isinstance(url, str):
        return False
    return bool(_URL_RE.match(url))


def validate_json_serializable(value: Any) -> bool:
    """Return ``True`` if *value* can be serialised to JSON without error.

    Args:
        value: Any Python value.

    Returns:
        ``True`` when ``json.dumps`` succeeds without error.
    """
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    else:
        return True


__all__ = [
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
