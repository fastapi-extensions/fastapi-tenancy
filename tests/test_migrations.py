"""Comprehensive tests for TenantMigrationManager."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fastapi_tenancy.core.exceptions import MigrationError
from fastapi_tenancy.core.types import IsolationStrategy, Tenant, TenantStatus
from fastapi_tenancy.migrations.manager import TenantMigrationManager
from fastapi_tenancy.storage.memory import InMemoryTenantStore


def _make_tenant(
    id: str = "t-001",
    identifier: str = "acme-corp",
    status: TenantStatus = TenantStatus.ACTIVE,
    isolation_strategy: IsolationStrategy | None = None,
    database_url: str | None = None,
    schema_name: str | None = None,
) -> Tenant:
    now = datetime.now(UTC)
    return Tenant(
        id=id,
        identifier=identifier,
        name=identifier.title(),
        status=status,
        isolation_strategy=isolation_strategy,
        database_url=database_url,
        schema_name=schema_name,
        created_at=now,
        updated_at=now,
    )


def _make_config(
    strategy: IsolationStrategy = IsolationStrategy.SCHEMA,
    db_url: str = "postgresql+asyncpg://user:pass@localhost/main",
    database_url_template: str | None = None,
    premium_tenants: list[str] | None = None,
    premium_isolation_strategy: IsolationStrategy = IsolationStrategy.SCHEMA,
    standard_isolation_strategy: IsolationStrategy = IsolationStrategy.RLS,
) -> MagicMock:
    """Build a lightweight mock TenancyConfig."""
    cfg = MagicMock()
    cfg.database_url = db_url
    cfg.database_url_template = database_url_template
    cfg.isolation_strategy = strategy
    cfg.premium_isolation_strategy = premium_isolation_strategy
    cfg.standard_isolation_strategy = standard_isolation_strategy
    cfg.premium_tenants = premium_tenants or []

    def _get_strategy(tenant_id: str) -> IsolationStrategy:
        if strategy == IsolationStrategy.HYBRID:
            if tenant_id in (premium_tenants or []):
                return premium_isolation_strategy
            return standard_isolation_strategy
        return strategy

    cfg.get_isolation_strategy_for_tenant.side_effect = _get_strategy
    cfg.get_schema_name.side_effect = lambda identifier: f"tenant_{identifier.replace('-', '_')}"
    cfg.get_database_url_for_tenant.side_effect = lambda tenant_id: (
        f"postgresql+asyncpg://user:pass@localhost/tenant_{tenant_id}"
    )
    return cfg


def _make_manager(
    cfg: Any | None = None,
    store: Any | None = None,
    alembic_cfg_path: str = "alembic.ini",
) -> TenantMigrationManager:
    if cfg is None:
        cfg = _make_config()
    if store is None:
        store = MagicMock()
    with patch.object(Path, "exists", return_value=True):
        return TenantMigrationManager(cfg, store, alembic_cfg_path=alembic_cfg_path)


class TestConstruction:
    def test_manager_stores_config_and_store(self) -> None:
        cfg = _make_config()
        store = MagicMock()
        mgr = _make_manager(cfg=cfg, store=store)
        assert mgr._config is cfg
        assert mgr._store is store

    def test_missing_alembic_ini_logs_warning(self, caplog: Any) -> None:

        cfg = _make_config()
        store = MagicMock()
        with patch.object(Path, "exists", return_value=False):  # noqa: SIM117
            with caplog.at_level(logging.WARNING, logger="fastapi_tenancy.migrations.manager"):
                TenantMigrationManager(cfg, store, alembic_cfg_path="/no/such/file.ini")
        assert "alembic.ini not found" in caplog.text

    def test_custom_executor_stored(self) -> None:
        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

        executor = ThreadPoolExecutor(max_workers=4)
        mgr = _make_manager()
        mgr._executor = executor
        assert mgr._executor is executor
        executor.shutdown(wait=False)


class TestBuildAlembicArgs:
    def test_schema_strategy_passes_url_and_schema(self) -> None:
        tenant = _make_tenant()
        cfg = _make_config(strategy=IsolationStrategy.SCHEMA)
        mgr = _make_manager(cfg=cfg)
        args = mgr._build_alembic_args(tenant)
        assert args["url"] == str(cfg.database_url)
        assert "schema" in args
        assert "acme_corp" in args["schema"]

    def test_schema_strategy_uses_tenant_schema_name_override(self) -> None:
        tenant = _make_tenant(schema_name="custom_schema")
        cfg = _make_config(strategy=IsolationStrategy.SCHEMA)
        mgr = _make_manager(cfg=cfg)
        args = mgr._build_alembic_args(tenant)
        assert args["schema"] == "custom_schema"

    def test_database_strategy_passes_per_tenant_url(self) -> None:
        tenant = _make_tenant()
        cfg = _make_config(strategy=IsolationStrategy.DATABASE)
        mgr = _make_manager(cfg=cfg)
        args = mgr._build_alembic_args(tenant)
        assert "url" in args
        assert "t-001" in args["url"] or "acme" in args["url"]

    def test_database_strategy_uses_tenant_database_url_override(self) -> None:
        custom_url = "postgresql+asyncpg://user:pass@other/tenant_override"
        tenant = _make_tenant(database_url=custom_url)
        cfg = _make_config(strategy=IsolationStrategy.DATABASE)
        mgr = _make_manager(cfg=cfg)
        args = mgr._build_alembic_args(tenant)
        assert args["url"] == custom_url

    def test_rls_strategy_passes_shared_url_no_schema(self) -> None:
        tenant = _make_tenant()
        cfg = _make_config(strategy=IsolationStrategy.RLS)
        mgr = _make_manager(cfg=cfg)
        args = mgr._build_alembic_args(tenant)
        assert args["url"] == str(cfg.database_url)
        assert "schema" not in args

    def test_hybrid_premium_resolved_to_schema(self) -> None:
        tenant = _make_tenant(id="t-premium")
        cfg = _make_config(
            strategy=IsolationStrategy.HYBRID,
            premium_tenants=["t-premium"],
            premium_isolation_strategy=IsolationStrategy.SCHEMA,
            standard_isolation_strategy=IsolationStrategy.RLS,
        )
        mgr = _make_manager(cfg=cfg)
        args = mgr._build_alembic_args(tenant)
        # Resolved to SCHEMA for premium
        assert "schema" in args

    def test_hybrid_standard_resolved_to_rls(self) -> None:
        tenant = _make_tenant(id="t-standard")
        cfg = _make_config(
            strategy=IsolationStrategy.HYBRID,
            premium_tenants=["t-premium"],  # t-standard is NOT in list
            premium_isolation_strategy=IsolationStrategy.SCHEMA,
            standard_isolation_strategy=IsolationStrategy.RLS,
        )
        mgr = _make_manager(cfg=cfg)
        args = mgr._build_alembic_args(tenant)
        # Resolved to RLS for standard → no schema arg
        assert "schema" not in args
        assert "url" in args

    def test_database_url_template_is_used_when_set(self) -> None:
        _make_tenant()
        cfg = _make_config(
            strategy=IsolationStrategy.DATABASE,
            database_url_template="postgresql+asyncpg://user:pass@host/tenant_{database_name}",
        )
        cfg.get_database_url_for_tenant.return_value = None
        mgr = _make_manager(cfg=cfg)
        # Provide tenant with no database_url so template path is exercised.
        t_no_url = _make_tenant(database_url=None)
        args = mgr._build_alembic_args(t_no_url)
        assert "url" in args


class TestRunMigrationSync:
    def test_upgrade_calls_alembic_command(self) -> None:
        tenant = _make_tenant()
        mgr = _make_manager()

        mock_cfg_instance = MagicMock()
        mock_cfg_instance.attributes = {}

        with (
            patch("fastapi_tenancy.migrations.manager._ALEMBIC_AVAILABLE", True),
            patch(
                "fastapi_tenancy.migrations.manager.AlembicConfig", return_value=mock_cfg_instance
            ),
            patch("fastapi_tenancy.migrations.manager.command") as mock_cmd,
        ):
            mgr._run_migration_sync(tenant, "upgrade", "head")
            mock_cmd.upgrade.assert_called_once_with(mock_cfg_instance, "head")
            mock_cmd.downgrade.assert_not_called()

    def test_downgrade_calls_alembic_command(self) -> None:
        tenant = _make_tenant()
        mgr = _make_manager()

        mock_cfg_instance = MagicMock()
        mock_cfg_instance.attributes = {}

        with (
            patch("fastapi_tenancy.migrations.manager._ALEMBIC_AVAILABLE", True),
            patch(
                "fastapi_tenancy.migrations.manager.AlembicConfig", return_value=mock_cfg_instance
            ),
            patch("fastapi_tenancy.migrations.manager.command") as mock_cmd,
        ):
            mgr._run_migration_sync(tenant, "downgrade", "-1")
            mock_cmd.downgrade.assert_called_once_with(mock_cfg_instance, "-1")

    def test_unknown_operation_raises_migration_error(self) -> None:
        tenant = _make_tenant()
        mgr = _make_manager()

        mock_cfg_instance = MagicMock()
        mock_cfg_instance.attributes = {}

        with (
            patch("fastapi_tenancy.migrations.manager._ALEMBIC_AVAILABLE", True),
            patch(
                "fastapi_tenancy.migrations.manager.AlembicConfig", return_value=mock_cfg_instance
            ),
            patch("fastapi_tenancy.migrations.manager.command"),
        ):
            with pytest.raises(MigrationError) as exc_info:
                mgr._run_migration_sync(tenant, "stamp", "head")
            assert exc_info.value.operation == "stamp"
            assert "Unknown migration operation" in exc_info.value.reason

    def test_alembic_not_available_raises_import_error(self) -> None:
        tenant = _make_tenant()
        mgr = _make_manager()
        with patch("fastapi_tenancy.migrations.manager._ALEMBIC_AVAILABLE", False):  # noqa: SIM117
            with pytest.raises(ImportError, match="Alembic is required"):
                mgr._run_migration_sync(tenant, "upgrade", "head")

    def test_alembic_exception_wrapped_in_migration_error(self) -> None:
        tenant = _make_tenant()
        mgr = _make_manager()

        mock_cfg_instance = MagicMock()
        mock_cfg_instance.attributes = {}

        with (
            patch("fastapi_tenancy.migrations.manager._ALEMBIC_AVAILABLE", True),
            patch(
                "fastapi_tenancy.migrations.manager.AlembicConfig", return_value=mock_cfg_instance
            ),
            patch("fastapi_tenancy.migrations.manager.command") as mock_cmd,
        ):
            mock_cmd.upgrade.side_effect = RuntimeError("DB connection refused")
            with pytest.raises(MigrationError) as exc_info:
                mgr._run_migration_sync(tenant, "upgrade", "head")
            err = exc_info.value
            assert err.tenant_id == tenant.id
            assert err.operation == "upgrade"
            assert "DB connection refused" in err.reason

    def test_x_args_set_on_alembic_config_attributes(self) -> None:
        """Ensure x_args dict is populated so env.py can read them."""
        tenant = _make_tenant()
        cfg = _make_config(strategy=IsolationStrategy.SCHEMA)
        mgr = _make_manager(cfg=cfg)

        captured_cfg: list[Any] = []
        mock_cfg_instance = MagicMock()
        mock_cfg_instance.attributes = {}

        def _capture_cfg(path: str) -> Any:
            return mock_cfg_instance

        with (
            patch("fastapi_tenancy.migrations.manager._ALEMBIC_AVAILABLE", True),
            patch("fastapi_tenancy.migrations.manager.AlembicConfig", side_effect=_capture_cfg),
            patch("fastapi_tenancy.migrations.manager.command") as mock_cmd,
        ):
            mock_cmd.upgrade.side_effect = lambda c, r: captured_cfg.append(c)
            mgr._run_migration_sync(tenant, "upgrade", "head")

        # x_args must be a dict containing at minimum "url"
        assert "x_args" in mock_cfg_instance.attributes
        assert isinstance(mock_cfg_instance.attributes["x_args"], dict)


class TestGetCurrentRevisionSync:
    def test_returns_none_when_alembic_not_available(self) -> None:
        tenant = _make_tenant()
        mgr = _make_manager()
        with patch("fastapi_tenancy.migrations.manager._ALEMBIC_AVAILABLE", False):
            result = mgr._get_current_revision_sync(tenant)
        assert result is None

    def test_returns_revision_string_from_output(self) -> None:
        tenant = _make_tenant()
        mgr = _make_manager()

        mock_cfg_instance = MagicMock()
        mock_cfg_instance.attributes = {}

        def _set_stdout(cfg: Any) -> None:
            cfg.stdout.write("abc1234def5 (head)\n")

        with (
            patch("fastapi_tenancy.migrations.manager._ALEMBIC_AVAILABLE", True),
            patch(
                "fastapi_tenancy.migrations.manager.AlembicConfig", return_value=mock_cfg_instance
            ),
            patch("fastapi_tenancy.migrations.manager.command") as mock_cmd,
        ):
            mock_cmd.current.side_effect = lambda cfg: cfg.stdout.write("abc1234 (head)\n")
            result = mgr._get_current_revision_sync(tenant)
        assert result == "abc1234 (head)"

    def test_returns_none_on_exception(self) -> None:
        tenant = _make_tenant()
        mgr = _make_manager()

        mock_cfg_instance = MagicMock()
        mock_cfg_instance.attributes = {}

        with (
            patch("fastapi_tenancy.migrations.manager._ALEMBIC_AVAILABLE", True),
            patch(
                "fastapi_tenancy.migrations.manager.AlembicConfig", return_value=mock_cfg_instance
            ),
            patch("fastapi_tenancy.migrations.manager.command") as mock_cmd,
        ):
            mock_cmd.current.side_effect = RuntimeError("connection error")
            result = mgr._get_current_revision_sync(tenant)
        assert result is None

    def test_returns_none_when_output_is_empty(self) -> None:
        tenant = _make_tenant()
        mgr = _make_manager()

        mock_cfg_instance = MagicMock()
        mock_cfg_instance.attributes = {}

        with (
            patch("fastapi_tenancy.migrations.manager._ALEMBIC_AVAILABLE", True),
            patch(
                "fastapi_tenancy.migrations.manager.AlembicConfig", return_value=mock_cfg_instance
            ),
            patch("fastapi_tenancy.migrations.manager.command") as mock_cmd,
        ):
            mock_cmd.current.side_effect = lambda cfg: None  # writes nothing
            result = mgr._get_current_revision_sync(tenant)
        assert result is None


class TestUpgradeDowngradeTenant:
    @pytest.mark.asyncio
    async def test_upgrade_tenant_delegates_to_thread_pool(self) -> None:
        tenant = _make_tenant()
        mgr = _make_manager()
        called_with: list[tuple[Any, ...]] = []

        def _fake_sync(t: Tenant, op: str, rev: str) -> None:
            called_with.append((t, op, rev))

        mgr._run_migration_sync = _fake_sync  # type: ignore[assignment]
        await mgr.upgrade_tenant(tenant, revision="head")
        assert called_with == [(tenant, "upgrade", "head")]

    @pytest.mark.asyncio
    async def test_downgrade_tenant_delegates_to_thread_pool(self) -> None:
        tenant = _make_tenant()
        mgr = _make_manager()
        called_with: list[tuple[Any, ...]] = []

        def _fake_sync(t: Tenant, op: str, rev: str) -> None:
            called_with.append((t, op, rev))

        mgr._run_migration_sync = _fake_sync  # type: ignore[assignment]
        await mgr.downgrade_tenant(tenant, revision="-1")
        assert called_with == [(tenant, "downgrade", "-1")]

    @pytest.mark.asyncio
    async def test_upgrade_tenant_wraps_exception_as_migration_error(self) -> None:
        tenant = _make_tenant()
        mgr = _make_manager()

        def _fail(*_: Any) -> None:
            raise OSError("disk full")

        mgr._run_migration_sync = _fail  # type: ignore[assignment]
        with pytest.raises(MigrationError) as exc_info:
            await mgr.upgrade_tenant(tenant, revision="head")
        err = exc_info.value
        assert err.tenant_id == tenant.id
        assert err.operation == "upgrade"
        assert "disk full" in err.reason

    @pytest.mark.asyncio
    async def test_downgrade_tenant_wraps_exception_as_migration_error(self) -> None:
        tenant = _make_tenant()
        mgr = _make_manager()

        def _fail(*_: Any) -> None:
            raise ValueError("bad revision")

        mgr._run_migration_sync = _fail  # type: ignore[assignment]
        with pytest.raises(MigrationError) as exc_info:
            await mgr.downgrade_tenant(tenant)
        assert exc_info.value.operation == "downgrade"

    @pytest.mark.asyncio
    async def test_upgrade_tenant_re_raises_migration_error_unchanged(self) -> None:
        """MigrationError from _run_migration_sync must not be double-wrapped."""
        tenant = _make_tenant()
        mgr = _make_manager()
        original = MigrationError(tenant_id=tenant.id, operation="upgrade", reason="original")

        def _fail(*_: Any) -> None:
            raise original

        mgr._run_migration_sync = _fail  # type: ignore[assignment]
        with pytest.raises(MigrationError) as exc_info:
            await mgr.upgrade_tenant(tenant)
        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_get_current_revision_returns_string(self) -> None:
        tenant = _make_tenant()
        mgr = _make_manager()
        mgr._get_current_revision_sync = lambda t: "abc1234"  # type: ignore[assignment]
        result = await mgr.get_current_revision(tenant)
        assert result == "abc1234"

    @pytest.mark.asyncio
    async def test_get_current_revision_returns_none_on_error(self) -> None:
        tenant = _make_tenant()
        mgr = _make_manager()

        def _fail(t: Tenant) -> str | None:
            raise RuntimeError("connection refused")

        mgr._get_current_revision_sync = _fail  # type: ignore[assignment]
        result = await mgr.get_current_revision(tenant)
        assert result is None


class TestMigrateOne:
    @pytest.mark.asyncio
    async def test_success_result_shape(self) -> None:
        tenant = _make_tenant()
        mgr = _make_manager()
        mgr._run_migration_sync = lambda *_: None  # type: ignore[method-assign]
        sem = asyncio.Semaphore(10)
        result = await mgr._migrate_one(tenant, "upgrade", "head", sem)
        assert result["tenant_id"] == tenant.id
        assert result["identifier"] == tenant.identifier
        assert result["success"] is True
        assert result["revision"] == "head"
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_failure_result_shape(self) -> None:
        tenant = _make_tenant()
        mgr = _make_manager()

        def _fail(*_: Any) -> None:
            raise MigrationError(tenant_id=tenant.id, operation="upgrade", reason="table locked")

        mgr._run_migration_sync = _fail  # type: ignore[assignment]
        sem = asyncio.Semaphore(10)
        result = await mgr._migrate_one(tenant, "upgrade", "head", sem)
        assert result["success"] is False
        assert "table locked" in result["error"]
        assert "revision" not in result

    @pytest.mark.asyncio
    async def test_semaphore_is_released_on_failure(self) -> None:
        tenant = _make_tenant()
        mgr = _make_manager()

        def _fail(*_: Any) -> None:
            raise MigrationError(tenant_id=tenant.id, operation="upgrade", reason="boom")

        mgr._run_migration_sync = _fail  # type: ignore[assignment]
        sem = asyncio.Semaphore(1)
        await mgr._migrate_one(tenant, "upgrade", "head", sem)
        # Semaphore should be fully released — we can acquire it immediately.
        assert sem._value == 1


class TestFleetOperations:
    def _attach_sync_noop(self, mgr: TenantMigrationManager) -> None:
        """Patch _run_migration_sync to be a no-op."""
        mgr._run_migration_sync = lambda *_: None  # type: ignore[method-assign]

    @pytest.mark.asyncio
    async def test_upgrade_all_empty_store_returns_empty_list(self) -> None:
        store = InMemoryTenantStore()
        mgr = _make_manager(store=store)
        self._attach_sync_noop(mgr)
        results = await mgr.upgrade_all(revision="head", concurrency=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_upgrade_all_migrates_all_active_tenants(self) -> None:
        store = InMemoryTenantStore()
        tenants = [_make_tenant(id=f"t-{i}", identifier=f"tenant-{i}") for i in range(5)]
        for t in tenants:
            await store.create(t)
        # Add an inactive tenant — must NOT be migrated.
        inactive = _make_tenant(
            id="t-inactive", identifier="inactive", status=TenantStatus.SUSPENDED
        )
        await store.create(inactive)

        mgr = _make_manager(store=store)
        self._attach_sync_noop(mgr)

        results = await mgr.upgrade_all(revision="head", concurrency=3)
        assert len(results) == 5
        assert all(r["success"] for r in results)
        migrated_ids = {r["tenant_id"] for r in results}
        assert "t-inactive" not in migrated_ids

    @pytest.mark.asyncio
    async def test_upgrade_all_partial_failure_reported(self) -> None:
        store = InMemoryTenantStore()
        good = _make_tenant(id="t-good", identifier="good")
        bad = _make_tenant(id="t-bad", identifier="bad")
        await store.create(good)
        await store.create(bad)

        def _maybe_fail(tenant: Tenant, op: str, rev: str) -> None:
            if tenant.id == "t-bad":
                raise MigrationError(tenant_id=tenant.id, operation=op, reason="schema missing")

        mgr = _make_manager(store=store)
        mgr._run_migration_sync = _maybe_fail  # type: ignore[assignment]

        results = await mgr.upgrade_all(revision="head", concurrency=2)
        assert len(results) == 2
        by_id = {r["tenant_id"]: r for r in results}
        assert by_id["t-good"]["success"] is True
        assert by_id["t-bad"]["success"] is False
        assert "schema missing" in by_id["t-bad"]["error"]

    @pytest.mark.asyncio
    async def test_upgrade_all_pagination(self) -> None:
        """upgrade_all must page through the store when tenant count > page_size."""
        store = InMemoryTenantStore()
        for i in range(15):
            await store.create(_make_tenant(id=f"t-{i:03d}", identifier=f"tenant-{i:03d}"))

        mgr = _make_manager(store=store)
        self._attach_sync_noop(mgr)

        results = await mgr.upgrade_all(revision="head", concurrency=5, page_size=6)
        assert len(results) == 15

    @pytest.mark.asyncio
    async def test_upgrade_all_bounded_concurrency(self) -> None:
        """Semaphore should cap concurrent workers at ``concurrency``."""
        store = InMemoryTenantStore()
        for i in range(10):
            await store.create(_make_tenant(id=f"t-{i}", identifier=f"tenant-{i}"))

        max_concurrent = 0
        active = 0
        lock = asyncio.Lock()

        async def _track(
            tenant: Tenant, op: str, rev: str, sem: asyncio.Semaphore
        ) -> dict[str, Any]:
            nonlocal max_concurrent, active
            async with sem:
                async with lock:
                    active += 1
                    max_concurrent = max(max_concurrent, active)
                await asyncio.sleep(0)  # yield to event loop
                async with lock:
                    active -= 1
            return {
                "tenant_id": tenant.id,
                "identifier": tenant.identifier,
                "success": True,
                "revision": rev,
            }

        mgr = _make_manager(store=store)
        self._attach_sync_noop(mgr)

        # Patch _migrate_one with our tracker
        with patch.object(mgr, "_migrate_one", side_effect=_track):
            await mgr.upgrade_all(revision="head", concurrency=3)

        assert max_concurrent <= 3

    @pytest.mark.asyncio
    async def test_downgrade_all_returns_results(self) -> None:
        store = InMemoryTenantStore()
        for i in range(3):
            await store.create(_make_tenant(id=f"t-{i}", identifier=f"tenant-{i}"))

        mgr = _make_manager(store=store)
        self._attach_sync_noop(mgr)

        results = await mgr.downgrade_all(revision="-1", concurrency=2)
        assert len(results) == 3
        assert all(r["success"] for r in results)

    @pytest.mark.asyncio
    async def test_upgrade_all_concurrency_1_serialises(self) -> None:
        store = InMemoryTenantStore()
        for i in range(4):
            await store.create(_make_tenant(id=f"t-{i}", identifier=f"tenant-{i}"))

        order: list[str] = []
        lock = asyncio.Lock()

        async def _ordered(
            tenant: Tenant, op: str, rev: str, sem: asyncio.Semaphore
        ) -> dict[str, Any]:
            async with sem:
                async with lock:
                    order.append(tenant.id)
                await asyncio.sleep(0)
            return {
                "tenant_id": tenant.id,
                "identifier": tenant.identifier,
                "success": True,
                "revision": rev,
            }

        mgr = _make_manager(store=store)
        self._attach_sync_noop(mgr)
        with patch.object(mgr, "_migrate_one", side_effect=_ordered):
            results = await mgr.upgrade_all(concurrency=1)
        # All 4 tenants migrated
        assert len(results) == 4


class TestAlembicConfigAttributes:
    def test_x_args_and_flat_keys_both_present(self) -> None:
        """Both cfg.attributes['url'] and cfg.attributes['x_args']['url'] must exist."""
        tenant = _make_tenant()
        cfg = _make_config(strategy=IsolationStrategy.SCHEMA)
        mgr = _make_manager(cfg=cfg)

        captured: list[MagicMock] = []
        mock_cfg_instance = MagicMock()
        mock_cfg_instance.attributes = {}

        with (
            patch("fastapi_tenancy.migrations.manager._ALEMBIC_AVAILABLE", True),
            patch(
                "fastapi_tenancy.migrations.manager.AlembicConfig", return_value=mock_cfg_instance
            ),
            patch("fastapi_tenancy.migrations.manager.command") as mock_cmd,
        ):
            mock_cmd.upgrade.side_effect = lambda c, r: captured.append(c)
            mgr._run_migration_sync(tenant, "upgrade", "head")

        attrs = mock_cfg_instance.attributes
        assert "url" in attrs
        assert "x_args" in attrs
        assert attrs["x_args"]["url"] == attrs["url"]

    def test_schema_args_present_for_schema_strategy(self) -> None:
        tenant = _make_tenant()
        cfg = _make_config(strategy=IsolationStrategy.SCHEMA)
        mgr = _make_manager(cfg=cfg)

        mock_cfg_instance = MagicMock()
        mock_cfg_instance.attributes = {}

        with (
            patch("fastapi_tenancy.migrations.manager._ALEMBIC_AVAILABLE", True),
            patch(
                "fastapi_tenancy.migrations.manager.AlembicConfig", return_value=mock_cfg_instance
            ),
            patch("fastapi_tenancy.migrations.manager.command"),
        ):
            mgr._run_migration_sync(tenant, "upgrade", "head")

        assert "schema" in mock_cfg_instance.attributes
        assert (
            mock_cfg_instance.attributes["schema"]
            == mock_cfg_instance.attributes["x_args"]["schema"]
        )

    def test_rls_no_schema_key(self) -> None:
        tenant = _make_tenant()
        cfg = _make_config(strategy=IsolationStrategy.RLS)
        mgr = _make_manager(cfg=cfg)

        mock_cfg_instance = MagicMock()
        mock_cfg_instance.attributes = {}

        with (
            patch("fastapi_tenancy.migrations.manager._ALEMBIC_AVAILABLE", True),
            patch(
                "fastapi_tenancy.migrations.manager.AlembicConfig", return_value=mock_cfg_instance
            ),
            patch("fastapi_tenancy.migrations.manager.command"),
        ):
            mgr._run_migration_sync(tenant, "upgrade", "head")

        assert "schema" not in mock_cfg_instance.attributes


class TestFleetLogging:
    @pytest.mark.asyncio
    async def test_upgrade_all_logs_success_summary(self, caplog: Any) -> None:

        store = InMemoryTenantStore()
        for i in range(3):
            await store.create(_make_tenant(id=f"t-{i}", identifier=f"tenant-{i}"))

        mgr = _make_manager(store=store)
        mgr._run_migration_sync = lambda *_: None  # type: ignore[method-assign]

        with caplog.at_level(logging.INFO, logger="fastapi_tenancy.migrations.manager"):
            results = await mgr.upgrade_all(revision="head")

        assert len(results) == 3
        assert any("3/3" in record.message for record in caplog.records)
