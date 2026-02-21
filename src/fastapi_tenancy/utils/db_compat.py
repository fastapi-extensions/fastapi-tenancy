"""Database dialect detection and capability matrix.

This module maps SQLAlchemy connection-URL schemes to a canonical ``DbDialect``
enum and exposes predicate functions that isolation providers use to select the
correct SQL dialect at runtime.

Supported dialects and their capabilities
-----------------------------------------

+------------+----------------+----------+---------------------+
| Dialect    | Native schemas | RLS      | Pool type           |
+============+================+==========+=====================+
| PostgreSQL | ✓              | ✓        | QueuePool           |
+------------+----------------+----------+---------------------+
| SQLite     | ✗              | ✗        | StaticPool (memory) |
+------------+----------------+----------+---------------------+
| MySQL      | ✗ (= DATABASE) | ✗        | QueuePool           |
+------------+----------------+----------+---------------------+
| MSSQL      | ✓ (partial)    | ✗        | QueuePool           |
+------------+----------------+----------+---------------------+
| Unknown    | ✗              | ✗        | QueuePool           |
+------------+----------------+----------+---------------------+

Design note
-----------
``_sanitize_identifier`` is intentionally self-contained (not imported from
``utils.validation``) to avoid a circular import:  ``db_compat`` is imported
by ``config.py`` (via the ``database_url`` validator), which is imported before
``utils.validation`` may be fully initialised in some import orders.  A unit
test enforces that both copies produce identical output for all inputs.
"""

from __future__ import annotations

from enum import StrEnum
import re

#######################
# Dialect enumeration #
#######################


class DbDialect(StrEnum):
    """Canonical database dialect families recognised by fastapi-tenancy."""

    POSTGRESQL = "postgresql"
    SQLITE = "sqlite"
    MYSQL = "mysql"
    MSSQL = "mssql"
    UNKNOWN = "unknown"


################################
# URL scheme → dialect mapping #
################################

_DIALECT_MAP: dict[str, DbDialect] = {
    # PostgreSQL
    "postgresql": DbDialect.POSTGRESQL,
    "postgresql+asyncpg": DbDialect.POSTGRESQL,
    "postgresql+psycopg": DbDialect.POSTGRESQL,
    "postgresql+psycopg2": DbDialect.POSTGRESQL,
    "asyncpg": DbDialect.POSTGRESQL,
    # SQLite
    "sqlite": DbDialect.SQLITE,
    "sqlite+aiosqlite": DbDialect.SQLITE,
    "aiosqlite": DbDialect.SQLITE,
    # MySQL / MariaDB
    "mysql": DbDialect.MYSQL,
    "mysql+aiomysql": DbDialect.MYSQL,
    "mysql+asyncmy": DbDialect.MYSQL,
    "mariadb": DbDialect.MYSQL,
    "mariadb+aiomysql": DbDialect.MYSQL,
    # Microsoft SQL Server
    "mssql": DbDialect.MSSQL,
    "mssql+aioodbc": DbDialect.MSSQL,
}

_SCHEME_RE = re.compile(r"^([a-z][a-z0-9+]*?)://", re.IGNORECASE)


def detect_dialect(database_url: str) -> DbDialect:
    """Infer the ``DbDialect`` from a SQLAlchemy connection URL.

    Only the scheme portion (up to the first ``://``) is examined.  Unknown
    or malformed URLs return ``DbDialect.UNKNOWN``.

    Args:
        database_url: A fully-qualified SQLAlchemy connection URL string.

    Returns:
        The matched ``DbDialect`` enum member.

    Examples::

        detect_dialect("postgresql+asyncpg://user:pass@host/db")
        # → DbDialect.POSTGRESQL

        detect_dialect("sqlite+aiosqlite:///./test.db")
        # → DbDialect.SQLITE
    """
    match = _SCHEME_RE.match(database_url.lower().strip())
    if not match:
        return DbDialect.UNKNOWN
    return _DIALECT_MAP.get(match.group(1), DbDialect.UNKNOWN)


#########################
# Capability predicates #
#########################


def supports_native_schemas(dialect: DbDialect) -> bool:
    """Return ``True`` if *dialect* supports ``CREATE SCHEMA`` + search path.

    Only PostgreSQL and MSSQL provide true schema-level isolation without
    requiring a separate database per tenant.

    Args:
        dialect: The database dialect to test.

    Returns:
        ``True`` for ``POSTGRESQL`` and ``MSSQL``.

    Example::

        supports_native_schemas(DbDialect.POSTGRESQL)  # True
        supports_native_schemas(DbDialect.SQLITE)      # False
    """
    return dialect in (DbDialect.POSTGRESQL, DbDialect.MSSQL)


def supports_native_rls(dialect: DbDialect) -> bool:
    """Return ``True`` if *dialect* supports server-side Row-Level Security.

    Currently only PostgreSQL exposes a production-ready RLS implementation
    compatible with ``SET app.current_tenant``.

    Args:
        dialect: The database dialect to test.

    Returns:
        ``True`` only for ``POSTGRESQL``.
    """
    return dialect == DbDialect.POSTGRESQL


def requires_static_pool(dialect: DbDialect) -> bool:
    """Return ``True`` if *dialect* requires SQLAlchemy's ``StaticPool``.

    SQLite in-memory databases exist only on a single underlying connection.
    ``StaticPool`` forces SQLAlchemy to reuse that connection rather than
    creating a new one for each checkout, which would produce an empty
    database every time.

    Args:
        dialect: The database dialect to test.

    Returns:
        ``True`` only for ``SQLITE``.
    """
    return dialect == DbDialect.SQLITE


def get_set_tenant_sql(dialect: DbDialect) -> str | None:
    """Return SQL that sets the current-tenant session variable.

    The returned statement uses ``:tenant_id`` as a bind-parameter
    placeholder.  Callers supply the value via ``{"tenant_id": value}``.

    Returns ``None`` for dialects with no equivalent mechanism; those callers
    should fall back to explicit ``WHERE tenant_id = :id`` filtering.

    Args:
        dialect: Target database dialect.

    Returns:
        A parameterised SQL string, or ``None`` if unsupported.

    Notes:
        - **PostgreSQL** — ``SET app.current_tenant = :tenant_id`` activates
          RLS policies referencing ``current_setting('app.current_tenant')``.
        - **MySQL / MSSQL / SQLite** — no equivalent; use explicit WHERE.
    """
    if dialect == DbDialect.POSTGRESQL:
        return "SET LOCAL app.current_tenant = :tenant_id"
    return None


def get_schema_set_sql(dialect: DbDialect) -> str | None:
    """Return SQL that sets the search path to a named schema.

    The returned statement uses ``:schema`` as a placeholder.

    Args:
        dialect: Target database dialect.

    Returns:
        A parameterised SQL string, or ``None`` if unsupported.

    Note:
        PostgreSQL's ``SET`` command does **not** support bound parameters in
        the SQL protocol.  Callers must validate the schema name with
        ``assert_safe_schema_name`` before interpolating it as a literal.
    """
    if dialect == DbDialect.POSTGRESQL:
        return "SET LOCAL search_path TO {schema}, public"
    return None


############################################################################
# Identifier helpers (self-contained — see module docstring for rationale) #
############################################################################


def _sanitize_identifier(identifier: str) -> str:
    """Convert an arbitrary string to a safe PostgreSQL identifier.

    This is a private copy of ``fastapi_tenancy.utils.validation.sanitize_identifier``
    kept in sync to avoid a circular import.  A unit test verifies that both
    functions produce identical output for all inputs.

    Args:
        identifier: Raw input string.

    Returns:
        A sanitised, lowercase, underscore-delimited identifier (≤ 63 chars).
    """
    s = identifier.lower().replace("-", "_").replace(".", "_")
    s = re.sub(r"[^a-z0-9_]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if s and not s[0].isalpha():
        s = f"t_{s}"
    return (s or "tenant")[:63]


def make_table_prefix(tenant_identifier: str) -> str:
    """Build a safe table-name prefix for dialects without native schema support.

    Used by ``SchemaIsolationProvider`` in prefix mode (SQLite, unknown dialects).
    The prefix is kept short so that ``prefix + table_name`` fits within the
    63-character identifier limit common to most SQL databases.

    Args:
        tenant_identifier: The tenant's slug (e.g. ``"acme-corp"``).

    Returns:
        A short prefix string ending with an underscore (e.g. ``"t_acme_corp_"``).

    Examples::

        make_table_prefix("acme-corp")   # "t_acme_corp_"
        make_table_prefix("my.company")  # "t_my_company_"
    """
    safe = _sanitize_identifier(tenant_identifier)
    # Strip a leading "t_" so we never produce "t_t_..." when the slug
    # started with a digit (which _sanitize_identifier prepends "t_" to).
    base = safe[2:] if safe.startswith("t_") else safe
    base = (base or safe)[:20].rstrip("_")
    return f"t_{base}_"


__all__ = [
    "DbDialect",
    "detect_dialect",
    "get_schema_set_sql",
    "get_set_tenant_sql",
    "make_table_prefix",
    "requires_static_pool",
    "supports_native_rls",
    "supports_native_schemas",
]
