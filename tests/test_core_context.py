"""Tests for fastapi_tenancy.core.context — ContextVar, TenantContext, tenant_scope."""

from __future__ import annotations

import asyncio
import pytest

from fastapi_tenancy.core.context import (
    TenantContext,
    get_current_tenant,
    get_current_tenant_optional,
    tenant_scope,
)
from fastapi_tenancy.core.exceptions import TenantNotFoundError
from fastapi_tenancy.core.types import Tenant, TenantStatus


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(id="ctx-t1", identifier="ctx-acme", name="Ctx Acme")


@pytest.fixture
def other_tenant() -> Tenant:
    return Tenant(id="ctx-t2", identifier="ctx-globex", name="Ctx Globex")


@pytest.fixture(autouse=True)
def clean_context():
    """Always clear context before and after each test."""
    TenantContext.clear()
    yield
    TenantContext.clear()


class TestTenantContextSet:
    def test_set_and_get(self, tenant):
        TenantContext.set(tenant)
        assert TenantContext.get() == tenant

    def test_set_returns_token(self, tenant):
        token = TenantContext.set(tenant)
        assert token is not None

    def test_reset_restores_previous(self, tenant, other_tenant):
        token1 = TenantContext.set(tenant)
        token2 = TenantContext.set(other_tenant)
        assert TenantContext.get() == other_tenant
        TenantContext.reset(token2)
        assert TenantContext.get() == tenant
        TenantContext.reset(token1)
        assert TenantContext.get_optional() is None

    def test_get_raises_when_unset(self):
        with pytest.raises(TenantNotFoundError) as exc_info:
            TenantContext.get()
        assert exc_info.value.details.get("hint")

    def test_get_optional_returns_none_when_unset(self):
        assert TenantContext.get_optional() is None

    def test_get_optional_returns_tenant_when_set(self, tenant):
        TenantContext.set(tenant)
        assert TenantContext.get_optional() == tenant

    def test_clear_removes_tenant(self, tenant):
        TenantContext.set(tenant)
        TenantContext.clear()
        assert TenantContext.get_optional() is None


class TestTenantContextMetadata:
    def test_set_and_get_metadata(self, tenant):
        TenantContext.set(tenant)
        TenantContext.set_metadata("request_id", "req-123")
        assert TenantContext.get_metadata("request_id") == "req-123"

    def test_get_missing_key_returns_default(self, tenant):
        TenantContext.set(tenant)
        assert TenantContext.get_metadata("missing") is None
        assert TenantContext.get_metadata("missing", default="fallback") == "fallback"

    def test_get_metadata_when_no_metadata_set(self):
        assert TenantContext.get_metadata("key") is None

    def test_get_all_metadata(self, tenant):
        TenantContext.set(tenant)
        TenantContext.set_metadata("a", 1)
        TenantContext.set_metadata("b", 2)
        meta = TenantContext.get_all_metadata()
        assert meta == {"a": 1, "b": 2}

    def test_get_all_metadata_empty(self):
        assert TenantContext.get_all_metadata() == {}

    def test_mutating_returned_metadata_does_not_affect_context(self, tenant):
        TenantContext.set(tenant)
        TenantContext.set_metadata("x", 10)
        meta = TenantContext.get_all_metadata()
        meta["y"] = 99  # mutate the copy
        assert TenantContext.get_metadata("y") is None

    def test_clear_metadata(self, tenant):
        TenantContext.set(tenant)
        TenantContext.set_metadata("key", "val")
        TenantContext.clear_metadata()
        assert TenantContext.get_metadata("key") is None
        # Tenant should still be set after clearing only metadata
        assert TenantContext.get_optional() == tenant

    def test_clear_clears_both_tenant_and_metadata(self, tenant):
        TenantContext.set(tenant)
        TenantContext.set_metadata("k", "v")
        TenantContext.clear()
        assert TenantContext.get_optional() is None
        assert TenantContext.get_all_metadata() == {}


class TestTenantScope:
    async def test_sets_tenant_during_scope(self, tenant):
        async with tenant_scope(tenant) as t:
            assert TenantContext.get() == tenant
            assert t == tenant

    async def test_restores_none_after_scope(self, tenant):
        async with tenant_scope(tenant):
            pass
        assert TenantContext.get_optional() is None

    async def test_restores_previous_tenant_after_scope(self, tenant, other_tenant):
        TenantContext.set(tenant)
        async with tenant_scope(other_tenant):
            assert TenantContext.get() == other_tenant
        assert TenantContext.get() == tenant

    async def test_clears_metadata_on_scope_entry(self, tenant):
        TenantContext.set_metadata("outer_key", "outer_val")
        async with tenant_scope(tenant):
            # Metadata should be fresh inside the scope
            assert TenantContext.get_metadata("outer_key") is None
            TenantContext.set_metadata("inner_key", "inner_val")
        # After scope, outer metadata should not be "outer_val" either
        # (the meta_token reset clears inner metadata)
        assert TenantContext.get_metadata("inner_key") is None

    async def test_restores_after_exception(self, tenant):
        with pytest.raises(ValueError):
            async with tenant_scope(tenant):
                raise ValueError("boom")
        assert TenantContext.get_optional() is None

    async def test_nested_scopes(self, tenant, other_tenant):
        async with tenant_scope(tenant):
            assert TenantContext.get() == tenant
            async with tenant_scope(other_tenant):
                assert TenantContext.get() == other_tenant
            assert TenantContext.get() == tenant
        assert TenantContext.get_optional() is None

    async def test_concurrent_tasks_isolated(self, tenant, other_tenant):
        """Two concurrent tasks must not share each other's tenant context."""
        results = {}

        async def task_a():
            async with tenant_scope(tenant):
                await asyncio.sleep(0)  # yield to other task
                results["a"] = TenantContext.get().id

        async def task_b():
            async with tenant_scope(other_tenant):
                await asyncio.sleep(0)
                results["b"] = TenantContext.get().id

        await asyncio.gather(task_a(), task_b())
        assert results["a"] == tenant.id
        assert results["b"] == other_tenant.id


class TestFastAPIDependencies:
    async def test_get_current_tenant_returns_tenant(self, tenant):
        TenantContext.set(tenant)
        result = get_current_tenant()
        assert result == tenant

    async def test_get_current_tenant_raises_when_unset(self):
        with pytest.raises(TenantNotFoundError):
            get_current_tenant()

    async def test_get_current_tenant_optional_returns_none(self):
        assert get_current_tenant_optional() is None

    async def test_get_current_tenant_optional_returns_tenant(self, tenant):
        TenantContext.set(tenant)
        assert get_current_tenant_optional() == tenant
