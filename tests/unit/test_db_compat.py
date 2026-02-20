"""Unit tests — fastapi_tenancy.utils.db_compat

Coverage target: 100 %

Verified:
* DbDialect enum values and string coercion
* detect_dialect — all known URL schemes, uppercase, unknown, malformed
* supports_native_schemas — true only for PG and MSSQL
* supports_native_rls — true only for PG
* requires_static_pool — true only for SQLite
* get_set_tenant_sql — PG returns SQL, others return None
* get_schema_set_sql — PG returns SQL, others return None
* _sanitize_identifier — matches validation.sanitize_identifier output exactly
* make_table_prefix — prefix format, truncation, digit-start handling
"""

from __future__ import annotations

import pytest

from fastapi_tenancy.utils.db_compat import (
    DbDialect,
    _sanitize_identifier,
    detect_dialect,
    get_schema_set_sql,
    get_set_tenant_sql,
    make_table_prefix,
    requires_static_pool,
    supports_native_rls,
    supports_native_schemas,
)
from fastapi_tenancy.utils.validation import sanitize_identifier

pytestmark = pytest.mark.unit


# ───────────────────────────── DbDialect ─────────────────────────────────────


class TestDbDialect:
    def test_values(self):
        assert DbDialect.POSTGRESQL == "postgresql"
        assert DbDialect.SQLITE == "sqlite"
        assert DbDialect.MYSQL == "mysql"
        assert DbDialect.MSSQL == "mssql"
        assert DbDialect.UNKNOWN == "unknown"

    def test_str_coercion(self):
        assert str(DbDialect.POSTGRESQL) == "postgresql"

    def test_all_members(self):
        members = {d.value for d in DbDialect}
        assert members == {"postgresql", "sqlite", "mysql", "mssql", "unknown"}


# ──────────────────────────── detect_dialect ─────────────────────────────────


class TestDetectDialect:
    # PostgreSQL variants
    def test_postgresql_bare(self):
        assert detect_dialect("postgresql://user:pass@host/db") == DbDialect.POSTGRESQL

    def test_postgresql_asyncpg(self):
        assert detect_dialect("postgresql+asyncpg://user:pass@host/db") == DbDialect.POSTGRESQL

    def test_postgresql_psycopg(self):
        assert detect_dialect("postgresql+psycopg://user:pass@host/db") == DbDialect.POSTGRESQL

    def test_postgresql_psycopg2(self):
        assert detect_dialect("postgresql+psycopg2://user:pass@host/db") == DbDialect.POSTGRESQL

    def test_asyncpg_bare(self):
        assert detect_dialect("asyncpg://user:pass@host/db") == DbDialect.POSTGRESQL

    # SQLite variants
    def test_sqlite_bare(self):
        assert detect_dialect("sqlite:///./test.db") == DbDialect.SQLITE

    def test_sqlite_aiosqlite(self):
        assert detect_dialect("sqlite+aiosqlite:///./test.db") == DbDialect.SQLITE

    def test_sqlite_memory(self):
        assert detect_dialect("sqlite+aiosqlite:///:memory:") == DbDialect.SQLITE

    # MySQL variants
    def test_mysql_bare(self):
        assert detect_dialect("mysql://user:pass@host/db") == DbDialect.MYSQL

    def test_mysql_aiomysql(self):
        assert detect_dialect("mysql+aiomysql://user:pass@host/db") == DbDialect.MYSQL

    def test_mariadb(self):
        assert detect_dialect("mariadb://user:pass@host/db") == DbDialect.MYSQL

    # MSSQL
    def test_mssql_bare(self):
        assert detect_dialect("mssql://user:pass@host/db") == DbDialect.MSSQL

    def test_mssql_aioodbc(self):
        assert detect_dialect("mssql+aioodbc://user:pass@host/db") == DbDialect.MSSQL

    # Edge cases
    def test_unknown_scheme(self):
        assert detect_dialect("oracle://user:pass@host/db") == DbDialect.UNKNOWN

    def test_malformed_url_no_scheme(self):
        assert detect_dialect("just-a-string") == DbDialect.UNKNOWN

    def test_empty_string(self):
        assert detect_dialect("") == DbDialect.UNKNOWN

    def test_uppercase_scheme_normalised(self):
        # detect_dialect lowercases before matching
        assert detect_dialect("POSTGRESQL+ASYNCPG://host/db") == DbDialect.POSTGRESQL


# ─────────────────────── supports_native_schemas ─────────────────────────────


class TestSupportsNativeSchemas:
    def test_postgresql_true(self):
        assert supports_native_schemas(DbDialect.POSTGRESQL) is True

    def test_mssql_true(self):
        assert supports_native_schemas(DbDialect.MSSQL) is True

    def test_sqlite_false(self):
        assert supports_native_schemas(DbDialect.SQLITE) is False

    def test_mysql_false(self):
        assert supports_native_schemas(DbDialect.MYSQL) is False

    def test_unknown_false(self):
        assert supports_native_schemas(DbDialect.UNKNOWN) is False


# ───────────────────────── supports_native_rls ───────────────────────────────


class TestSupportsNativeRls:
    def test_postgresql_true(self):
        assert supports_native_rls(DbDialect.POSTGRESQL) is True

    def test_sqlite_false(self):
        assert supports_native_rls(DbDialect.SQLITE) is False

    def test_mysql_false(self):
        assert supports_native_rls(DbDialect.MYSQL) is False

    def test_mssql_false(self):
        assert supports_native_rls(DbDialect.MSSQL) is False

    def test_unknown_false(self):
        assert supports_native_rls(DbDialect.UNKNOWN) is False


# ─────────────────────────── requires_static_pool ────────────────────────────


class TestRequiresStaticPool:
    def test_sqlite_true(self):
        assert requires_static_pool(DbDialect.SQLITE) is True

    def test_postgresql_false(self):
        assert requires_static_pool(DbDialect.POSTGRESQL) is False

    def test_mysql_false(self):
        assert requires_static_pool(DbDialect.MYSQL) is False

    def test_mssql_false(self):
        assert requires_static_pool(DbDialect.MSSQL) is False

    def test_unknown_false(self):
        assert requires_static_pool(DbDialect.UNKNOWN) is False


# ─────────────────────────── get_set_tenant_sql ──────────────────────────────


class TestGetSetTenantSql:
    def test_postgresql_returns_sql(self):
        sql = get_set_tenant_sql(DbDialect.POSTGRESQL)
        assert sql is not None
        assert ":tenant_id" in sql

    def test_sqlite_returns_none(self):
        assert get_set_tenant_sql(DbDialect.SQLITE) is None

    def test_mysql_returns_none(self):
        assert get_set_tenant_sql(DbDialect.MYSQL) is None

    def test_mssql_returns_none(self):
        assert get_set_tenant_sql(DbDialect.MSSQL) is None

    def test_unknown_returns_none(self):
        assert get_set_tenant_sql(DbDialect.UNKNOWN) is None


# ─────────────────────────── get_schema_set_sql ──────────────────────────────


class TestGetSchemaSetSql:
    def test_postgresql_returns_sql(self):
        sql = get_schema_set_sql(DbDialect.POSTGRESQL)
        assert sql is not None
        assert ":schema" in sql or "search_path" in sql.lower()

    def test_sqlite_returns_none(self):
        assert get_schema_set_sql(DbDialect.SQLITE) is None

    def test_mysql_returns_none(self):
        assert get_schema_set_sql(DbDialect.MYSQL) is None

    def test_mssql_returns_none(self):
        assert get_schema_set_sql(DbDialect.MSSQL) is None


# ─────────────────── _sanitize_identifier parity check ───────────────────────


class TestSanitizeIdentifierParity:
    """The private copy in db_compat must be identical to validation.sanitize_identifier."""

    CASES = [
        "acme-corp",
        "2fast",
        "A B C",
        "hello@world!",
        "my.company",
        "---",
        "already_clean",
        "a" * 100,
        "café",
    ]

    @pytest.mark.parametrize("identifier", CASES)
    def test_parity_with_validation_module(self, identifier: str):
        assert _sanitize_identifier(identifier) == sanitize_identifier(identifier)


# ──────────────────────────── make_table_prefix ──────────────────────────────


class TestMakeTablePrefix:
    def test_basic(self):
        prefix = make_table_prefix("acme-corp")
        assert prefix.startswith("t_")
        assert prefix.endswith("_")

    def test_dot_in_slug(self):
        prefix = make_table_prefix("my.company")
        assert prefix.startswith("t_")
        assert prefix.endswith("_")

    def test_prefix_is_short(self):
        # Prefix should be short enough to allow a 40-char table name
        prefix = make_table_prefix("a-very-long-tenant-identifier-that-might-overflow")
        assert len(prefix) <= 25, f"Prefix too long: {prefix!r}"

    def test_no_double_t_prefix(self):
        # Slug starting with digit gets "t_" prepended by sanitize
        # make_table_prefix should not double it
        prefix = make_table_prefix("2-digit-start")
        assert not prefix.startswith("t_t_")

    def test_all_lowercase(self):
        prefix = make_table_prefix("UPPER-CASE")
        assert prefix == prefix.lower()

    def test_ends_with_underscore(self):
        for slug in ["acme", "my-corp", "tenant-42"]:
            assert make_table_prefix(slug).endswith("_")
