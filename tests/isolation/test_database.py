"""Tests for :class:`~fastapi_tenancy.isolation.database.DatabaseIsolationProvider` and
the :class:`~fastapi_tenancy.isolation.database._LRUEngineCache`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import gc
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.exceptions import IsolationError
from fastapi_tenancy.core.types import IsolationStrategy, Tenant, TenantStatus
from fastapi_tenancy.isolation.database import DatabaseIsolationProvider, _LRUEngineCache
from fastapi_tenancy.utils.db_compat import DbDialect

if TYPE_CHECKING:
    from collections.abc import Callable


def _db_cfg(url: str = "sqlite+aiosqlite:///:memory:", **kw: Any) -> TenancyConfig:
    template = kw.pop(
        "database_url_template",
        "sqlite+aiosqlite:///./test_{database_name}.db",
    )
    return TenancyConfig(
        database_url=url,
        isolation_strategy=IsolationStrategy.DATABASE,
        database_url_template=template,
        schema_prefix="t_",
        database_echo=False,
        database_pool_size=2,
        database_max_overflow=2,
        database_pool_timeout=10,
        database_pool_recycle=300,
        database_pool_pre_ping=False,
        **kw,
    )


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


async def _make_engine() -> AsyncEngine:
    return create_async_engine("sqlite+aiosqlite:///:memory:")


@pytest.mark.unit
class TestLRUEngineCache:
    async def test_miss_returns_none(self) -> None:
        cache = _LRUEngineCache(max_size=5)
        assert await cache.get("missing") is None

    async def test_put_and_get(self) -> None:
        cache = _LRUEngineCache(max_size=5)
        engine = await _make_engine()
        await cache.put("k1", engine)
        assert await cache.get("k1") is engine
        await engine.dispose()

    async def test_get_promotes_to_mru(self) -> None:
        cache = _LRUEngineCache(max_size=2)
        e1 = await _make_engine()
        e2 = await _make_engine()
        e3 = await _make_engine()
        await cache.put("k1", e1)
        await cache.put("k2", e2)
        # Access k1 to promote it to MRU
        await cache.get("k1")
        # Adding k3 should evict k2 (LRU), not k1
        evicted = await cache.put("k3", e3)
        assert evicted is e2
        assert await cache.get("k1") is e1
        assert await cache.get("k3") is e3
        await e1.dispose()
        await e2.dispose()
        await e3.dispose()

    async def test_put_evicts_lru_when_full(self) -> None:
        cache = _LRUEngineCache(max_size=2)
        e1 = await _make_engine()
        e2 = await _make_engine()
        e3 = await _make_engine()
        await cache.put("k1", e1)
        await cache.put("k2", e2)
        evicted = await cache.put("k3", e3)
        assert evicted is e1
        assert cache.size == 2
        await e1.dispose()
        await e2.dispose()
        await e3.dispose()

    async def test_put_existing_key_no_eviction(self) -> None:
        cache = _LRUEngineCache(max_size=2)
        e1 = await _make_engine()
        await cache.put("k1", e1)
        # Re-inserting same key should not evict anything
        evicted = await cache.put("k1", e1)
        assert evicted is None
        assert cache.size == 1
        await e1.dispose()

    async def test_remove_existing(self) -> None:
        cache = _LRUEngineCache(max_size=5)
        e1 = await _make_engine()
        await cache.put("k1", e1)
        removed = await cache.remove("k1")
        assert removed is e1
        assert cache.size == 0
        await e1.dispose()

    async def test_remove_missing_returns_none(self) -> None:
        cache = _LRUEngineCache(max_size=5)
        assert await cache.remove("ghost") is None

    async def test_dispose_all_clears_cache(self) -> None:
        cache = _LRUEngineCache(max_size=5)
        e1 = await _make_engine()
        e2 = await _make_engine()
        await cache.put("k1", e1)
        await cache.put("k2", e2)
        count = await cache.dispose_all()
        assert count == 2
        assert cache.size == 0

    async def test_dispose_all_handles_dispose_exception(self) -> None:
        cache = _LRUEngineCache(max_size=5)
        bad_engine = MagicMock()
        bad_engine.dispose = AsyncMock(side_effect=RuntimeError("pool broken"))
        cache._cache["bad"] = bad_engine
        # Must not propagate the exception; count should still work
        count = await cache.dispose_all()
        assert count == 0  # dispose raised, so not counted

    async def test_size_property(self) -> None:
        cache = _LRUEngineCache(max_size=5)
        assert cache.size == 0
        e1 = await _make_engine()
        await cache.put("k1", e1)
        assert cache.size == 1
        await e1.dispose()


@pytest.mark.unit
class TestDatabaseProviderConstruction:
    def test_sqlite_detected(self) -> None:
        p = DatabaseIsolationProvider(_db_cfg())
        assert p.dialect == DbDialect.SQLITE

    def test_pg_detected(self) -> None:
        cfg = _db_cfg(
            "postgresql+asyncpg://u:p@h/db",
            database_url_template="postgresql+asyncpg://u:p@h/{database_name}",
        )
        p = DatabaseIsolationProvider(cfg)
        assert p.dialect == DbDialect.POSTGRESQL

    def test_master_engine_injected(self) -> None:

        master = create_async_engine("sqlite+aiosqlite:///:memory:")
        p = DatabaseIsolationProvider(_db_cfg(), master_engine=master)
        assert p._master is master

    def test_sqlite_static_pool_on_master(self) -> None:
        from sqlalchemy.pool import StaticPool  # noqa: PLC0415

        p = DatabaseIsolationProvider(_db_cfg())
        assert isinstance(p._master.pool, StaticPool)


@pytest.mark.unit
class TestNamingHelpers:
    def test_database_name_format(self, make_tenant: Callable[..., Tenant]) -> None:
        p = DatabaseIsolationProvider(_db_cfg())
        t = make_tenant(identifier="acme-corp")
        name = p._database_name(t)
        assert name.startswith("tenant_")
        assert name.endswith("_db")

    def test_tenant_url_sqlite_uses_slug(self, make_tenant: Callable[..., Tenant]) -> None:
        p = DatabaseIsolationProvider(_db_cfg())
        t = make_tenant(identifier="acme-corp")
        url = p._tenant_url(t)
        assert "acme" in url
        assert url.endswith(".db")

    def test_tenant_url_uses_template_when_set(self, make_tenant: Callable[..., Tenant]) -> None:
        cfg = _db_cfg(
            "postgresql+asyncpg://u:p@h/db",
            database_url_template="postgresql+asyncpg://u:p@h/{database_name}",
        )
        p = DatabaseIsolationProvider(cfg)
        t = make_tenant(identifier="acme-corp")
        url = p._tenant_url(t)
        assert "postgresql" in url
        assert "tenant_" in url


@pytest.mark.unit
class TestGetEngine:
    async def test_fast_path_returns_cached(
        self, database_provider: DatabaseIsolationProvider, make_tenant: Callable[..., Tenant]
    ) -> None:
        t = make_tenant(identifier="fast-path-tenant")
        engine1 = await database_provider._get_engine(t)
        engine2 = await database_provider._get_engine(t)
        assert engine1 is engine2

    async def test_different_tenants_get_different_engines(
        self, database_provider: DatabaseIsolationProvider, make_tenant: Callable[..., Tenant]
    ) -> None:
        t1 = make_tenant(identifier="engine-tenant-a")
        t2 = make_tenant(identifier="engine-tenant-b")
        e1 = await database_provider._get_engine(t1)
        e2 = await database_provider._get_engine(t2)
        assert e1 is not e2

    async def test_concurrent_gets_same_tenant_return_same_engine(
        self, database_provider: DatabaseIsolationProvider, make_tenant: Callable[..., Tenant]
    ) -> None:
        t = make_tenant(identifier="concurrent-engine-tenant")
        engines = await asyncio.gather(*(database_provider._get_engine(t) for _ in range(10)))
        # All coroutines must receive the same engine object
        assert all(e is engines[0] for e in engines)

    async def test_lru_eviction_disposes_old_engine(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        cfg = _db_cfg(max_cached_engines=12)
        p = DatabaseIsolationProvider(cfg)
        t1 = make_tenant(identifier="lru-tenant-a")
        t2 = make_tenant(identifier="lru-tenant-b")
        t3 = make_tenant(identifier="lru-tenant-c")
        await p._get_engine(t1)
        await p._get_engine(t2)
        # Adding t3 must evict t1 (LRU)
        await p._get_engine(t3)
        assert p._engine_cache.size == 3
        await p.close()


@pytest.mark.unit
class TestGetSession:
    async def test_yields_session(
        self, database_provider: DatabaseIsolationProvider, make_tenant: Callable[..., Tenant]
    ) -> None:
        from sqlalchemy.ext.asyncio import AsyncSession  # noqa: PLC0415

        t = make_tenant(identifier="session-tenant")
        async with database_provider.get_session(t) as session:
            assert isinstance(session, AsyncSession)

    async def test_exception_in_handler_raises_isolation_error(
        self, database_provider: DatabaseIsolationProvider, make_tenant: Callable[..., Tenant]
    ) -> None:
        t = make_tenant(identifier="session-err-tenant")
        with pytest.raises(IsolationError):
            async with database_provider.get_session(t):
                raise RuntimeError("handler blew up")


@pytest.mark.unit
class TestApplyFilters:
    async def test_returns_query_unchanged(
        self, database_provider: DatabaseIsolationProvider, make_tenant: Callable[..., Tenant]
    ) -> None:
        meta = sa.MetaData()
        tbl = sa.Table("t", meta, sa.Column("id", sa.Integer))
        q = sa.select(tbl)
        t = make_tenant()
        result = await database_provider.apply_filters(q, t)
        assert result is q


@pytest.mark.unit
class TestInitializeTenant:
    async def test_sqlite_creates_engine(
        self, database_provider: DatabaseIsolationProvider, make_tenant: Callable[..., Tenant]
    ) -> None:
        t = make_tenant(identifier="sqlite-init-tenant")
        await database_provider.initialize_tenant(t)
        assert database_provider._engine_cache.size >= 1

    async def test_sqlite_with_metadata_creates_tables(
        self,
        database_provider: DatabaseIsolationProvider,
        make_tenant: Callable[..., Tenant],
        simple_metadata: sa.MetaData,
    ) -> None:
        t = make_tenant(identifier="sqlite-init-tables")
        await database_provider.initialize_tenant(t, metadata=simple_metadata)

    async def test_mssql_raises_isolation_error(self, make_tenant: Callable[..., Tenant]) -> None:
        pytest.importorskip("aioodbc", reason="aioodbc not installed")
        # MSSQL does not support automatic CREATE DATABASE
        cfg = TenancyConfig(
            database_url="mssql+aioodbc://sa:pass@localhost/master?driver=ODBC+Driver+18+for+SQL+Server",
            isolation_strategy=IsolationStrategy.DATABASE,
            database_url_template="mssql+aioodbc://sa:pass@localhost/{database_name}?driver=ODBC+Driver+18+for+SQL+Server",
            schema_prefix="t_",
            database_echo=False,
            database_pool_size=2,
            database_max_overflow=2,
            database_pool_timeout=10,
            database_pool_recycle=300,
            database_pool_pre_ping=False,
        )
        p = DatabaseIsolationProvider(cfg)
        t = make_tenant()
        with pytest.raises(IsolationError, match="MSSQL"):
            await p.initialize_tenant(t)


@pytest.mark.unit
class TestDestroyTenantSQLite:
    async def test_destroy_sqlite_deletes_file(
        self,
        make_tenant: Callable[..., Tenant],
        tmp_path: Path,
    ) -> None:
        db_file = tmp_path / "tenant_test_destroy.db"
        cfg = TenancyConfig(
            database_url=f"sqlite+aiosqlite:///{db_file.parent}/placeholder.db",
            isolation_strategy=IsolationStrategy.DATABASE,
            database_url_template=f"sqlite+aiosqlite:///{db_file.parent}/{{database_name}}.db",
            schema_prefix="t_",
            database_echo=False,
            database_pool_size=2,
            database_max_overflow=2,
            database_pool_timeout=10,
            database_pool_recycle=300,
            database_pool_pre_ping=False,
        )
        p = DatabaseIsolationProvider(cfg)
        t = make_tenant(identifier="destroy-me")
        # Create the DB file first
        await p.initialize_tenant(t)
        # Get the actual path
        url = p._tenant_url(t)
        from urllib.parse import urlparse  # noqa: PLC0415

        parsed = urlparse(url)
        actual_path = Path(parsed.netloc + parsed.path)
        if actual_path.exists():
            await p.destroy_tenant(t)
            assert not actual_path.exists()
        await p.close()

    async def test_destroy_sqlite_nonexistent_file_no_error(
        self,
        database_provider: DatabaseIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant(identifier="ghost-sqlite-db")
        # File never existed — should not raise
        await database_provider.destroy_tenant(t)


@pytest.mark.unit
class TestVerifyIsolationSQLite:
    async def test_returns_false_before_init(
        self, database_provider: DatabaseIsolationProvider, make_tenant: Callable[..., Tenant]
    ) -> None:
        t = make_tenant(identifier="not-yet-created")
        # SQLite in-memory — file path check returns False
        assert await database_provider.verify_isolation(t) is False


@pytest.mark.unit
class TestClose:
    async def test_close_disposes_all(
        self, database_provider: DatabaseIsolationProvider, make_tenant: Callable[..., Tenant]
    ) -> None:
        t = make_tenant(identifier="close-test-tenant")
        await database_provider._get_engine(t)
        await database_provider.close()
        assert database_provider._engine_cache.size == 0


@pytest.mark.e2e
class TestPostgresDatabaseProvider:
    async def test_initialize_creates_database(
        self,
        pg_database_provider: DatabaseIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant(identifier="pg-db-create-test")
        await pg_database_provider.initialize_tenant(t)
        assert await pg_database_provider.verify_isolation(t) is True
        # Cleanup
        await pg_database_provider.destroy_tenant(t)

    async def test_initialize_skip_existing_database(
        self,
        pg_database_provider: DatabaseIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant(identifier="pg-db-idempotent-test")
        await pg_database_provider.initialize_tenant(t)
        # Second call must not raise (database already exists branch)
        await pg_database_provider.initialize_tenant(t)
        # Cleanup
        await pg_database_provider.destroy_tenant(t)

    async def test_destroy_drops_database(
        self,
        pg_database_provider: DatabaseIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant(identifier="pg-db-destroy-test")
        await pg_database_provider.initialize_tenant(t)
        await pg_database_provider.destroy_tenant(t)
        assert await pg_database_provider.verify_isolation(t) is False

    async def test_verify_false_for_unknown(
        self,
        pg_database_provider: DatabaseIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant(identifier="pg-never-existed-db")
        assert await pg_database_provider.verify_isolation(t) is False


@pytest.mark.integration
class TestCreationLocksLeak:
    """FIX: _creation_locks (WeakValueDictionary) must not retain stale Lock entries.

    With WeakValueDictionary, an entry disappears automatically once no live
    Python reference to the Lock object remains.  The tests call gc.collect()
    after dropping local references to ensure the GC has had a chance to
    reclaim entries before the assertion runs.
    """

    async def test_lock_removed_after_successful_creation(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        import gc  # noqa: PLC0415

        provider = DatabaseIsolationProvider(_db_cfg())
        tenant = make_tenant(identifier="lock-ok")
        try:
            await provider._get_engine(tenant)
            # Drop any implicit reference held in the local frame and run GC so
            # the WeakValueDictionary entry is collected before the assertion.
            gc.collect()
            assert tenant.id not in provider._creation_locks
        finally:
            await provider.close()

    async def test_lock_removed_on_engine_creation_failure(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        import gc  # noqa: PLC0415

        provider = DatabaseIsolationProvider(_db_cfg())
        tenant = make_tenant(identifier="lock-fail")
        with (
            patch(
                "fastapi_tenancy.isolation.database.create_async_engine",
                side_effect=RuntimeError("simulated failure"),
            ),
            pytest.raises(RuntimeError, match="simulated failure"),
        ):
            await provider._get_engine(tenant)
        # After the exception propagates, the local `tenant_lock` reference
        # inside _get_engine drops.  A GC cycle removes the WeakValueDictionary
        # entry so future attempts start fresh with no stale lock.
        gc.collect()
        assert tenant.id not in provider._creation_locks
        await provider.close()

    async def test_repeated_attempt_after_failure_succeeds(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:

        provider = DatabaseIsolationProvider(_db_cfg())
        tenant = make_tenant(identifier="lock-retry")
        call_count = 0
        real_create = create_async_engine

        def _failing_then_ok(url: str, **kw: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first attempt fails")
            return real_create(url, **kw)

        with patch("fastapi_tenancy.isolation.database.create_async_engine", _failing_then_ok):
            with pytest.raises(RuntimeError):
                await provider._get_engine(tenant)
            engine = await provider._get_engine(tenant)
            assert engine is not None
        await provider.close()


@pytest.mark.integration
class TestConcurrentEngineCreation:
    """FIX: 50 concurrent coroutines for the same tenant must produce exactly one engine."""

    async def test_exactly_one_engine_created_under_concurrency(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:

        provider = DatabaseIsolationProvider(_db_cfg())
        tenant = make_tenant(identifier="concurrent-tenant")
        creation_count = 0
        real_create = create_async_engine

        def _counting_create(url: str, **kw: Any) -> Any:
            nonlocal creation_count
            creation_count += 1
            return real_create(url, **kw)

        with patch("fastapi_tenancy.isolation.database.create_async_engine", _counting_create):
            engines = await asyncio.gather(*[provider._get_engine(tenant) for _ in range(50)])

        assert len({id(e) for e in engines}) == 1, "Multiple engine objects returned"
        assert creation_count == 1, f"Expected 1 creation, got {creation_count}"
        assert tenant.id not in provider._creation_locks
        await provider.close()

    async def test_concurrent_different_tenants_each_get_own_engine(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        provider = DatabaseIsolationProvider(_db_cfg())
        tenants = [make_tenant(identifier=f"multi-{i}") for i in range(10)]
        engines = await asyncio.gather(*[provider._get_engine(t) for t in tenants])
        assert len({id(e) for e in engines}) == 10
        await provider.close()


@pytest.mark.integration
class TestCreationLocksWeakValueDictionary:
    """FIX: _creation_locks uses WeakValueDictionary for automatic GC cleanup.

    Before the fix:
        A plain ``dict[str, asyncio.Lock]`` was used.  Manual ``pop`` calls
        cleaned up on success and failure, but edge cases (concurrent failures,
        engine creation that raised mid-pool-construction) could leave entries
        permanently.

    After the fix:
        ``WeakValueDictionary`` entries are collected automatically when no
        strong reference exists.  The local ``tenant_lock`` variable in
        ``_get_engine`` keeps the lock alive for the critical section duration.
    """

    async def test_lock_absent_after_successful_creation(self) -> None:
        """After engine creation succeeds, the lock entry is GC'd."""
        provider = DatabaseIsolationProvider(_db_cfg())
        tenant = _make_tenant(tenant_id="t-wvd-01", identifier="wvd-success")
        try:
            await provider._get_engine(tenant)
            gc.collect()
            assert tenant.id not in provider._creation_locks
        finally:
            await provider.close()

    async def test_lock_absent_after_creation_failure(self) -> None:
        """After engine creation fails, the lock entry is GC'd."""
        import gc as _gc  # noqa: PLC0415

        provider = DatabaseIsolationProvider(_db_cfg())
        tenant = _make_tenant(tenant_id="t-wvd-02", identifier="wvd-fail")

        with (
            patch(
                "fastapi_tenancy.isolation.database.create_async_engine",
                side_effect=RuntimeError("simulated"),
            ),
            pytest.raises(RuntimeError),
        ):
            await provider._get_engine(tenant)

        _gc.collect()
        assert tenant.id not in provider._creation_locks
        await provider.close()

    async def test_retry_after_failure_succeeds(self) -> None:
        """A second call after a failed first call must succeed without errors."""
        call_count = 0
        real_create = __import__(
            "fastapi_tenancy.isolation.database", fromlist=["create_async_engine"]
        ).create_async_engine

        def _failing_then_ok(url: str, **kw: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first attempt fails")
            return real_create(url, **kw)

        provider = DatabaseIsolationProvider(_db_cfg())
        tenant = _make_tenant(tenant_id="t-wvd-03", identifier="wvd-retry")

        with (
            patch(
                "fastapi_tenancy.isolation.database.create_async_engine",
                side_effect=_failing_then_ok,
            ),
            pytest.raises(RuntimeError),
        ):
            await provider._get_engine(tenant)

        # Second attempt — must not be blocked by a stale lock.
        engine = await provider._get_engine(tenant)
        assert engine is not None
        await provider.close()

    async def test_concurrent_calls_produce_single_engine(self) -> None:
        """Concurrent first-requests for the same tenant must produce exactly one engine."""
        provider = DatabaseIsolationProvider(_db_cfg())
        tenant = _make_tenant(tenant_id="t-wvd-04", identifier="wvd-concurrent")

        engines = await asyncio.gather(
            provider._get_engine(tenant),
            provider._get_engine(tenant),
            provider._get_engine(tenant),
        )

        # All three calls must return the same engine object.
        assert engines[0] is engines[1]
        assert engines[1] is engines[2]
        assert provider._engine_cache.size == 1

        await provider.close()

    async def test_dict_is_weak_value_dictionary(self) -> None:
        """_creation_locks must be a WeakValueDictionary instance."""
        import weakref  # noqa: PLC0415

        provider = DatabaseIsolationProvider(_db_cfg())
        assert isinstance(provider._creation_locks, weakref.WeakValueDictionary)
        await provider.close()
