"""Unit tests — fastapi_tenancy.core.config

Coverage target: 100 %

Verified:
* Construction with required database_url
* Default field values match spec
* Resolution + isolation strategy fields
* Cross-field validators (SUBDOMAIN needs domain_suffix, JWT needs jwt_secret)
* Secret masking in __str__
* Env-prefix "TENANCY_" settings (monkeypatched)
* Numeric field bounds (ge/le validators)
* helper methods: schema_name_for(), database_name_for(), tenant_db_url()
"""

from __future__ import annotations

import os

import pytest

from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.types import IsolationStrategy, ResolutionStrategy

pytestmark = pytest.mark.unit

_SQLITE_URL = "sqlite+aiosqlite:///:memory:"


def make_cfg(**kw) -> TenancyConfig:
    defaults = {"database_url": _SQLITE_URL}
    defaults.update(kw)
    return TenancyConfig(**defaults)


# ─────────────────────────── Required fields ─────────────────────────────────


class TestRequiredFields:
    def test_database_url_required(self):
        with pytest.raises(Exception):
            TenancyConfig()  # missing database_url

    def test_minimal_construction(self):
        cfg = make_cfg()
        assert cfg.database_url == _SQLITE_URL


# ────────────────────────────── Defaults ─────────────────────────────────────


class TestDefaults:
    def test_resolution_strategy_default(self):
        assert make_cfg().resolution_strategy == ResolutionStrategy.HEADER

    def test_isolation_strategy_default(self):
        assert make_cfg().isolation_strategy == IsolationStrategy.SCHEMA

    def test_database_pool_size_default(self):
        assert make_cfg().database_pool_size == 20

    def test_database_max_overflow_default(self):
        assert make_cfg().database_max_overflow == 40

    def test_database_pool_timeout_default(self):
        assert make_cfg().database_pool_timeout == 30

    def test_database_pool_recycle_default(self):
        assert make_cfg().database_pool_recycle == 3600

    def test_database_echo_false(self):
        assert make_cfg().database_echo is False

    def test_tenant_header_name_default(self):
        cfg = make_cfg()
        assert cfg.tenant_header_name == "X-Tenant-ID"

    def test_path_prefix_default(self):
        cfg = make_cfg()
        assert cfg.path_prefix == "/tenants"

    def test_domain_suffix_none(self):
        assert make_cfg().domain_suffix is None

    def test_jwt_secret_none(self):
        assert make_cfg().jwt_secret is None

    def test_cache_enabled_false(self):
        assert make_cfg().cache_enabled is False

    def test_debug_false(self):
        assert make_cfg().debug is False


# ─────────────────────────── Strategy fields ─────────────────────────────────


class TestStrategyFields:
    def test_set_resolution_subdomain(self):
        cfg = make_cfg(
            resolution_strategy=ResolutionStrategy.SUBDOMAIN,
            domain_suffix=".example.com",
        )
        assert cfg.resolution_strategy == ResolutionStrategy.SUBDOMAIN

    def test_set_isolation_rls(self):
        cfg = make_cfg(isolation_strategy=IsolationStrategy.RLS)
        assert cfg.isolation_strategy == IsolationStrategy.RLS

    def test_set_isolation_database(self):
        cfg = make_cfg(isolation_strategy=IsolationStrategy.DATABASE)
        assert cfg.isolation_strategy == IsolationStrategy.DATABASE


# ──────────────────────── Cross-field validators ──────────────────────────────


class TestCrossFieldValidators:
    def test_subdomain_strategy_requires_domain_suffix(self):
        with pytest.raises(Exception):
            make_cfg(
                resolution_strategy=ResolutionStrategy.SUBDOMAIN,
                domain_suffix=None,
            )

    def test_jwt_strategy_requires_secret(self):
        with pytest.raises(Exception):
            make_cfg(
                resolution_strategy=ResolutionStrategy.JWT,
                jwt_secret=None,
            )

    def test_jwt_strategy_short_secret_raises(self):
        with pytest.raises(Exception):
            make_cfg(
                resolution_strategy=ResolutionStrategy.JWT,
                jwt_secret="short",
            )

    def test_jwt_valid_with_long_secret(self):
        cfg = make_cfg(
            resolution_strategy=ResolutionStrategy.JWT,
            jwt_secret="a" * 32,
        )
        assert cfg.resolution_strategy == ResolutionStrategy.JWT


# ─────────────────────────── Secret masking ──────────────────────────────────


class TestSecretMasking:
    def test_str_masks_connection_password(self):
        cfg = make_cfg(database_url="postgresql+asyncpg://user:supersecret@host/db")
        s = str(cfg)
        assert "supersecret" not in s

    def test_str_masks_jwt_secret(self):
        cfg = make_cfg(
            resolution_strategy=ResolutionStrategy.JWT,
            jwt_secret="a" * 32,
        )
        s = str(cfg)
        # jwt_secret value should not appear literally
        assert "a" * 32 not in s


# ─────────────────── Numeric bounds validation ───────────────────────────────


class TestNumericBounds:
    def test_pool_size_ge_1(self):
        with pytest.raises(Exception):
            make_cfg(database_pool_size=0)

    def test_pool_size_le_100(self):
        with pytest.raises(Exception):
            make_cfg(database_pool_size=101)

    def test_max_overflow_ge_0(self):
        with pytest.raises(Exception):
            make_cfg(database_max_overflow=-1)

    def test_pool_timeout_ge_1(self):
        with pytest.raises(Exception):
            make_cfg(database_pool_timeout=0)


# ─────────────────────────── Helper methods ──────────────────────────────────


class TestHelperMethods:
    def test_schema_name_for(self):
        cfg = make_cfg()
        name = cfg.schema_name_for("acme-corp")
        assert isinstance(name, str)
        assert len(name) > 0
        # Should be safe for SQL (lowercase, underscores)
        assert name == name.lower()
        assert " " not in name

    def test_database_name_for(self):
        cfg = make_cfg()
        name = cfg.database_name_for("acme-corp")
        assert isinstance(name, str)
        assert len(name) > 0
        assert name == name.lower()

    def test_tenant_db_url_with_template(self):
        cfg = make_cfg(
            database_url_template="postgresql+asyncpg://user:pass@host/{database_name}"
        )
        url = cfg.tenant_db_url("acme-corp")
        assert "acme" in url or "tenant" in url

    def test_tenant_db_url_no_template_returns_none(self):
        cfg = make_cfg()
        assert cfg.tenant_db_url("acme-corp") is None


# ─────────────────────────── Env override ────────────────────────────────────


class TestEnvOverride:
    def test_env_prefix_tenancy(self, monkeypatch):
        monkeypatch.setenv("TENANCY_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
        monkeypatch.setenv("TENANCY_DEBUG", "true")
        cfg = TenancyConfig()
        assert cfg.database_url == "sqlite+aiosqlite:///:memory:"
        assert cfg.debug is True

    def test_env_resolution_strategy(self, monkeypatch):
        monkeypatch.setenv("TENANCY_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
        monkeypatch.setenv("TENANCY_RESOLUTION_STRATEGY", "path")
        cfg = TenancyConfig()
        assert cfg.resolution_strategy == ResolutionStrategy.PATH
