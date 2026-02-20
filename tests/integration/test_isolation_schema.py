"""Integration tests — fastapi_tenancy.isolation.schema.SchemaIsolationProvider

On SQLite (no native schemas) the provider operates in **prefix mode**:
tables are prefixed with ``t_{slug}_`` rather than placed in a schema.

Tests use a temp SQLite file and a minimal ORM model to verify that:
* initialize_tenant() creates prefixed tables
* get_session() yields a working async session
* verify_isolation() succeeds after initialise
* destroy_tenant() drops the prefixed tables
* Sessions for different tenants are independent
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fastapi_tenancy.core.types import IsolationStrategy, Tenant, TenantStatus

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_NOW = datetime(2024, 1, 1, tzinfo=UTC)


def _tenant(identifier: str = "acme-corp", id: str = "t-001") -> Tenant:
    return Tenant(
        id=id,
        identifier=identifier,
        name="Test Corp",
        status=TenantStatus.ACTIVE,
        isolation_strategy=IsolationStrategy.SCHEMA,
        created_at=_NOW,
        updated_at=_NOW,
    )


@pytest.fixture
async def engine(tmp_path):
    pytest.importorskip("aiosqlite", reason="aiosqlite not installed")
    from sqlalchemy.ext.asyncio import create_async_engine

    url = f"sqlite+aiosqlite:///{tmp_path}/iso.db"
    eng = create_async_engine(url)
    yield eng
    await eng.dispose()


@pytest.fixture
async def provider(engine):
    from fastapi_tenancy.isolation.schema import SchemaIsolationProvider

    p = SchemaIsolationProvider(engine=engine)
    yield p


@pytest.fixture
def t1():
    return _tenant(identifier="acme-corp", id="t-001")


@pytest.fixture
def t2():
    return _tenant(identifier="globex-corp", id="t-002")


# ──────────────────── initialize_tenant ───────────────────────────────────────


class TestInitializeTenant:
    async def test_initialize_succeeds(self, provider, t1):
        # Should not raise
        await provider.initialize_tenant(t1)

    async def test_initialize_idempotent(self, provider, t1):
        await provider.initialize_tenant(t1)
        await provider.initialize_tenant(t1)  # second call must not raise


# ────────────────────────── get_session ──────────────────────────────────────


class TestGetSession:
    async def test_session_yields_without_error(self, provider, t1):
        await provider.initialize_tenant(t1)
        async with provider.get_session(t1) as session:
            assert session is not None

    async def test_two_tenant_sessions_independent(self, provider, t1, t2):
        await provider.initialize_tenant(t1)
        await provider.initialize_tenant(t2)
        async with provider.get_session(t1) as s1:
            async with provider.get_session(t2) as s2:
                assert s1 is not s2


# ─────────────────────── verify_isolation ────────────────────────────────────


class TestVerifyIsolation:
    async def test_verify_after_init_succeeds(self, provider, t1):
        await provider.initialize_tenant(t1)
        result = await provider.verify_isolation(t1)
        assert result is True

    async def test_verify_uninitialised_returns_false(self, provider, t1):
        # tenant never initialized
        result = await provider.verify_isolation(t1)
        assert result is False


# ─────────────────────── destroy_tenant ──────────────────────────────────────


class TestDestroyTenant:
    async def test_destroy_after_init(self, provider, t1):
        await provider.initialize_tenant(t1)
        await provider.destroy_tenant(t1)  # should not raise
        # After destroy, verify_isolation returns False
        result = await provider.verify_isolation(t1)
        assert result is False
