"""Abstract contract tests for :class:`~fastapi_tenancy.storage.database.SQLAlchemyTenantStore`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import text

from fastapi_tenancy.core.exceptions import TenancyError, TenantNotFoundError
from fastapi_tenancy.core.types import IsolationStrategy, Tenant, TenantStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi_tenancy.storage.database import SQLAlchemyTenantStore


def _ts(offset: int = 0) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=offset)


@pytest.mark.integration
class TestSQLACreateRead:
    async def test_create_and_get_by_id(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant()
        created = await any_sqla_store.create(t)
        fetched = await any_sqla_store.get_by_id(created.id)
        assert fetched.id == created.id
        assert fetched.identifier == created.identifier
        assert fetched.name == created.name

    async def test_create_and_get_by_identifier(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant(identifier="sqla-slug")
        await any_sqla_store.create(t)
        fetched = await any_sqla_store.get_by_identifier("sqla-slug")
        assert fetched.id == t.id

    async def test_get_by_id_missing_raises_not_found(
        self, any_sqla_store: SQLAlchemyTenantStore
    ) -> None:
        with pytest.raises(TenantNotFoundError):
            await any_sqla_store.get_by_id("does-not-exist")

    async def test_get_by_identifier_missing_raises_not_found(
        self, any_sqla_store: SQLAlchemyTenantStore
    ) -> None:
        with pytest.raises(TenantNotFoundError):
            await any_sqla_store.get_by_identifier("ghost-slug")

    async def test_metadata_roundtrip(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        payload: dict[str, Any] = {
            "plan": "enterprise",
            "nested": {"a": [1, 2, 3], "b": True},
            "count": 42,
        }
        t = make_tenant(metadata=payload)
        await any_sqla_store.create(t)
        fetched = await any_sqla_store.get_by_identifier(t.identifier)
        assert fetched.metadata == payload

    async def test_optional_fields_stored_and_retrieved(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant(
            isolation_strategy=IsolationStrategy.SCHEMA,
            database_url="postgresql+asyncpg://u:p@h/db",
            schema_name="tenant_abc",
        )
        await any_sqla_store.create(t)
        fetched = await any_sqla_store.get_by_id(t.id)
        assert fetched.isolation_strategy == IsolationStrategy.SCHEMA
        assert fetched.database_url == "postgresql+asyncpg://u:p@h/db"
        assert fetched.schema_name == "tenant_abc"

    async def test_datetime_is_timezone_aware(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant()
        await any_sqla_store.create(t)
        fetched = await any_sqla_store.get_by_id(t.id)
        assert fetched.created_at.tzinfo is not None
        assert fetched.updated_at.tzinfo is not None


@pytest.mark.integration
class TestSQLAConstraints:
    async def test_duplicate_id_raises_value_error(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant()
        await any_sqla_store.create(t)
        with pytest.raises(ValueError, match="already exists"):
            await any_sqla_store.create(t)

    async def test_duplicate_identifier_raises_value_error(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t1 = make_tenant(identifier="unique-ident")
        t2 = make_tenant(identifier="unique-ident")
        await any_sqla_store.create(t1)
        with pytest.raises(ValueError, match="already exists"):
            await any_sqla_store.create(t2)

    async def test_generic_exception_wrapped_in_tenancy_error(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unexpected DB exceptions must be wrapped into TenancyError."""

        class _BrokenSession:
            async def __aenter__(self) -> _BrokenSession:
                raise RuntimeError("DB exploded")

            async def __aexit__(self, *_: object) -> bool:
                return False

        monkeypatch.setattr(any_sqla_store, "_session_factory", lambda *a, **kw: _BrokenSession())
        with pytest.raises(TenancyError):
            await any_sqla_store.create(make_tenant())


@pytest.mark.integration
class TestSQLAUpdate:
    async def test_update_name(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await any_sqla_store.create(make_tenant(name="Original"))
        updated = await any_sqla_store.update(t.model_copy(update={"name": "Renamed"}))
        assert updated.name == "Renamed"
        assert (await any_sqla_store.get_by_id(t.id)).name == "Renamed"

    async def test_update_status(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await any_sqla_store.create(make_tenant())
        result = await any_sqla_store.update(
            t.model_copy(update={"status": TenantStatus.SUSPENDED})
        )
        assert result.status == TenantStatus.SUSPENDED

    async def test_update_refreshes_updated_at(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await any_sqla_store.create(make_tenant())
        result = await any_sqla_store.update(t.model_copy(update={"name": "New"}))
        assert result.updated_at >= t.updated_at

    async def test_update_missing_raises_not_found(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        ghost = make_tenant()
        with pytest.raises(TenantNotFoundError):
            await any_sqla_store.update(ghost)

    async def test_update_identifier_conflict_raises_value_error(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        await any_sqla_store.create(make_tenant(identifier="slug-1"))
        t2 = await any_sqla_store.create(make_tenant(identifier="slug-2"))
        with pytest.raises(ValueError):
            # Try to rename t2 to t1's identifier
            await any_sqla_store.update(t2.model_copy(update={"identifier": "slug-1"}))

    async def test_update_clears_optional_fields(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await any_sqla_store.create(
            make_tenant(
                isolation_strategy=IsolationStrategy.RLS,
                database_url="postgres://u:p@h/db",
                schema_name="tenant_x",
            )
        )
        cleared = t.model_copy(
            update={"isolation_strategy": None, "database_url": None, "schema_name": None}
        )
        result = await any_sqla_store.update(cleared)
        assert result.isolation_strategy is None
        assert result.database_url is None
        assert result.schema_name is None


@pytest.mark.integration
class TestSQLADelete:
    async def test_delete_removes_row(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await any_sqla_store.create(make_tenant())
        await any_sqla_store.delete(t.id)
        with pytest.raises(TenantNotFoundError):
            await any_sqla_store.get_by_id(t.id)

    async def test_delete_missing_raises_not_found(
        self, any_sqla_store: SQLAlchemyTenantStore
    ) -> None:
        with pytest.raises(TenantNotFoundError):
            await any_sqla_store.delete("ghost-id")

    async def test_delete_twice_raises(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await any_sqla_store.create(make_tenant())
        await any_sqla_store.delete(t.id)
        with pytest.raises(TenantNotFoundError):
            await any_sqla_store.delete(t.id)

    async def test_delete_exception_wrapped(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        t = await any_sqla_store.create(make_tenant())

        class _BrokenSession:
            async def __aenter__(self) -> _BrokenSession:
                raise RuntimeError("boom")

            async def __aexit__(self, *_: object) -> bool:
                return False

        monkeypatch.setattr(any_sqla_store, "_session_factory", lambda *a, **kw: _BrokenSession())
        with pytest.raises(TenancyError):
            await any_sqla_store.delete(t.id)


@pytest.mark.integration
class TestSQLAListCountExists:
    async def test_list_empty(self, any_sqla_store: SQLAlchemyTenantStore) -> None:
        assert await any_sqla_store.list() == []

    async def test_list_ordered_newest_first(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t1 = await any_sqla_store.create(make_tenant(created_at=_ts(-3)))
        t2 = await any_sqla_store.create(make_tenant(created_at=_ts(-2)))
        t3 = await any_sqla_store.create(make_tenant(created_at=_ts(-1)))
        results = await any_sqla_store.list()
        ids = [r.id for r in results]
        assert ids.index(t3.id) < ids.index(t2.id) < ids.index(t1.id)

    async def test_list_filter_by_status(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        await any_sqla_store.create(make_tenant(status=TenantStatus.ACTIVE))
        await any_sqla_store.create(make_tenant(status=TenantStatus.SUSPENDED))
        active = await any_sqla_store.list(status=TenantStatus.ACTIVE)
        assert all(t.status == TenantStatus.ACTIVE for t in active)

    async def test_list_pagination(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        for _ in range(5):
            await any_sqla_store.create(make_tenant())
        page = await any_sqla_store.list(skip=2, limit=2)
        assert len(page) == 2

    async def test_count_all(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        await any_sqla_store.create(make_tenant())
        await any_sqla_store.create(make_tenant())
        assert await any_sqla_store.count() == 2

    async def test_count_by_status(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        await any_sqla_store.create(make_tenant(status=TenantStatus.ACTIVE))
        await any_sqla_store.create(make_tenant(status=TenantStatus.SUSPENDED))
        assert await any_sqla_store.count(status=TenantStatus.ACTIVE) == 1
        assert await any_sqla_store.count(status=TenantStatus.SUSPENDED) == 1
        assert await any_sqla_store.count(status=TenantStatus.DELETED) == 0

    async def test_exists_true_false(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await any_sqla_store.create(make_tenant())
        assert await any_sqla_store.exists(t.id) is True
        assert await any_sqla_store.exists("ghost") is False

    async def test_count_empty_returns_zero(self, any_sqla_store: SQLAlchemyTenantStore) -> None:
        assert await any_sqla_store.count() == 0


@pytest.mark.integration
class TestSQLASetStatus:
    async def test_set_status_updates_row(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await any_sqla_store.create(make_tenant())
        result = await any_sqla_store.set_status(t.id, TenantStatus.SUSPENDED)
        assert result.status == TenantStatus.SUSPENDED
        assert (await any_sqla_store.get_by_id(t.id)).status == TenantStatus.SUSPENDED

    async def test_set_status_missing_raises_not_found(
        self, any_sqla_store: SQLAlchemyTenantStore
    ) -> None:
        with pytest.raises(TenantNotFoundError):
            await any_sqla_store.set_status("ghost", TenantStatus.ACTIVE)

    async def test_set_status_all_values(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await any_sqla_store.create(make_tenant())
        for status in TenantStatus:
            result = await any_sqla_store.set_status(t.id, status)
            assert result.status == status


@pytest.mark.integration
class TestSQLAUpdateMetadata:
    async def test_merge_adds_keys(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await any_sqla_store.create(make_tenant(metadata={"a": 1}))
        result = await any_sqla_store.update_metadata(t.id, {"b": 2})
        assert result.metadata == {"a": 1, "b": 2}

    async def test_merge_overwrites_existing_key(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await any_sqla_store.create(make_tenant(metadata={"key": "old"}))
        result = await any_sqla_store.update_metadata(t.id, {"key": "new"})
        assert result.metadata["key"] == "new"

    async def test_successive_merges_accumulate(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await any_sqla_store.create(make_tenant())
        await any_sqla_store.update_metadata(t.id, {"a": 1})
        result = await any_sqla_store.update_metadata(t.id, {"b": 2})
        assert result.metadata == {"a": 1, "b": 2}

    async def test_missing_raises_not_found(self, any_sqla_store: SQLAlchemyTenantStore) -> None:
        with pytest.raises(TenantNotFoundError):
            await any_sqla_store.update_metadata("ghost", {"k": "v"})

    async def test_corrupted_metadata_gracefully_replaced(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        """Corrupted JSON in the DB must be recovered as {} before merge."""
        t = await any_sqla_store.create(make_tenant())
        async with any_sqla_store._engine.begin() as conn:
            await conn.execute(
                text("UPDATE tenants SET tenant_metadata = 'INVALID_JSON' WHERE id = :id"),
                {"id": t.id},
            )
        result = await any_sqla_store.update_metadata(t.id, {"safe": "data"})
        assert result.metadata == {"safe": "data"}


@pytest.mark.integration
class TestSQLAGetByIds:
    async def test_fetch_multiple(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t1 = await any_sqla_store.create(make_tenant())
        t2 = await any_sqla_store.create(make_tenant())
        results = await any_sqla_store.get_by_ids([t1.id, t2.id])
        assert {r.id for r in results} == {t1.id, t2.id}

    async def test_skips_missing_ids(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await any_sqla_store.create(make_tenant())
        results = await any_sqla_store.get_by_ids([t.id, "missing"])
        assert len(results) == 1

    async def test_empty_input_returns_empty(self, any_sqla_store: SQLAlchemyTenantStore) -> None:
        assert await any_sqla_store.get_by_ids([]) == []

    async def test_all_missing_returns_empty(self, any_sqla_store: SQLAlchemyTenantStore) -> None:
        assert await any_sqla_store.get_by_ids(["a", "b"]) == []


@pytest.mark.integration
class TestSQLASearch:
    async def test_match_by_identifier(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        await any_sqla_store.create(make_tenant(identifier="search-acme", name="Acme Corp"))
        await any_sqla_store.create(make_tenant(identifier="search-globex", name="Globex"))
        results = await any_sqla_store.search("acme")
        assert len(results) == 1
        assert results[0].identifier == "search-acme"

    async def test_match_by_name(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        await any_sqla_store.create(make_tenant(identifier="corp-z", name="Umbrella Corp"))
        results = await any_sqla_store.search("umbrella")
        assert len(results) == 1

    async def test_limit_honoured(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        for i in range(10):
            await any_sqla_store.create(make_tenant(identifier=f"match-{i:02d}", name=f"Match {i}"))
        results = await any_sqla_store.search("match", limit=3)
        assert len(results) == 3

    async def test_no_match_returns_empty(self, any_sqla_store: SQLAlchemyTenantStore) -> None:
        assert await any_sqla_store.search("xyzzy") == []

    async def test_case_insensitive(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        await any_sqla_store.create(make_tenant(identifier="case-test", name="MyCompany"))
        results = await any_sqla_store.search("MYCOMPANY")
        assert len(results) == 1


@pytest.mark.integration
class TestSQLABulkUpdateStatus:
    async def test_updates_all_matched(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t1 = await any_sqla_store.create(make_tenant())
        t2 = await any_sqla_store.create(make_tenant())
        result = await any_sqla_store.bulk_update_status([t1.id, t2.id], TenantStatus.SUSPENDED)
        assert len(result) == 2
        assert all(t.status == TenantStatus.SUSPENDED for t in result)

    async def test_empty_input_returns_empty(self, any_sqla_store: SQLAlchemyTenantStore) -> None:
        assert await any_sqla_store.bulk_update_status([], TenantStatus.ACTIVE) == []

    async def test_skips_missing_ids(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await any_sqla_store.create(make_tenant())
        result = await any_sqla_store.bulk_update_status(
            [t.id, "ghost-1", "ghost-2"], TenantStatus.DELETED
        )
        assert len(result) == 1

    async def test_persisted_in_db(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await any_sqla_store.create(make_tenant())
        await any_sqla_store.bulk_update_status([t.id], TenantStatus.SUSPENDED)
        fetched = await any_sqla_store.get_by_id(t.id)
        assert fetched.status == TenantStatus.SUSPENDED


@pytest.mark.integration
class TestSQLAEdgeCases:
    async def test_corrupted_metadata_returns_empty_dict(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await any_sqla_store.create(make_tenant())
        async with any_sqla_store._engine.begin() as conn:
            await conn.execute(
                text("UPDATE tenants SET tenant_metadata = 'BAD_JSON' WHERE id = :id"),
                {"id": t.id},
            )
        fetched = await any_sqla_store.get_by_id(t.id)
        assert fetched.metadata == {}

    async def test_naive_datetime_coerced_to_utc(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = await any_sqla_store.create(make_tenant())
        async with any_sqla_store._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE tenants "
                    "SET created_at = '2024-01-01 00:00:00', "
                    "    updated_at = '2024-01-01 00:00:00' "
                    "WHERE id = :id"
                ),
                {"id": t.id},
            )
        fetched = await any_sqla_store.get_by_id(t.id)
        assert fetched.created_at.tzinfo is not None
        assert fetched.updated_at.tzinfo is not None

    async def test_initialize_is_idempotent(self, any_sqla_store: SQLAlchemyTenantStore) -> None:
        # Second call must not raise (CREATE TABLE IF NOT EXISTS)
        await any_sqla_store.initialize()

    async def test_close_disposes_engine(
        self,
        any_sqla_store: SQLAlchemyTenantStore,
    ) -> None:
        # close() must complete without exception; subsequent calls may fail but
        # must not raise an unexpected error type
        await any_sqla_store.close()
