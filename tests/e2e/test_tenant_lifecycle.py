"""End-to-end tests — complete tenant lifecycle

Tests the full lifecycle of a tenant:
  create → activate → use → suspend → reactivate → soft-delete

Uses InMemoryTenantStore (no external services).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.storage.memory import InMemoryTenantStore

pytestmark = pytest.mark.e2e

_NOW = datetime(2024, 1, 1, tzinfo=UTC)


def _tenant(
    id: str = "t-001",
    identifier: str = "new-startup",
    status: TenantStatus = TenantStatus.PROVISIONING,
) -> Tenant:
    return Tenant(
        id=id,
        identifier=identifier,
        name="New Startup",
        status=status,
        created_at=_NOW,
        updated_at=_NOW,
    )


@pytest.fixture
async def store():
    s = InMemoryTenantStore()
    yield s
    s.clear()


# ─────────────────────────── Full lifecycle ───────────────────────────────────


class TestFullLifecycle:
    async def test_create_in_provisioning_state(self, store):
        t = _tenant(status=TenantStatus.PROVISIONING)
        created = await store.create(t)
        assert created.status == TenantStatus.PROVISIONING
        assert not created.is_active()

    async def test_activate_tenant(self, store):
        t = _tenant(status=TenantStatus.PROVISIONING)
        await store.create(t)
        activated = await store.set_status(t.id, TenantStatus.ACTIVE)
        assert activated.status == TenantStatus.ACTIVE
        assert activated.is_active()

    async def test_active_tenant_is_retrievable(self, store):
        t = _tenant(status=TenantStatus.ACTIVE)
        await store.create(t)
        found = await store.get_by_identifier(t.identifier)
        assert found.is_active()

    async def test_suspend_active_tenant(self, store):
        t = _tenant(status=TenantStatus.ACTIVE)
        await store.create(t)
        suspended = await store.set_status(t.id, TenantStatus.SUSPENDED)
        assert suspended.status == TenantStatus.SUSPENDED
        assert not suspended.is_active()

    async def test_reactivate_suspended_tenant(self, store):
        t = _tenant(status=TenantStatus.SUSPENDED)
        await store.create(t)
        reactivated = await store.set_status(t.id, TenantStatus.ACTIVE)
        assert reactivated.is_active()

    async def test_soft_delete(self, store):
        t = _tenant(status=TenantStatus.ACTIVE)
        await store.create(t)
        deleted = await store.set_status(t.id, TenantStatus.DELETED)
        assert deleted.status == TenantStatus.DELETED
        assert not deleted.is_active()

    async def test_deleted_tenant_still_retrievable(self, store):
        """Soft delete keeps the record in the store."""
        t = _tenant(status=TenantStatus.ACTIVE)
        await store.create(t)
        await store.set_status(t.id, TenantStatus.DELETED)
        # Still findable by ID (soft delete — record not removed)
        found = await store.get_by_id(t.id)
        assert found.status == TenantStatus.DELETED

    async def test_hard_delete_removes_record(self, store):
        from fastapi_tenancy.core.exceptions import TenantNotFoundError

        t = _tenant()
        await store.create(t)
        await store.delete(t.id)
        with pytest.raises(TenantNotFoundError):
            await store.get_by_id(t.id)


# ──────────────────────── Metadata lifecycle ─────────────────────────────────


class TestMetadataLifecycle:
    async def test_add_plan_metadata(self, store):
        t = _tenant(status=TenantStatus.ACTIVE)
        await store.create(t)
        updated = await store.update_metadata(t.id, {"plan": "basic"})
        assert updated.metadata["plan"] == "basic"

    async def test_upgrade_plan(self, store):
        t = _tenant(status=TenantStatus.ACTIVE)
        await store.create(t)
        await store.update_metadata(t.id, {"plan": "basic"})
        upgraded = await store.update_metadata(t.id, {"plan": "pro", "max_users": 500})
        assert upgraded.metadata["plan"] == "pro"
        assert upgraded.metadata["max_users"] == 500

    async def test_add_feature_flags(self, store):
        t = _tenant(status=TenantStatus.ACTIVE)
        await store.create(t)
        updated = await store.update_metadata(t.id, {"features": ["sso", "audit", "custom_domain"]})
        assert "sso" in updated.metadata["features"]


# ────────────────────────── Multi-tenant ─────────────────────────────────────


class TestMultiTenantCoexistence:
    async def test_multiple_tenants_independent(self, store):
        t1 = _tenant(id="t-001", identifier="corp-one", status=TenantStatus.ACTIVE)
        t2 = _tenant(id="t-002", identifier="corp-two", status=TenantStatus.ACTIVE)
        t3 = _tenant(id="t-003", identifier="corp-three", status=TenantStatus.SUSPENDED)
        await store.create(t1)
        await store.create(t2)
        await store.create(t3)

        assert await store.count() == 3
        assert await store.count(status=TenantStatus.ACTIVE) == 2
        assert await store.count(status=TenantStatus.SUSPENDED) == 1

    async def test_suspend_one_does_not_affect_others(self, store):
        t1 = _tenant(id="t-001", identifier="corp-one", status=TenantStatus.ACTIVE)
        t2 = _tenant(id="t-002", identifier="corp-two", status=TenantStatus.ACTIVE)
        await store.create(t1)
        await store.create(t2)

        await store.set_status(t1.id, TenantStatus.SUSPENDED)

        t1_found = await store.get_by_id(t1.id)
        t2_found = await store.get_by_id(t2.id)
        assert t1_found.status == TenantStatus.SUSPENDED
        assert t2_found.status == TenantStatus.ACTIVE

    async def test_bulk_suspend_all(self, store):
        ids = []
        for i in range(5):
            t = _tenant(id=f"t-{i:03d}", identifier=f"corp-{i:02d}", status=TenantStatus.ACTIVE)
            await store.create(t)
            ids.append(t.id)

        results = await store.bulk_update_status(ids, TenantStatus.SUSPENDED)
        assert len(results) == 5
        assert all(r.status == TenantStatus.SUSPENDED for r in results)


# ──────────────────── Resolver → store lifecycle ─────────────────────────────


class TestResolverStoreIntegration:
    async def test_resolver_finds_active_tenant(self, store):
        from fastapi_tenancy.resolution.header import HeaderTenantResolver

        t = _tenant(identifier="resolver-test", status=TenantStatus.ACTIVE)
        await store.create(t)

        from unittest.mock import MagicMock

        resolver = HeaderTenantResolver(tenant_store=store)
        request = MagicMock()
        request.headers = {"X-Tenant-ID": "resolver-test"}
        request.url.hostname = "localhost"
        request.url.path = "/data"

        found = await resolver.resolve(request)
        assert found.identifier == "resolver-test"
        assert found.is_active()

    async def test_resolver_fails_for_deleted_tenant(self, store):
        """Resolver finds the tenant but middleware blocks deleted status."""
        from fastapi_tenancy.core.exceptions import TenantNotFoundError
        from fastapi_tenancy.resolution.header import HeaderTenantResolver

        t = _tenant(identifier="deleted-test", status=TenantStatus.DELETED)
        await store.create(t)

        from unittest.mock import MagicMock

        resolver = HeaderTenantResolver(tenant_store=store)
        request = MagicMock()
        request.headers = {"X-Tenant-ID": "deleted-test"}

        # Resolver itself finds it (filtering is middleware's job)
        found = await resolver.resolve(request)
        assert not found.is_active()

    async def test_resolver_fails_for_nonexistent(self, store):
        from fastapi_tenancy.core.exceptions import TenantNotFoundError
        from fastapi_tenancy.resolution.header import HeaderTenantResolver
        from unittest.mock import MagicMock

        resolver = HeaderTenantResolver(tenant_store=store)
        request = MagicMock()
        request.headers = {"X-Tenant-ID": "does-not-exist"}

        with pytest.raises(TenantNotFoundError):
            await resolver.resolve(request)
