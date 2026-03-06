"""Security utilities for fastapi-tenancy.

All functions use Python's ``secrets`` module which is backed by the operating
system's cryptographically secure random number generator.  Do **not** replace
calls here with ``random``.

Public API
----------
``generate_tenant_id``
    Generate a random, URL-safe opaque tenant ID.

``generate_api_key``
    Generate a random, alphanumeric API key.

``generate_secret_key``
    Generate a long hex-encoded key for JWT secrets and encryption.

``generate_verification_token``
    Generate a URL-safe token for e-mail / phone verification flows.

``constant_time_compare``
    Compare two strings in constant time (timing-attack safe).

``hash_value``
    Compute a SHA-256 hash with optional salt.

``mask_sensitive_data``
    Redact sensitive keys from a dictionary before logging or serialisation.
"""

from __future__ import annotations

import hashlib
import secrets
import string
from typing import Any


def generate_tenant_id(prefix: str = "tenant") -> str:
    """Generate a cryptographically secure, URL-safe opaque tenant ID.

    The generated ID is *not* a slug — it is an internal primary key intended
    to be opaque to end-users.  Use the tenant's ``identifier`` field for
    human-readable values.

    Args:
        prefix: Short string prepended to the random part.  Defaults to
            ``"tenant"``.

    Returns:
        A string of the form ``"{prefix}-{random}"`` where ``random`` is 16
        URL-safe base64 characters.

    Example::

        generate_tenant_id()       # "tenant-aB3xYz9mQp2sKl7n"
        generate_tenant_id("org")  # "org-Kl7nMf4wTv1cBz8p"
    """
    return f"{prefix}-{secrets.token_urlsafe(12)}"


def generate_api_key(length: int = 32) -> str:
    """Generate a random alphanumeric API key.

    The output alphabet is ``[A-Za-z0-9]`` with 62 characters, giving
    approximately ``log2(62^32) ≈ 190`` bits of entropy for the default length.

    Args:
        length: Number of characters in the key.  Minimum recommended: 32.

    Returns:
        A random alphanumeric string of *length* characters.

    Example::

        generate_api_key()    # "Xy3mPq7nRt2kLs9..."  (32 chars)
        generate_api_key(64)  # 64-character key
    """
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_secret_key(byte_length: int = 64) -> str:
    """Generate a hex-encoded secret key for JWT secrets and HMAC keys.

    The output is ``2 * byte_length`` hex characters.  The default
    ``byte_length=64`` yields a 512-bit key.

    Args:
        byte_length: Number of random bytes before hex encoding.

    Returns:
        Hex-encoded random string.

    Example::

        secret = generate_secret_key()  # 128-char hex string (512 bits)
    """
    return secrets.token_hex(byte_length)


def generate_verification_token(byte_length: int = 32) -> str:
    """Generate a URL-safe verification token.

    Suitable for e-mail confirmation links, password-reset tokens, and similar
    single-use flows.

    Args:
        byte_length: Number of random bytes before URL-safe base64 encoding.

    Returns:
        A URL-safe base64-encoded random string.

    Example::

        token = generate_verification_token()
        url = f"https://app.example.com/verify?token={token}"
    """
    return secrets.token_urlsafe(byte_length)


def constant_time_compare(value1: str, value2: str) -> bool:
    """Compare two strings in constant time to prevent timing attacks.

    This is the correct primitive for comparing API keys, HMAC digests, and
    other secrets.  A naive ``==`` comparison short-circuits on the first
    differing byte, leaking information about partial matches.

    Args:
        value1: First string.
        value2: Second string.

    Returns:
        ``True`` when both strings are identical.
    """
    return secrets.compare_digest(value1, value2)


def hash_value(value: str, salt: str | None = None) -> str:
    """Compute a hex-encoded SHA-256 hash of *value* with an optional *salt*.

    Args:
        value: The string to hash.
        salt: Optional salt prepended to *value* before hashing.

    Returns:
        Lowercase hex-encoded SHA-256 digest (64 characters).

    Example::

        hash_value("my-api-key")
        hash_value("my-api-key", salt="random-salt-string")
    """
    payload = f"{salt}{value}" if salt else value
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def mask_sensitive_data(
    data: dict[str, Any],
    sensitive_keys: list[str] | None = None,
    mask: str = "***MASKED***",
) -> dict[str, Any]:
    """Return a copy of *data* with sensitive values replaced by *mask*.

    Key matching is case-insensitive substring search against the default
    (or caller-supplied) list of sensitive keywords.

    Args:
        data: The dictionary to sanitise.
        sensitive_keys: List of keyword substrings that mark a key as sensitive.
            Defaults to a curated list covering common credential and secret
            field names.
        mask: Replacement string for masked values.

    Returns:
        A shallow copy of *data* with sensitive string values replaced.
        Non-string values at matching keys are also replaced.

    Example::

        mask_sensitive_data({"username": "alice", "password": "s3cr3t"})
        # → {"username": "alice", "password": "***MASKED***"}
    """
    if sensitive_keys is None:
        sensitive_keys = [
            "password",
            "secret",
            "token",
            "api_key",
            "apikey",
            "database_url",
            "connection_string",
            "private_key",
            "access_token",
            "refresh_token",
            "encryption_key",
            "jwt_secret",
        ]

    result = dict(data)
    for key in result:
        key_lower = key.lower()
        if any(sensitive in key_lower for sensitive in sensitive_keys):
            result[key] = mask
    return result


__all__ = [
    "constant_time_compare",
    "generate_api_key",
    "generate_secret_key",
    "generate_tenant_id",
    "generate_verification_token",
    "hash_value",
    "mask_sensitive_data",
]
