"""Tests for fastapi_tenancy.core.context — ContextVar, TenantContext, tenant_scope."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from fastapi_tenancy.core.context import (
    TenantContext,
    get_current_tenant,
    get_current_tenant_optional,
    tenant_scope,
)
from fastapi_tenancy.core.exceptions import TenantNotFoundError
from fastapi_tenancy.core.types import Tenant, TenantStatus

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(id="ctx-t1", identifier="ctx-acme", name="Ctx Acme")


@pytest.fixture
def other_tenant() -> Tenant:
    return Tenant(id="ctx-t2", identifier="ctx-globex", name="Ctx Globex")


def _make_tenant(
    *,
    tenant_id: str = "t-fix-001",
    identifier: str = "fix-tenant",
    status: TenantStatus = TenantStatus.ACTIVE,
) -> Tenant:
    now = datetime.now(UTC)
    return Tenant(
        id=tenant_id,
        identifier=identifier,
        name=identifier.title(),
        status=status,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture(autouse=True)
def clean_context() -> Generator[Any, Any, Any]:
    """Always clear context before and after each test."""
    TenantContext.clear()
    yield
    TenantContext.clear()


class TestTenantContextSet:
    def test_set_and_get(self, tenant: Tenant) -> None:
        TenantContext.set(tenant)
        assert TenantContext.get() == tenant

    def test_set_returns_token(self, tenant: Tenant) -> None:
        token = TenantContext.set(tenant)
        assert token is not None

    def test_reset_restores_previous(self, tenant: Tenant, other_tenant: Tenant) -> None:
        token1 = TenantContext.set(tenant)
        token2 = TenantContext.set(other_tenant)
        assert TenantContext.get() == other_tenant
        TenantContext.reset(token2)
        assert TenantContext.get() == tenant
        TenantContext.reset(token1)
        assert TenantContext.get_optional() is None

    def test_get_raises_when_unset(self) -> None:
        with pytest.raises(TenantNotFoundError) as exc_info:
            TenantContext.get()
        assert exc_info.value.details.get("hint")

    def test_get_optional_returns_none_when_unset(self) -> None:
        assert TenantContext.get_optional() is None

    def test_get_optional_returns_tenant_when_set(self, tenant: Tenant) -> None:
        TenantContext.set(tenant)
        assert TenantContext.get_optional() == tenant

    def test_clear_removes_tenant(self, tenant: Tenant) -> None:
        TenantContext.set(tenant)
        TenantContext.clear()
        assert TenantContext.get_optional() is None


class TestTenantContextMetadata:
    def test_set_and_get_metadata(self, tenant: Tenant) -> None:
        TenantContext.set(tenant)
        TenantContext.set_metadata("request_id", "req-123")
        assert TenantContext.get_metadata("request_id") == "req-123"

    def test_get_missing_key_returns_default(self, tenant: Tenant) -> None:
        TenantContext.set(tenant)
        assert TenantContext.get_metadata("missing") is None
        assert TenantContext.get_metadata("missing", default="fallback") == "fallback"

    def test_get_metadata_when_no_metadata_set(self) -> None:
        assert TenantContext.get_metadata("key") is None

    def test_get_all_metadata(self, tenant: Tenant) -> None:
        TenantContext.set(tenant)
        TenantContext.set_metadata("a", 1)
        TenantContext.set_metadata("b", 2)
        meta = TenantContext.get_all_metadata()
        assert meta == {"a": 1, "b": 2}

    def test_get_all_metadata_empty(self) -> None:
        assert TenantContext.get_all_metadata() == {}

    def test_mutating_returned_metadata_does_not_affect_context(self, tenant: Tenant) -> None:
        TenantContext.set(tenant)
        TenantContext.set_metadata("x", 10)
        meta = TenantContext.get_all_metadata()
        meta["y"] = 99  # mutate the copy
        assert TenantContext.get_metadata("y") is None

    def test_clear_metadata(self, tenant: Tenant) -> None:
        TenantContext.set(tenant)
        TenantContext.set_metadata("key", "val")
        TenantContext.clear_metadata()
        assert TenantContext.get_metadata("key") is None
        # Tenant should still be set after clearing only metadata
        assert TenantContext.get_optional() == tenant

    def test_clear_clears_both_tenant_and_metadata(self, tenant: Tenant) -> None:
        TenantContext.set(tenant)
        TenantContext.set_metadata("k", "v")
        TenantContext.clear()
        assert TenantContext.get_optional() is None
        assert TenantContext.get_all_metadata() == {}


class TestTenantScope:
    async def test_sets_tenant_during_scope(self, tenant: Tenant) -> None:
        async with tenant_scope(tenant) as t:
            assert TenantContext.get() == tenant
            assert t == tenant

    async def test_restores_none_after_scope(self, tenant: Tenant) -> None:
        async with tenant_scope(tenant):
            pass
        assert TenantContext.get_optional() is None

    async def test_restores_previous_tenant_after_scope(
        self, tenant: Tenant, other_tenant: Tenant
    ) -> None:
        TenantContext.set(tenant)
        async with tenant_scope(other_tenant):
            assert TenantContext.get() == other_tenant
        assert TenantContext.get() == tenant

    async def test_clears_metadata_on_scope_entry(self, tenant: Tenant) -> None:
        TenantContext.set_metadata("outer_key", "outer_val")
        async with tenant_scope(tenant):
            # Metadata should be fresh inside the scope
            assert TenantContext.get_metadata("outer_key") is None
            TenantContext.set_metadata("inner_key", "inner_val")
        # After scope, outer metadata should not be "outer_val" either
        # (the meta_token reset clears inner metadata)
        assert TenantContext.get_metadata("inner_key") is None

    async def test_restores_after_exception(self, tenant: Tenant) -> None:
        with pytest.raises(ValueError):
            async with tenant_scope(tenant):
                raise ValueError("boom")
        assert TenantContext.get_optional() is None

    async def test_nested_scopes(self, tenant: Tenant, other_tenant: Tenant) -> None:
        async with tenant_scope(tenant):
            assert TenantContext.get() == tenant
            async with tenant_scope(other_tenant):
                assert TenantContext.get() == other_tenant
            assert TenantContext.get() == tenant
        assert TenantContext.get_optional() is None

    async def test_concurrent_tasks_isolated(self, tenant: Tenant, other_tenant: Tenant) -> None:
        """Two concurrent tasks must not share each other's tenant context."""
        results = {}

        async def task_a() -> None:
            async with tenant_scope(tenant):
                await asyncio.sleep(0)  # yield to other task
                results["a"] = TenantContext.get().id

        async def task_b() -> None:
            async with tenant_scope(other_tenant):
                await asyncio.sleep(0)
                results["b"] = TenantContext.get().id

        await asyncio.gather(task_a(), task_b())
        assert results["a"] == tenant.id
        assert results["b"] == other_tenant.id


class TestFastAPIDependencies:
    async def test_get_current_tenant_returns_tenant(self, tenant: Tenant) -> None:
        TenantContext.set(tenant)
        result = get_current_tenant()
        assert result == tenant

    async def test_get_current_tenant_raises_when_unset(self) -> None:
        with pytest.raises(TenantNotFoundError):
            get_current_tenant()

    async def test_get_current_tenant_optional_returns_none(self) -> None:
        assert get_current_tenant_optional() is None

    async def test_get_current_tenant_optional_returns_tenant(self, tenant: Tenant) -> None:
        TenantContext.set(tenant)
        assert get_current_tenant_optional() == tenant


class TestContextClearReturnsTokens:
    """FIX: clear() returns (tenant_token, metadata_token); reset_all() restores state.

    Before the fix:
        ``clear()`` called ``_tenant_ctx.set(None)`` and ``_metadata_ctx.set(None)``
        and returned ``None``.  The tokens produced by those set() calls were
        discarded immediately, making it *impossible* to restore the previous state.
        In nested scopes (e.g. a test fixture clearing context inside a
        ``tenant_scope`` block), the outer tenant would be permanently erased
        from the perspective of any code that later called ``reset(token)``.

        ``clear_metadata()`` had the same problem for the metadata variable.

    After the fix:
        Both methods return their tokens.  ``reset_all(tenant_token, meta_token)``
        restores both variables to exactly the state that existed before the clear.
        Existing callers that ignore the return value are unaffected.
    """

    def setup_method(self) -> None:
        """Start each test with a clean context."""
        TenantContext.clear()

    def teardown_method(self) -> None:
        """Leave a clean context after each test."""
        TenantContext.clear()

    def test_clear_returns_two_tokens(self) -> None:
        """clear() must return a 2-tuple of Token objects."""
        from contextvars import Token  # noqa: PLC0415

        result = TenantContext.clear()
        assert isinstance(result, tuple)
        assert len(result) == 2
        tenant_token, meta_token = result
        assert isinstance(tenant_token, Token)
        assert isinstance(meta_token, Token)

    def test_clear_still_clears_both(self) -> None:
        """clear() must still clear both variables (backward compatibility)."""
        tenant = _make_tenant()
        TenantContext.set(tenant)
        TenantContext.set_metadata("key", "value")

        TenantContext.clear()

        assert TenantContext.get_optional() is None
        assert TenantContext.get_all_metadata() == {}

    def test_reset_all_restores_tenant(self) -> None:
        """reset_all() must restore the tenant that was active before clear()."""
        tenant = _make_tenant()
        TenantContext.set(tenant)

        tokens = TenantContext.clear()
        assert TenantContext.get_optional() is None  # cleared

        TenantContext.reset_all(*tokens)
        assert TenantContext.get_optional() == tenant  # restored

    def test_reset_all_restores_metadata(self) -> None:
        """reset_all() must restore metadata that was active before clear()."""
        tenant = _make_tenant()
        TenantContext.set(tenant)
        TenantContext.set_metadata("plan", "enterprise")
        TenantContext.set_metadata("seats", 100)

        tokens = TenantContext.clear()
        assert TenantContext.get_all_metadata() == {}  # cleared

        TenantContext.reset_all(*tokens)
        assert TenantContext.get_metadata("plan") == "enterprise"
        assert TenantContext.get_metadata("seats") == 100

    def test_reset_all_is_safe_when_nothing_was_set(self) -> None:
        """reset_all() from a blank context must not raise."""
        tokens = TenantContext.clear()  # already blank
        TenantContext.reset_all(*tokens)  # must not raise
        assert TenantContext.get_optional() is None

    def test_nested_clear_and_reset_all(self) -> None:
        """Nested clear/reset_all must correctly restore the outer tenant."""
        outer = _make_tenant(tenant_id="t-outer", identifier="outer-tenant")
        inner = _make_tenant(tenant_id="t-inner", identifier="inner-tenant")

        # Set outer tenant.
        outer_token = TenantContext.set(outer)
        assert TenantContext.get_optional() == outer

        # Clear inside a nested block, set inner tenant, then restore.
        saved_tokens = TenantContext.clear()
        assert TenantContext.get_optional() is None

        TenantContext.set(inner)
        assert TenantContext.get_optional() == inner

        TenantContext.reset_all(*saved_tokens)
        assert TenantContext.get_optional() == outer  # outer is back

        # Now restore the original pre-outer state.
        TenantContext.reset(outer_token)
        assert TenantContext.get_optional() is None

    def test_clear_metadata_returns_token(self) -> None:
        """clear_metadata() must return a Token for the metadata variable."""
        from contextvars import Token  # noqa: PLC0415

        tenant = _make_tenant()
        TenantContext.set(tenant)
        TenantContext.set_metadata("x", 1)

        token = TenantContext.clear_metadata()

        assert isinstance(token, Token)
        # Metadata is cleared.
        assert TenantContext.get_metadata("x") is None
        # Tenant is untouched.
        assert TenantContext.get_optional() == tenant

    def test_clear_metadata_token_restores_metadata(self) -> None:
        """The token from clear_metadata() must allow restoring previous metadata."""
        from fastapi_tenancy.core.context import _metadata_ctx  # noqa: PLC0415

        tenant = _make_tenant()
        TenantContext.set(tenant)
        TenantContext.set_metadata("key", "original")

        token = TenantContext.clear_metadata()
        assert TenantContext.get_metadata("key") is None

        _metadata_ctx.reset(token)
        assert TenantContext.get_metadata("key") == "original"

    def test_existing_callers_ignoring_return_value_still_work(self) -> None:
        """Code that ignores clear()'s return value must continue to work."""
        tenant = _make_tenant()
        TenantContext.set(tenant)

        TenantContext.clear()  # return value intentionally ignored

        assert TenantContext.get_optional() is None  # still cleared
