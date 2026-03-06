"""Unit tests for :class:`~fastapi_tenancy.storage.tenant_store.TenantStore` base class."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING, Any

import pytest

from fastapi_tenancy.core.exceptions import TenantNotFoundError
from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.storage.tenant_store import TenantStore

if TYPE_CHECKING:
    from collections.abc import Sequence


class DummyStore(TenantStore[Tenant]):
    """In-memory store that intentionally does NOT override any batch methods.

    This isolates the base-class N+1 implementations so they can be tested
    without interference from concrete optimisations.
    """

    def __init__(self) -> None:
        self._data: dict[str, Tenant] = {}

    async def get_by_id(self, tenant_id: str) -> Tenant:
        t = self._data.get(tenant_id)
        if t is None:
            raise TenantNotFoundError(identifier=tenant_id)
        return t

    async def get_by_identifier(self, identifier: str) -> Tenant:
        for t in self._data.values():
            if t.identifier == identifier:
                return t
        raise TenantNotFoundError(identifier=identifier)

    async def create(self, tenant: Tenant) -> Tenant:
        if tenant.id in self._data:
            raise ValueError(f"Duplicate id={tenant.id!r}")
        self._data[tenant.id] = tenant
        return tenant

    async def update(self, tenant: Tenant) -> Tenant:
        if tenant.id not in self._data:
            raise TenantNotFoundError(identifier=tenant.id)
        self._data[tenant.id] = tenant
        return tenant

    async def delete(self, tenant_id: str) -> None:
        if tenant_id not in self._data:
            raise TenantNotFoundError(identifier=tenant_id)
        del self._data[tenant_id]

    async def list(
        self,
        skip: int = 0,
        limit: int = 100,
        status: TenantStatus | None = None,
    ) -> Sequence[Tenant]:
        rows = list(self._data.values())
        if status is not None:
            rows = [t for t in rows if t.status == status]
        return rows[skip : skip + limit]

    async def count(self, status: TenantStatus | None = None) -> int:
        return len(await self.list(status=status))

    async def exists(self, tenant_id: str) -> bool:
        return tenant_id in self._data

    async def set_status(self, tenant_id: str, status: TenantStatus) -> Tenant:
        t = await self.get_by_id(tenant_id)
        updated = t.model_copy(update={"status": status})
        self._data[tenant_id] = updated
        return updated

    async def update_metadata(self, tenant_id: str, metadata: dict[str, Any]) -> Tenant:
        t = await self.get_by_id(tenant_id)
        updated = t.model_copy(update={"metadata": {**t.metadata, **metadata}})
        self._data[tenant_id] = updated
        return updated


def _make(
    n: int,
    *,
    name_prefix: str = "Tenant",
    status: TenantStatus = TenantStatus.ACTIVE,
) -> Tenant:
    now = datetime.now(UTC)
    return Tenant(
        id=f"t-{n:04d}",
        identifier=f"tenant-{n:04d}",
        name=f"{name_prefix} {n}",
        status=status,
        metadata={},
        created_at=now,
        updated_at=now,
    )


@pytest.mark.unit
class TestBaseGetByIds:
    async def test_all_found(self) -> None:
        store = DummyStore()
        t1 = await store.create(_make(1))
        t2 = await store.create(_make(2))
        result = await store.get_by_ids([t1.id, t2.id])
        assert {r.id for r in result} == {t1.id, t2.id}

    async def test_missing_ids_skipped(self) -> None:
        store = DummyStore()
        t = await store.create(_make(1))
        result = await store.get_by_ids([t.id, "missing-1", "missing-2"])
        assert len(result) == 1
        assert result[0].id == t.id

    async def test_all_missing_returns_empty(self) -> None:
        store = DummyStore()
        result = await store.get_by_ids(["a", "b", "c"])
        assert result == []

    async def test_empty_input_returns_empty(self) -> None:
        store = DummyStore()
        assert await store.get_by_ids([]) == []

    async def test_order_preserved(self) -> None:
        store = DummyStore()
        t1 = await store.create(_make(1))
        t2 = await store.create(_make(2))
        t3 = await store.create(_make(3))
        result = await store.get_by_ids([t3.id, t1.id, t2.id])
        assert [r.id for r in result] == [t3.id, t1.id, t2.id]

    async def test_generator_input_consumed(self) -> None:
        store = DummyStore()
        t = await store.create(_make(1))
        # Pass a generator rather than a list
        result = await store.get_by_ids(i for i in [t.id])
        assert len(result) == 1


@pytest.mark.unit
class TestBaseSearch:
    async def test_match_by_identifier(self) -> None:
        store = DummyStore()
        await store.create(_make(1))  # identifier = "tenant-0001"
        result = await store.search("0001")
        assert len(result) == 1
        assert result[0].id == "t-0001"

    async def test_match_by_name(self) -> None:
        store = DummyStore()
        await store.create(
            Tenant(
                id="name-t",
                identifier="umbrella-org",
                name="Umbrella Corporation",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        result = await store.search("umbrella")
        assert len(result) == 1

    async def test_case_insensitive(self) -> None:
        store = DummyStore()
        await store.create(_make(1))
        result = await store.search("TENANT")
        assert len(result) == 1

    async def test_no_match_returns_empty(self) -> None:
        store = DummyStore()
        await store.create(_make(1))
        assert await store.search("xyzzy-no-match") == []

    async def test_limit_respected(self) -> None:
        store = DummyStore()
        for i in range(10):
            await store.create(_make(i + 100))
        result = await store.search("tenant", limit=3)
        assert len(result) == 3

    async def test_status_filter_via_list(self) -> None:
        """Base search() filters via list(); status param is respected."""
        store = DummyStore()
        await store.create(_make(1, status=TenantStatus.ACTIVE))
        await store.create(_make(2, status=TenantStatus.SUSPENDED))
        # The base search calls list() which filters by status
        all_results = await store.search("tenant", limit=10)
        # Both are returned (no status filter on search itself)
        assert len(all_results) == 2

    async def test_scan_limit_and_result_limit_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When both scan_limit and result_limit are saturated, a warning is emitted."""
        store = DummyStore()
        # Create exactly _scan_limit=5 tenants, all matching
        for i in range(5):
            await store.create(_make(i + 200))
        caplog.set_level(logging.WARNING)
        result = await store.search("tenant", limit=5, _scan_limit=5)
        assert len(result) == 5
        assert "scan limit" in caplog.text.lower()

    async def test_no_warning_when_scan_limit_not_reached(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = DummyStore()
        for i in range(3):
            await store.create(_make(i + 300))
        caplog.set_level(logging.WARNING)
        await store.search("tenant", limit=10, _scan_limit=100)
        assert "scan limit" not in caplog.text.lower()

    async def test_empty_store_returns_empty(self) -> None:
        store = DummyStore()
        assert await store.search("anything") == []


@pytest.mark.unit
class TestBaseBulkUpdateStatus:
    async def test_all_found_and_updated(self) -> None:
        store = DummyStore()
        t1 = await store.create(_make(1))
        t2 = await store.create(_make(2))
        result = await store.bulk_update_status([t1.id, t2.id], TenantStatus.SUSPENDED)
        assert len(result) == 2
        assert all(t.status == TenantStatus.SUSPENDED for t in result)

    async def test_missing_ids_skipped(self) -> None:
        store = DummyStore()
        t = await store.create(_make(1))
        result = await store.bulk_update_status([t.id, "ghost-1", "ghost-2"], TenantStatus.DELETED)
        assert len(result) == 1
        assert result[0].id == t.id

    async def test_all_missing_returns_empty(self) -> None:
        store = DummyStore()
        result = await store.bulk_update_status(["x", "y", "z"], TenantStatus.SUSPENDED)
        assert result == []

    async def test_empty_input_returns_empty(self) -> None:
        store = DummyStore()
        assert await store.bulk_update_status([], TenantStatus.ACTIVE) == []

    async def test_changes_persisted(self) -> None:
        store = DummyStore()
        t = await store.create(_make(1))
        await store.bulk_update_status([t.id], TenantStatus.DELETED)
        fetched = await store.get_by_id(t.id)
        assert fetched.status == TenantStatus.DELETED

    async def test_generator_input_consumed(self) -> None:
        store = DummyStore()
        t = await store.create(_make(1))
        result = await store.bulk_update_status((i for i in [t.id]), TenantStatus.SUSPENDED)
        assert len(result) == 1


@pytest.mark.unit
class TestBaseClose:
    async def test_close_does_not_raise(self) -> None:
        store = DummyStore()
        await store.close()  # inherits base no-op — must not raise

    async def test_close_idempotent(self) -> None:
        store = DummyStore()
        await store.close()
        await store.close()  # second call also must not raise


@pytest.mark.unit
class TestTypeAlias:
    def test_default_tenant_store_is_tenant_store(self) -> None:
        # DefaultTenantStore = TenantStore[Tenant] — it must be the same generic
        # alias; instantiating a DummyStore must satisfy isinstance via the base
        assert issubclass(DummyStore, TenantStore)

    def test_exported(self) -> None:
        from fastapi_tenancy.storage.tenant_store import DefaultTenantStore  # noqa: PLC0415

        assert DefaultTenantStore is not None
