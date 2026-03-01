"""Internal identifier sanitisation — shared by db_compat and validation.

This module exists solely to break the duplication between
``fastapi_tenancy.utils.db_compat`` and
``fastapi_tenancy.utils.validation``.

Why a separate internal module?
--------------------------------
``db_compat`` is imported early in the import chain (by ``config.py`` via the
``database_url`` field validator) — before ``utils.validation`` may be fully
initialised in some import orders.  Having ``db_compat`` import from
``validation`` would create a circular import.

Conversely, ``validation`` previously imported nothing from ``db_compat``.

The clean solution is a tiny, dependency-free ``_sanitize`` module that both
can import without creating any cycle.

This module is **private** (prefixed with ``_``) and not part of the public
API.  Do not import it from application code.
"""

from __future__ import annotations

import re

__all__ = ["core_sanitize_identifier"]


def core_sanitize_identifier(identifier: str) -> str:
    """Convert an arbitrary string to a safe, lowercase PostgreSQL identifier.

    Transformation rules (applied in order):
        1. Lowercase.
        2. Hyphens and dots replaced with underscores.
        3. All remaining non-alphanumeric, non-underscore characters replaced
           with underscores.
        4. Consecutive underscores collapsed to one.
        5. Leading/trailing underscores stripped.
        6. If the first character is a digit, prepend ``"t_"``.
        7. Truncate to 63 characters (PostgreSQL identifier limit).
        8. Fall back to ``"tenant"`` for empty results.

    Args:
        identifier: Raw input string (e.g. a tenant slug or UUID).

    Returns:
        A valid PostgreSQL identifier derived from *identifier*.

    Examples::

        core_sanitize_identifier("acme-corp")   # "acme_corp"
        core_sanitize_identifier("2fast")       # "t_2fast"
        core_sanitize_identifier("A B C")       # "a_b_c"
    """
    s = identifier.lower().replace("-", "_").replace(".", "_")
    s = re.sub(r"[^a-z0-9_]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if s and not s[0].isalpha():
        s = f"t_{s}"
    return (s or "tenant")[:63]
