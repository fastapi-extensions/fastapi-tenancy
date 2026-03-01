"""Tests for fastapi_tenancy.core.config — TenancyConfig validation and helpers."""

from __future__ import annotations

import warnings

import pytest

from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.types import IsolationStrategy, ResolutionStrategy


SQLITE_URL = "sqlite+aiosqlite:///:memory:"


def make_config(**kwargs) -> TenancyConfig:
    return TenancyConfig(database_url=SQLITE_URL, **kwargs)


class TestTenancyConfigDefaults:
    def test_default_strategies(self):
        c = make_config()
        assert c.resolution_strategy == ResolutionStrategy.HEADER
        assert c.isolation_strategy == IsolationStrategy.SCHEMA

    def test_default_pool_settings(self):
        c = make_config()
        assert c.database_pool_size == 20
        assert c.database_max_overflow == 40
        assert c.database_pool_timeout == 30
        assert c.database_pool_recycle == 3600
        assert c.database_pool_pre_ping is True
        assert c.database_echo is False

    def test_default_cache_settings(self):
        c = make_config()
        assert c.cache_enabled is False
        assert c.redis_url is None
        assert c.cache_ttl == 3600

    def test_default_rate_limiting(self):
        c = make_config()
        assert c.enable_rate_limiting is False
        assert c.rate_limit_per_minute == 100
        assert c.rate_limit_window_seconds == 60

    def test_default_header_name(self):
        c = make_config()
        assert c.tenant_header_name == "X-Tenant-ID"

    def test_default_schema_prefix(self):
        c = make_config()
        assert c.schema_prefix == "tenant_"

    def test_default_public_schema(self):
        c = make_config()
        assert c.public_schema == "public"

    def test_default_soft_delete(self):
        c = make_config()
        assert c.enable_soft_delete is True

    def test_default_max_tenants(self):
        c = make_config()
        assert c.max_tenants is None


class TestDatabaseUrlValidation:
    def test_valid_async_url(self):
        c = make_config()
        assert "sqlite+aiosqlite" in c.database_url

    def test_sync_url_warns(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            TenancyConfig(database_url="sqlite:///test.db")
            assert any("synchronous driver" in str(warning.message) for warning in w)

    def test_trailing_slash_stripped(self):
        c = TenancyConfig(database_url="sqlite+aiosqlite:///:memory:/")
        assert not c.database_url.endswith("/")


class TestJwtSecretValidation:
    def test_jwt_required_when_strategy_jwt(self):
        with pytest.raises(Exception, match="jwt_secret"):
            TenancyConfig(
                database_url=SQLITE_URL,
                resolution_strategy=ResolutionStrategy.JWT,
                # no jwt_secret
            )

    def test_jwt_secret_too_short(self):
        with pytest.raises(Exception, match="32 characters"):
            TenancyConfig(
                database_url=SQLITE_URL,
                resolution_strategy=ResolutionStrategy.JWT,
                jwt_secret="short",
            )

    def test_valid_jwt_config(self):
        c = TenancyConfig(
            database_url=SQLITE_URL,
            resolution_strategy=ResolutionStrategy.JWT,
            jwt_secret="a" * 32,
        )
        assert c.jwt_secret == "a" * 32

    def test_jwt_secret_none_ok_for_non_jwt_strategy(self):
        c = make_config()
        assert c.jwt_secret is None


class TestDomainSuffixValidation:
    def test_domain_suffix_required_for_subdomain(self):
        with pytest.raises(Exception, match="domain_suffix"):
            TenancyConfig(
                database_url=SQLITE_URL,
                resolution_strategy=ResolutionStrategy.SUBDOMAIN,
            )

    def test_valid_subdomain_config(self):
        c = TenancyConfig(
            database_url=SQLITE_URL,
            resolution_strategy=ResolutionStrategy.SUBDOMAIN,
            domain_suffix=".example.com",
        )
        assert c.domain_suffix == ".example.com"


class TestEncryptionKeyValidation:
    def test_key_required_when_encryption_enabled(self):
        with pytest.raises(Exception, match="encryption_key"):
            TenancyConfig(database_url=SQLITE_URL, enable_encryption=True)

    def test_key_too_short(self):
        with pytest.raises(Exception, match="32 characters"):
            TenancyConfig(
                database_url=SQLITE_URL,
                enable_encryption=True,
                encryption_key="tooshort",
            )

    def test_valid_encryption_config(self):
        c = TenancyConfig(
            database_url=SQLITE_URL,
            enable_encryption=True,
            encryption_key="x" * 32,
        )
        assert c.encryption_key == "x" * 32


class TestSchemaPrefixValidation:
    def test_valid_prefix(self):
        c = TenancyConfig(database_url=SQLITE_URL, schema_prefix="t_")
        assert c.schema_prefix == "t_"

    def test_invalid_prefix_starts_with_digit(self):
        with pytest.raises(Exception):
            TenancyConfig(database_url=SQLITE_URL, schema_prefix="1bad")

    def test_invalid_prefix_uppercase(self):
        with pytest.raises(Exception):
            TenancyConfig(database_url=SQLITE_URL, schema_prefix="Bad_")

    def test_invalid_prefix_hyphen(self):
        with pytest.raises(Exception):
            TenancyConfig(database_url=SQLITE_URL, schema_prefix="bad-prefix")


class TestCrossFieldConsistency:
    def test_cache_requires_redis_url(self):
        with pytest.raises(Exception, match="redis_url"):
            TenancyConfig(database_url=SQLITE_URL, cache_enabled=True)

    def test_rate_limiting_requires_redis_url(self):
        with pytest.raises(Exception, match="redis_url"):
            TenancyConfig(database_url=SQLITE_URL, enable_rate_limiting=True)

    def test_hybrid_requires_distinct_strategies(self):
        with pytest.raises(Exception, match="different strategies"):
            TenancyConfig(
                database_url=SQLITE_URL,
                isolation_strategy=IsolationStrategy.HYBRID,
                premium_isolation_strategy=IsolationStrategy.SCHEMA,
                standard_isolation_strategy=IsolationStrategy.SCHEMA,  # same!
            )

    def test_valid_hybrid_config(self):
        c = TenancyConfig(
            database_url=SQLITE_URL,
            isolation_strategy=IsolationStrategy.HYBRID,
            premium_isolation_strategy=IsolationStrategy.SCHEMA,
            standard_isolation_strategy=IsolationStrategy.RLS,
        )
        assert c.isolation_strategy == IsolationStrategy.HYBRID

    def test_database_isolation_requires_url_template(self):
        with pytest.raises(Exception, match="database_url_template"):
            TenancyConfig(
                database_url=SQLITE_URL,
                isolation_strategy=IsolationStrategy.DATABASE,
            )

    def test_database_isolation_template_without_placeholder(self):
        with pytest.raises(Exception, match="placeholder"):
            TenancyConfig(
                database_url=SQLITE_URL,
                isolation_strategy=IsolationStrategy.DATABASE,
                database_url_template="sqlite+aiosqlite:///no_placeholder.db",
            )

    def test_valid_database_isolation(self):
        c = TenancyConfig(
            database_url=SQLITE_URL,
            isolation_strategy=IsolationStrategy.DATABASE,
            database_url_template="sqlite+aiosqlite:///{tenant_id}.db",
        )
        assert c.database_url_template is not None

    def test_valid_cache_config(self):
        c = TenancyConfig(
            database_url=SQLITE_URL,
            cache_enabled=True,
            redis_url="redis://localhost:6379/0",
        )
        assert c.cache_enabled is True


class TestHelperMethods:
    def test_get_schema_name(self):
        c = make_config(schema_prefix="tenant_")
        assert c.get_schema_name("acme-corp") == "tenant_acme_corp"
        assert c.get_schema_name("globex") == "tenant_globex"

    def test_get_schema_name_invalid_identifier(self):
        c = make_config()
        with pytest.raises(ValueError, match="Invalid tenant identifier"):
            c.get_schema_name("-invalid-")

    def test_get_database_url_for_tenant_with_template(self):
        c = TenancyConfig(
            database_url=SQLITE_URL,
            isolation_strategy=IsolationStrategy.DATABASE,
            database_url_template="postgresql+asyncpg://user:pass@host/{database_name}",
        )
        url = c.get_database_url_for_tenant("tenant-abc123")
        assert "tenant_abc123" in url

    def test_get_database_url_for_tenant_no_template(self):
        c = make_config()
        # Without template, falls back to the base database_url
        url = c.get_database_url_for_tenant("t1")
        assert url == SQLITE_URL

    def test_is_premium_tenant(self):
        c = TenancyConfig(
            database_url=SQLITE_URL,
            isolation_strategy=IsolationStrategy.HYBRID,
            premium_tenants=["t-premium1", "t-premium2"],
            premium_isolation_strategy=IsolationStrategy.SCHEMA,
            standard_isolation_strategy=IsolationStrategy.RLS,
        )
        assert c.is_premium_tenant("t-premium1") is True
        assert c.is_premium_tenant("t-standard") is False

    def test_is_premium_tenant_empty_list(self):
        c = make_config()
        assert c.is_premium_tenant("any-tenant") is False

    def test_get_isolation_strategy_non_hybrid(self):
        c = make_config(isolation_strategy=IsolationStrategy.SCHEMA)
        assert c.get_isolation_strategy_for_tenant("any") == IsolationStrategy.SCHEMA

    def test_get_isolation_strategy_hybrid_premium(self):
        c = TenancyConfig(
            database_url=SQLITE_URL,
            isolation_strategy=IsolationStrategy.HYBRID,
            premium_tenants=["premium-1"],
            premium_isolation_strategy=IsolationStrategy.SCHEMA,
            standard_isolation_strategy=IsolationStrategy.RLS,
        )
        assert c.get_isolation_strategy_for_tenant("premium-1") == IsolationStrategy.SCHEMA
        assert c.get_isolation_strategy_for_tenant("standard-1") == IsolationStrategy.RLS

    def test_str_masks_password(self):
        c = TenancyConfig(database_url="postgresql+asyncpg://user:supersecret@host/db")
        text = str(c)
        assert "supersecret" not in text
        assert "***" in text


class TestPremiumSetBuilt:
    def test_premium_set_is_frozenset(self):
        c = TenancyConfig(
            database_url=SQLITE_URL,
            isolation_strategy=IsolationStrategy.HYBRID,
            premium_tenants=["a", "b", "c"],
            premium_isolation_strategy=IsolationStrategy.SCHEMA,
            standard_isolation_strategy=IsolationStrategy.RLS,
        )
        assert isinstance(c._premium_set, frozenset)
        assert "a" in c._premium_set
        assert "d" not in c._premium_set
