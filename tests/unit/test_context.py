"""Unit tests — fastapi_tenancy.core.context

Coverage target: 100 %

Verified:
* TenantContext.set() / get() / get_optional() / reset() / clear()
* get() raises TenantNotFoundError when no tenant set
* Token-based reset restores previous state correctly
* Metadata: set_metadata, get_metadata, get_all_metadata, clear_metadata
* TenantContext.scope (sync __enter__ / __exit__)
* TenantContext.scope (async __aenter__ / __aexit__)
* Scope restores previous context on exit even on exception
* FastAPI dependency helpers: get_current_tenant, get_current_tenant_optional
* Concurrent isolation (contextvars copy per task)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from fastapi_tenancy.core.context import (
    TenantContext,
    get_current_tenant,
    get_current_tenant_optional,
)
from fastapi_tenancy.core.exceptions import TenantNotFoundError
from fastapi_tenancy.core.types import Tenant, TenantStatus

pytestmark = pytest.mark.unit

_NOW = datetime(2024, 1, 1, tzinfo=UTC)


def _t(identifier: str = "acme-corp", id: str = "t-001") -> Tenant:
    return Tenant(
        id=id,
        identifier=identifier,
        name="Test Corp",
        status=TenantStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )


# ─── Helpers ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_context():
    """Always clear the context before and after each test."""
    TenantContext.clear()
    yield
    TenantContext.clear()


# ──────────────────────────── set / get ──────────────────────────────────────


class TestSetGet:
    def test_get_raises_when_no_tenant(self):
        with pytest.raises(TenantNotFoundError):
            TenantContext.get()

    def test_set_then_get_returns_tenant(self):
        t = _t()
        TenantContext.set(t)
        assert TenantContext.get() is t

    def test_get_optional_returns_none_when_no_tenant(self):
        assert TenantContext.get_optional() is None

    def test_get_optional_returns_tenant_when_set(self):
        t = _t()
        TenantContext.set(t)
        assert TenantContext.get_optional() is t

    def test_set_returns_token(self):
        t = _t()
        token = TenantContext.set(t)
        assert token is not None

    def test_clear_removes_tenant(self):
        TenantContext.set(_t())
        TenantContext.clear()
        assert TenantContext.get_optional() is None

    def test_reset_restores_none(self):
        t = _t()
        token = TenantContext.set(t)
        TenantContext.reset(token)
        assert TenantContext.get_optional() is None

    def test_reset_restores_previous_tenant(self):
        t1 = _t(identifier="first", id="t-001")
        t2 = _t(identifier="second", id="t-002")
        TenantContext.set(t1)
        token = TenantContext.set(t2)
        assert TenantContext.get().identifier == "second"
        TenantContext.reset(token)
        assert TenantContext.get().identifier == "first"


# ───────────────────────────── Metadata ──────────────────────────────────────


class TestMetadata:
    def test_get_metadata_missing_key_returns_default(self):
        assert TenantContext.get_metadata("missing") is None

    def test_get_metadata_custom_default(self):
        assert TenantContext.get_metadata("missing", default="fallback") == "fallback"

    def test_set_and_get_metadata(self):
        TenantContext.set_metadata("request_id", "req-abc")
        assert TenantContext.get_metadata("request_id") == "req-abc"

    def test_set_multiple_metadata_keys(self):
        TenantContext.set_metadata("a", 1)
        TenantContext.set_metadata("b", 2)
        assert TenantContext.get_metadata("a") == 1
        assert TenantContext.get_metadata("b") == 2

    def test_overwrite_metadata_key(self):
        TenantContext.set_metadata("key", "v1")
        TenantContext.set_metadata("key", "v2")
        assert TenantContext.get_metadata("key") == "v2"

    def test_get_all_metadata_empty(self):
        assert TenantContext.get_all_metadata() == {}

    def test_get_all_metadata_populated(self):
        TenantContext.set_metadata("x", 10)
        TenantContext.set_metadata("y", 20)
        all_meta = TenantContext.get_all_metadata()
        assert all_meta == {"x": 10, "y": 20}

    def test_get_all_metadata_returns_copy(self):
        TenantContext.set_metadata("x", 1)
        all_meta = TenantContext.get_all_metadata()
        all_meta["injected"] = 99
        assert TenantContext.get_metadata("injected") is None

    def test_clear_metadata_removes_all(self):
        TenantContext.set_metadata("a", 1)
        TenantContext.clear_metadata()
        assert TenantContext.get_all_metadata() == {}

    def test_clear_metadata_keeps_tenant(self):
        t = _t()
        TenantContext.set(t)
        TenantContext.set_metadata("key", "val")
        TenantContext.clear_metadata()
        assert TenantContext.get() is t  # tenant still set

    def test_clear_removes_metadata_too(self):
        TenantContext.set_metadata("x", 1)
        TenantContext.clear()
        assert TenantContext.get_all_metadata() == {}


# ─────────────────────────── scope sync ──────────────────────────────────────


class TestScopeSync:
    def test_sets_tenant_inside_scope(self):
        t = _t()
        with TenantContext.scope(t) as active:
            assert active is t
            assert TenantContext.get() is t

    def test_restores_none_after_scope(self):
        t = _t()
        with TenantContext.scope(t):
            pass
        assert TenantContext.get_optional() is None

    def test_restores_previous_tenant_after_scope(self):
        t1 = _t(identifier="outer", id="t-001")
        t2 = _t(identifier="inner", id="t-002")
        TenantContext.set(t1)
        with TenantContext.scope(t2):
            assert TenantContext.get().identifier == "inner"
        assert TenantContext.get().identifier == "outer"

    def test_cleanup_on_exception(self):
        t = _t()
        try:
            with TenantContext.scope(t):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert TenantContext.get_optional() is None

    def test_metadata_cleared_on_scope_entry(self):
        TenantContext.set_metadata("pre_scope", "value")
        t = _t()
        with TenantContext.scope(t):
            # scope sets metadata context to None on entry
            assert TenantContext.get_metadata("pre_scope") is None

    def test_metadata_restored_after_scope(self):
        TenantContext.set_metadata("outer_key", "outer_val")
        with TenantContext.scope(_t()):
            TenantContext.set_metadata("inner_key", "inner_val")
        # outer metadata restored
        assert TenantContext.get_metadata("outer_key") == "outer_val"


# ─────────────────────────── scope async ─────────────────────────────────────


class TestScopeAsync:
    async def test_sets_tenant_inside_async_scope(self):
        t = _t()
        async with TenantContext.scope(t) as active:
            assert active is t
            assert TenantContext.get() is t

    async def test_restores_none_after_async_scope(self):
        async with TenantContext.scope(_t()):
            pass
        assert TenantContext.get_optional() is None

    async def test_cleanup_on_async_exception(self):
        try:
            async with TenantContext.scope(_t()):
                raise ValueError("async boom")
        except ValueError:
            pass
        assert TenantContext.get_optional() is None

    async def test_nested_async_scopes(self):
        t1 = _t(identifier="outer", id="t-001")
        t2 = _t(identifier="inner", id="t-002")
        async with TenantContext.scope(t1):
            assert TenantContext.get().identifier == "outer"
            async with TenantContext.scope(t2):
                assert TenantContext.get().identifier == "inner"
            assert TenantContext.get().identifier == "outer"


# ─────────────────────── Task-level isolation ─────────────────────────────────


class TestConcurrentIsolation:
    async def test_different_tasks_see_different_tenants(self):
        t1 = _t(identifier="tenant-one", id="t-001")
        t2 = _t(identifier="tenant-two", id="t-002")
        results: dict[str, str] = {}

        async def task_one():
            async with TenantContext.scope(t1):
                await asyncio.sleep(0)
                results["task1"] = TenantContext.get().identifier

        async def task_two():
            async with TenantContext.scope(t2):
                await asyncio.sleep(0)
                results["task2"] = TenantContext.get().identifier

        await asyncio.gather(task_one(), task_two())
        assert results["task1"] == "tenant-one"
        assert results["task2"] == "tenant-two"


# ─────────────────── FastAPI dependency functions ─────────────────────────────


class TestDependencyFunctions:
    def test_get_current_tenant_raises_when_empty(self):
        with pytest.raises(TenantNotFoundError):
            get_current_tenant()

    def test_get_current_tenant_returns_when_set(self):
        t = _t()
        TenantContext.set(t)
        assert get_current_tenant() is t

    def test_get_current_tenant_optional_none(self):
        assert get_current_tenant_optional() is None

    def test_get_current_tenant_optional_returns_set(self):
        t = _t()
        TenantContext.set(t)
        assert get_current_tenant_optional() is t
