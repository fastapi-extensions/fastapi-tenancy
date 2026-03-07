"""Fernet-based field-level encryption for sensitive tenant data.

This module provides :class:`TenancyEncryption` — a thin wrapper around the
``cryptography`` library's Fernet symmetric encryption scheme.  It is used to
encrypt and decrypt sensitive ``Tenant`` fields (``database_url``, ``metadata``
values) at rest when ``TenancyConfig.enable_encryption=True``.

Why Fernet?
-----------
Fernet is AES-128-CBC with HMAC-SHA256 for authentication, a 128-bit random IV
(prepended to the ciphertext), and a timestamp.  It is:

* **Authenticated** — a forged or tampered ciphertext raises ``InvalidToken``
  rather than silently decrypting garbage.
* **Standard** — part of the ``cryptography`` package which is already a
  transitive dependency of many FastAPI stacks via ``httpx`` / ``pyOpenSSL``.
* **Simple** — a single ``encrypt(plaintext) → ciphertext`` / ``decrypt``
  interface with no algorithm-selection footguns.

Key derivation
--------------
``TenancyConfig.encryption_key`` is a caller-supplied base64 URL-safe string of
at least 32 characters.  The raw bytes are passed through HKDF-SHA256 to
derive a 32-byte Fernet-compatible key.  This means:

1. Users can supply any high-entropy passphrase without manually padding to
   exactly 32 bytes.
2. Changing the derivation context string (``b"fastapi-tenancy-v1"``) in a
   future version provides domain separation from other uses of the same key
   material.

Encrypted fields
----------------
The following ``Tenant`` fields are encrypted when ``enable_encryption=True``:

* ``database_url`` — connection credentials must not leak in backups or logs.
* Metadata values whose key starts with ``"_enc_"`` — application-defined
  sensitive attributes (e.g. ``_enc_api_key``, ``_enc_webhook_secret``).

Ciphertext encoding
-------------------
Encrypted values are stored as ASCII strings prefixed with ``"enc::"`` so that:

* Unencrypted and encrypted databases can coexist during a rolling migration.
* The layer that reads the value can detect at runtime whether to decrypt.
* The prefix is not a secret — the ciphertext itself provides authenticity.

Usage
-----
::

    enc = TenancyEncryption.from_config(config)

    encrypted_url = enc.encrypt("postgresql+asyncpg://user:pass@host/tenant_db")
    original_url  = enc.decrypt(encrypted_url)

    tenant_row = enc.encrypt_tenant_fields(tenant)
    plain_tenant = enc.decrypt_tenant_fields(tenant_row)
"""

from __future__ import annotations

import base64
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Prefix that marks a string as Fernet-encrypted.
_ENCRYPTED_PREFIX = "enc::"

#: Metadata key prefix that triggers per-value encryption.
_ENC_METADATA_PREFIX = "_enc_"


def _derive_fernet_key(raw_key: str) -> bytes:
    """Derive a 32-byte URL-safe base64 Fernet key from *raw_key* via HKDF.

    Args:
        raw_key: Caller-supplied key string (at least 32 characters).

    Returns:
        32 bytes of URL-safe base64-encoded key material suitable for
        ``Fernet(key)``.
    """
    from cryptography.hazmat.primitives import hashes  # noqa: PLC0415
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: PLC0415

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"fastapi-tenancy-v1",
    )
    derived = hkdf.derive(raw_key.encode("utf-8"))
    # Fernet requires URL-safe base64-encoded 32-byte key.
    return base64.urlsafe_b64encode(derived)


class TenancyEncryption:
    """Fernet-based field-level encryption helper.

    Do not instantiate directly — use :meth:`from_config` which handles
    the ``enable_encryption=False`` no-op case cleanly.

    Args:
        fernet_key: 32-byte URL-safe base64 key (output of :func:`_derive_fernet_key`).
    """

    def __init__(self, fernet_key: bytes) -> None:
        from cryptography.fernet import Fernet  # noqa: PLC0415

        self._fernet = Fernet(fernet_key)

    @classmethod
    def from_config(cls, config: Any) -> TenancyEncryption | None:
        """Build a :class:`TenancyEncryption` from a :class:`~fastapi_tenancy.core.config.TenancyConfig`.

        Returns ``None`` when ``enable_encryption=False`` so callers can guard
        with a simple ``if enc:`` pattern.

        Args:
            config: The :class:`~fastapi_tenancy.core.config.TenancyConfig` instance.

        Returns:
            A configured :class:`TenancyEncryption`, or ``None`` when encryption
            is disabled.

        Raises:
            ValueError: When ``enable_encryption=True`` but no ``encryption_key``
                is set (should be caught by Pydantic validator, but guarded here
                as defence-in-depth).
        """  # noqa: E501
        if not config.enable_encryption:
            return None
        if not config.encryption_key:
            msg = "encryption_key must be set when enable_encryption=True"
            raise ValueError(msg)
        key = _derive_fernet_key(config.encryption_key)
        return cls(key)

    # ------------------------------------------------------------------
    # Low-level encrypt / decrypt
    # ------------------------------------------------------------------

    def encrypt(self, plaintext: str) -> str:
        """Encrypt *plaintext* and return a prefixed ciphertext string.

        Idempotent — already-encrypted strings (those starting with
        ``"enc::"``) are returned unchanged to allow safe re-encryption
        passes on partially migrated data.

        Args:
            plaintext: The string value to encrypt.

        Returns:
            ``"enc::" + base64(ciphertext)`` as an ASCII string.
        """
        if plaintext.startswith(_ENCRYPTED_PREFIX):
            return plaintext  # already encrypted — idempotent
        token = self._fernet.encrypt(plaintext.encode("utf-8"))
        return _ENCRYPTED_PREFIX + token.decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a prefixed ciphertext string and return the original plaintext.

        Idempotent — plaintext strings (those not starting with ``"enc::"``)
        are returned unchanged to allow safe decryption passes on partially
        migrated data.

        Args:
            ciphertext: The ``"enc::..."`` string produced by :meth:`encrypt`,
                or a plain (unencrypted) string.

        Returns:
            The original plaintext string.

        Raises:
            cryptography.fernet.InvalidToken: When the ciphertext is forged,
                truncated, or was encrypted with a different key.
        """
        if not ciphertext.startswith(_ENCRYPTED_PREFIX):
            return ciphertext  # not encrypted — return as-is (migration compat)
        token = ciphertext[len(_ENCRYPTED_PREFIX) :].encode("ascii")
        return self._fernet.decrypt(token).decode("utf-8")

    def is_encrypted(self, value: str) -> bool:
        """Return ``True`` when *value* is an encrypted ciphertext.

        Args:
            value: String to test.

        Returns:
            ``True`` when the value starts with the encryption prefix.
        """
        return value.startswith(_ENCRYPTED_PREFIX)

    # ------------------------------------------------------------------
    # Tenant-level helpers
    # ------------------------------------------------------------------

    def encrypt_tenant_fields(self, tenant: Any) -> Any:
        """Return a copy of *tenant* with sensitive fields encrypted.

        Fields encrypted:
        * ``database_url`` — if present and not already encrypted.
        * Metadata values whose key starts with ``"_enc_"``.

        Args:
            tenant: A :class:`~fastapi_tenancy.core.types.Tenant` instance.

        Returns:
            A new ``Tenant`` instance with encrypted field values.
        """
        updates: dict[str, Any] = {}

        if tenant.database_url and not self.is_encrypted(tenant.database_url):
            updates["database_url"] = self.encrypt(tenant.database_url)
            logger.debug("Encrypted database_url for tenant %s", tenant.id)

        if tenant.metadata:
            new_meta = dict(tenant.metadata)
            for key, value in new_meta.items():
                if (
                    key.startswith(_ENC_METADATA_PREFIX)
                    and isinstance(value, str)
                    and not self.is_encrypted(value)
                ):
                    new_meta[key] = self.encrypt(value)
                    logger.debug("Encrypted metadata[%r] for tenant %s", key, tenant.id)

            if new_meta != tenant.metadata:
                updates["metadata"] = new_meta

        if updates:
            return tenant.model_copy(update=updates)
        return tenant

    def decrypt_tenant_fields(self, tenant: Any) -> Any:
        """Return a copy of *tenant* with sensitive fields decrypted.

        This is the inverse of :meth:`encrypt_tenant_fields`.  Call this
        after loading a tenant from the store to restore plaintext values
        before passing the tenant to application code.

        Args:
            tenant: A :class:`~fastapi_tenancy.core.types.Tenant` instance
                (possibly with encrypted field values).

        Returns:
            A new ``Tenant`` instance with decrypted field values.

        Raises:
            cryptography.fernet.InvalidToken: When a ciphertext is invalid.
        """
        updates: dict[str, Any] = {}

        if tenant.database_url and self.is_encrypted(tenant.database_url):
            updates["database_url"] = self.decrypt(tenant.database_url)
            logger.debug("Decrypted database_url for tenant %s", tenant.id)

        if tenant.metadata:
            new_meta = dict(tenant.metadata)
            changed = False
            for key, value in new_meta.items():
                if (
                    key.startswith(_ENC_METADATA_PREFIX)
                    and isinstance(value, str)
                    and self.is_encrypted(value)
                ):
                    new_meta[key] = self.decrypt(value)
                    changed = True

            if changed:
                updates["metadata"] = new_meta

        if updates:
            return tenant.model_copy(update=updates)
        return tenant


__all__ = ["TenancyEncryption"]
