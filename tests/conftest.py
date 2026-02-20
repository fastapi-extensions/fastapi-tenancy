"""Root conftest.py — shared fixtures, helpers, and pytest configuration.

Scope hierarchy used across the suite
--------------------------------------
* ``session``   — built once per pytest session (expensive engines, app instances)
* ``module``    — built once per test module
* ``function``  — built fresh for each test (default; safe for mutating stores)

Async mode
----------
``asyncio_mode = "auto"`` in pyproject.toml means every ``async def test_*``
is automatically treated as an async test.  No ``@pytest.mark.asyncio``
needed anywhere.

Available markers (declared in pyproject.toml)
-----------------------------------------------
* ``unit``        — pure Python, no I/O, sub-millisecond
* ``integration`` — uses SQLite / mocks, no external services
* ``e2e``         — full FastAPI TestClient / full request cycles
* ``slow``        — > 1 second
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Make the rewrite src importable regardless of cwd
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------

from fastapi_tenancy.core.types import (
    IsolationStrategy,
    ResolutionStrategy,
    Tenant,
    TenantStatus,
)


def _now() -> datetime:
    return datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)


def build_tenant(
    *,
    id: str = "t-001",
    identifier: str = "acme-corp",
    name: str = "Acme Corporation",
    status: TenantStatus = TenantStatus.ACTIVE,
    metadata: dict[str, Any] | None = None,
    isolation_strategy: IsolationStrategy | None = None,
    database_url: str | None = None,
    schema_name: str | None = None,
) -> Tenant:
    """Build a :class:`Tenant` with sensible defaults."""
    return Tenant(
        id=id,
        identifier=identifier,
        name=name,
        status=status,
        metadata=metadata or {},
        isolation_strategy=isolation_strategy,
        database_url=database_url,
        schema_name=schema_name,
        created_at=_now(),
        updated_at=_now(),
    )


# ---------------------------------------------------------------------------
# Tenant fixtures — one per status + extras
# ---------------------------------------------------------------------------


@pytest.fixture
def t_active() -> Tenant:
    return build_tenant()


@pytest.fixture
def t_suspended() -> Tenant:
    return build_tenant(id="t-002", identifier="suspended-co", status=TenantStatus.SUSPENDED)


@pytest.fixture
def t_deleted() -> Tenant:
    return build_tenant(id="t-003", identifier="deleted-org", status=TenantStatus.DELETED)


@pytest.fixture
def t_provisioning() -> Tenant:
    return build_tenant(id="t-004", identifier="new-startup", status=TenantStatus.PROVISIONING)


@pytest.fixture
def t_second() -> Tenant:
    return build_tenant(id="t-005", identifier="globex-corp", name="Globex Corp")


@pytest.fixture
def t_with_meta() -> Tenant:
    return build_tenant(
        id="t-006",
        identifier="meta-tenant",
        metadata={"plan": "pro", "max_users": 100, "features_enabled": ["sso", "audit"]},
    )


# ---------------------------------------------------------------------------
# InMemoryTenantStore fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def store():
    """Empty InMemoryTenantStore, cleared after each test."""
    from fastapi_tenancy.storage.memory import InMemoryTenantStore

    s = InMemoryTenantStore()
    yield s
    s.clear()


@pytest.fixture
async def seeded_store(t_active, t_second):
    """Store pre-populated with two active tenants."""
    from fastapi_tenancy.storage.memory import InMemoryTenantStore

    s = InMemoryTenantStore()
    await s.create(t_active)
    await s.create(t_second)
    yield s
    s.clear()


# ---------------------------------------------------------------------------
# SQLite / SQLAlchemy store
# ---------------------------------------------------------------------------


@pytest.fixture
async def sqlite_store(tmp_path):
    """SQLAlchemyTenantStore backed by a temp SQLite file."""
    from fastapi_tenancy.storage.database import SQLAlchemyTenantStore

    db_file = tmp_path / "test.db"
    s = SQLAlchemyTenantStore(database_url=f"sqlite+aiosqlite:///{db_file}")
    await s.initialize()
    yield s
    await s.close()


# ---------------------------------------------------------------------------
# Mock request builder
# ---------------------------------------------------------------------------


def make_request(
    headers: dict[str, str] | None = None,
    hostname: str = "acme-corp.example.com",
    path: str = "/api/data",
    method: str = "GET",
    cookies: dict[str, str] | None = None,
) -> MagicMock:
    """Return a lightweight mock of a Starlette Request."""
    req = MagicMock()
    req.method = method
    req.headers = dict(headers or {})
    req.url.hostname = hostname
    req.url.path = path
    req.cookies = cookies or {}
    return req


# ---------------------------------------------------------------------------
# Mock TenantStore
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_store(t_active):
    """AsyncMock TenantStore that resolves to t_active."""
    s = AsyncMock()
    s.get_by_identifier.return_value = t_active
    s.get_by_id.return_value = t_active
    s.exists.return_value = True
    return s


# ---------------------------------------------------------------------------
# TenancyConfig fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg():
    """Minimal TenancyConfig using SQLite — no external services."""
    from fastapi_tenancy.core.config import TenancyConfig

    return TenancyConfig(
        database_url="sqlite+aiosqlite:///:memory:",
        resolution_strategy=ResolutionStrategy.HEADER,
        isolation_strategy=IsolationStrategy.SCHEMA,
    )
