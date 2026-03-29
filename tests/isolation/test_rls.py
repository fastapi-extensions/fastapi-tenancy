"""Tests for :class:`~fastapi_tenancy.isolation.rls.RLSIsolationProvider`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa

from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.exceptions import ConfigurationError, IsolationError
from fastapi_tenancy.core.types import IsolationStrategy, Tenant, TenantStatus
from fastapi_tenancy.isolation.rls import _RLS_GUC, _TENANT_COLUMN, RLSIsolationProvider

if TYPE_CHECKING:
    from collections.abc import Callable


def _pg_cfg(url: str = "postgresql+asyncpg://u:p@localhost/db", **kw: Any) -> TenancyConfig:
    return TenancyConfig(
        database_url=url,
        isolation_strategy=IsolationStrategy.RLS,
        schema_prefix="t_",
        database_echo=False,
        database_pool_size=2,
        database_max_overflow=2,
        database_pool_timeout=10,
        database_pool_recycle=300,
        database_pool_pre_ping=False,
        **kw,
    )


def _sqlite_cfg() -> TenancyConfig:
    return TenancyConfig(
        database_url="sqlite+aiosqlite:///:memory:",
        isolation_strategy=IsolationStrategy.SCHEMA,  # valid for schema
        schema_prefix="t_",
        database_echo=False,
        database_pool_size=2,
        database_max_overflow=2,
        database_pool_timeout=10,
        database_pool_recycle=300,
        database_pool_pre_ping=False,
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


@pytest.fixture
def rls_metadata() -> sa.MetaData:
    meta = sa.MetaData()
    sa.Table(
        "orders",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("amount", sa.Float),
    )
    # Table WITHOUT tenant_id — must be skipped by destroy_tenant
    sa.Table(
        "global_config",
        meta,
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.String(255)),
    )
    return meta


@pytest.mark.unit
class TestRLSConstruction:
    def test_non_pg_dialect_raises_configuration_error(self) -> None:
        _sqlite_cfg()
        # Override isolation_strategy to RLS to get past TenancyConfig validation
        # We test the provider-level check by creating a PG config then patching url
        cfg_rls = TenancyConfig(
            database_url="sqlite+aiosqlite:///:memory:",
            isolation_strategy=IsolationStrategy.RLS,
            schema_prefix="t_",
            database_echo=False,
            database_pool_size=2,
            database_max_overflow=2,
            database_pool_timeout=10,
            database_pool_recycle=300,
            database_pool_pre_ping=False,
        )
        with pytest.raises(ConfigurationError, match="RLS isolation requires PostgreSQL"):
            RLSIsolationProvider(cfg_rls)

    def test_mysql_raises_configuration_error(self) -> None:
        cfg = TenancyConfig(
            database_url="mysql+aiomysql://u:p@localhost/db",
            isolation_strategy=IsolationStrategy.RLS,
            schema_prefix="t_",
            database_echo=False,
            database_pool_size=2,
            database_max_overflow=2,
            database_pool_timeout=10,
            database_pool_recycle=300,
            database_pool_pre_ping=False,
        )
        with pytest.raises(ConfigurationError):
            RLSIsolationProvider(cfg)

    def test_pg_accepted(self) -> None:
        cfg = _pg_cfg()
        p = RLSIsolationProvider(cfg)
        assert p is not None

    def test_shared_engine_injected(self, sqlite_engine: Any) -> None:
        # We can't use SQLite for RLS, but we can test engine injection
        # by mocking the dialect check
        cfg = _pg_cfg()
        from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

        pg_like_engine = create_async_engine("postgresql+asyncpg://u:p@localhost/db")
        p = RLSIsolationProvider(cfg, engine=pg_like_engine)
        assert p.engine is pg_like_engine


@pytest.mark.unit
class TestRLSApplyFilters:
    async def test_adds_tenant_id_where_clause(self, make_tenant: Callable[..., Tenant]) -> None:
        cfg = _pg_cfg()
        p = RLSIsolationProvider(cfg)
        meta = sa.MetaData()
        tbl = sa.Table(
            "orders",
            meta,
            sa.Column("id", sa.Integer),
            sa.Column("tenant_id", sa.String),
        )
        q = sa.select(tbl)
        t = make_tenant()
        filtered = await p.apply_filters(q, t)
        compiled = str(filtered.compile(compile_kwargs={"literal_binds": False}))
        assert "WHERE" in compiled

    async def test_preserves_query_type(self, make_tenant: Callable[..., Tenant]) -> None:
        cfg = _pg_cfg()
        p = RLSIsolationProvider(cfg)
        meta = sa.MetaData()
        tbl = sa.Table("t", meta, sa.Column("id", sa.Integer))
        q = sa.select(tbl)
        t = make_tenant()
        result = await p.apply_filters(q, t)
        assert hasattr(result, "where")


@pytest.mark.unit
class TestRLSInitializeTenant:
    async def test_no_metadata_is_noop(self, make_tenant: Callable[..., Tenant]) -> None:
        cfg = _pg_cfg()
        p = RLSIsolationProvider(cfg)
        t = make_tenant()
        # Should complete without error and without hitting DB
        await p.initialize_tenant(t, metadata=None)

    async def test_with_metadata_raises_isolation_error_on_db_failure(
        self,
        make_tenant: Callable[..., Tenant],
        rls_metadata: sa.MetaData,
    ) -> None:
        cfg = _pg_cfg()
        p = RLSIsolationProvider(cfg)
        t = make_tenant()
        # Engine will fail to connect — should wrap as IsolationError
        with pytest.raises(IsolationError, match="initialize_tenant"):
            await p.initialize_tenant(t, metadata=rls_metadata)


@pytest.mark.unit
class TestRLSDestroyTenant:
    async def test_no_metadata_raises_isolation_error(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        cfg = _pg_cfg()
        p = RLSIsolationProvider(cfg)
        t = make_tenant()
        with pytest.raises(IsolationError, match="destroy_tenant"):
            await p.destroy_tenant(t)

    async def test_metadata_kwarg_missing_raises_isolation_error(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        cfg = _pg_cfg()
        p = RLSIsolationProvider(cfg)
        t = make_tenant()
        with pytest.raises(IsolationError):
            await p.destroy_tenant(t, metadata=None)


@pytest.mark.unit
class TestRLSVerifyIsolation:
    async def test_returns_false_on_connection_error(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        cfg = _pg_cfg()
        p = RLSIsolationProvider(cfg)
        t = make_tenant()
        # PostgreSQL not running in unit test environment
        result = await p.verify_isolation(t)
        assert result is False


@pytest.mark.unit
class TestRLSClose:
    async def test_close_disposes_engine(self) -> None:
        cfg = _pg_cfg()
        p = RLSIsolationProvider(cfg)
        await p.close()


@pytest.mark.unit
class TestRLSConstants:
    def test_guc_name(self) -> None:
        assert _RLS_GUC == "app.current_tenant"

    def test_tenant_column_name(self) -> None:
        assert _TENANT_COLUMN == "tenant_id"


@pytest.mark.e2e
class TestRLSPostgresIntegration:
    async def test_get_session_sets_guc(
        self,
        pg_rls_provider: RLSIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant()
        async with pg_rls_provider.get_session(t) as session:
            result = await session.execute(
                sa.text("SELECT current_setting('app.current_tenant', TRUE)")
            )
            guc_val = result.scalar()
            assert guc_val == t.id

    async def test_get_session_exception_wrapped(
        self,
        pg_rls_provider: RLSIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant()
        with pytest.raises(IsolationError):
            async with pg_rls_provider.get_session(t):
                raise RuntimeError("handler error")

    async def test_initialize_with_metadata_creates_tables(
        self,
        pg_rls_provider: RLSIsolationProvider,
        make_tenant: Callable[..., Tenant],
        rls_metadata: sa.MetaData,
    ) -> None:
        t = make_tenant()
        await pg_rls_provider.initialize_tenant(t, metadata=rls_metadata)
        # Tables must now exist (idempotent)
        await pg_rls_provider.initialize_tenant(t, metadata=rls_metadata)

    async def test_destroy_deletes_rows(
        self,
        pg_rls_provider: RLSIsolationProvider,
        make_tenant: Callable[..., Tenant],
        rls_metadata: sa.MetaData,
    ) -> None:
        t = make_tenant()
        await pg_rls_provider.initialize_tenant(t, metadata=rls_metadata)
        # Insert a row
        async with pg_rls_provider.get_session(t) as session:
            await session.execute(
                sa.text("INSERT INTO orders (tenant_id, amount) VALUES (:tid, :amt)"),
                {"tid": t.id, "amt": 100.0},
            )
            await session.commit()
        # Destroy must delete all rows for this tenant
        await pg_rls_provider.destroy_tenant(t, metadata=rls_metadata)
        async with pg_rls_provider.engine.connect() as conn:
            result = await conn.execute(
                sa.text("SELECT COUNT(*) FROM orders WHERE tenant_id = :tid"),
                {"tid": t.id},
            )
            assert result.scalar() == 0

    async def test_verify_isolation_returns_true(
        self,
        pg_rls_provider: RLSIsolationProvider,
        make_tenant: Callable[..., Tenant],
    ) -> None:
        t = make_tenant()
        assert await pg_rls_provider.verify_isolation(t) is True


@pytest.mark.integration
class TestRLSListenerCleanup:
    """FIX: RLS begin listener removed from DBAPI connection before pool return.

    Before the fix:
        ``@event.listens_for(sync_conn, "begin")`` used the decorator form,
        which never removes the listener.  When the physical connection was
        returned to the pool and reused for a different tenant, the stale
        listener fired at the start of the new transaction — silently setting
        the GUC to the old tenant's ID.

    After the fix:
        The listener function is defined as a named local, registered with
        ``event.listen``, and removed with ``event.remove`` in a ``finally``
        block that wraps the entire session lifetime (including the
        ``AsyncSession()`` constructor call).
    """

    def _make_pg_rls_provider(self) -> Any:
        """Return an RLSIsolationProvider backed by a real PG, or skip."""
        import os  # noqa: PLC0415
        import socket  # noqa: PLC0415

        pg_url = os.getenv(
            "POSTGRES_URL",
            "postgresql+asyncpg://testing:Testing123!@localhost:5432/test_db",
        )
        try:
            host = pg_url.split("@")[1].split("/")[0].split(":")[0]
            port = (
                int(pg_url.split("@")[1].split("/")[0].split(":")[1])
                if ":" in pg_url.split("@")[1].split("/")[0]
                else 5432
            )
            with socket.create_connection((host, port), timeout=1):
                pass
        except OSError:
            pytest.skip("PostgreSQL not reachable — skipping RLS listener test")

        from fastapi_tenancy.isolation.rls import RLSIsolationProvider  # noqa: PLC0415

        cfg = TenancyConfig(
            database_url=pg_url,
            isolation_strategy=IsolationStrategy.RLS,
            schema_prefix="t_",
            database_echo=False,
            database_pool_size=2,
            database_max_overflow=2,
            database_pool_timeout=10,
            database_pool_recycle=300,
            database_pool_pre_ping=False,
        )
        return RLSIsolationProvider(cfg)

    async def test_listener_removed_after_normal_close(self) -> None:
        """After get_session exits normally, no begin listener remains on sync_conn."""

        provider = self._make_pg_rls_provider()
        tenant = _make_tenant()

        captured_sync_conn: list[Any] = []

        async with provider.get_session(tenant) as session:
            conn = await session.connection()
            captured_sync_conn.append(conn.sync_connection)

        sync_conn = captured_sync_conn[0]

        # After the session closes, no _set_rls_guc listener should remain.
        remaining = [
            fn
            for fn in getattr(sync_conn.dispatch, "begin", [])
            if "_set_rls_guc" in getattr(fn, "__qualname__", "")
        ]
        assert remaining == [], f"RLS GUC listener leaked after normal session close: {remaining}"
        await provider.close()

    async def test_listener_removed_after_exception(self) -> None:
        """After get_session exits via exception, no begin listener remains."""

        provider = self._make_pg_rls_provider()
        tenant = _make_tenant()
        captured_sync_conn: list[Any] = []

        with pytest.raises(IsolationError):  # noqa: PT012
            async with provider.get_session(tenant) as session:
                conn = await session.connection()
                captured_sync_conn.append(conn.sync_connection)
                raise RuntimeError("handler blew up")

        if captured_sync_conn:
            sync_conn = captured_sync_conn[0]
            remaining = [
                fn
                for fn in getattr(sync_conn.dispatch, "begin", [])
                if "_set_rls_guc" in getattr(fn, "__qualname__", "")
            ]
            assert remaining == [], f"RLS GUC listener leaked after exception: {remaining}"
        await provider.close()

    async def test_guc_not_reused_across_tenants(self) -> None:
        """A connection reused from the pool must not carry a stale tenant GUC.

        Opens session for tenant A, closes it (connection returns to pool),
        then opens session for tenant B on the same connection and verifies
        the GUC reflects B's ID — not A's.
        """
        provider = self._make_pg_rls_provider()
        t_a = _make_tenant(tenant_id="t-rls-a", identifier="rls-alpha")
        t_b = _make_tenant(tenant_id="t-rls-b", identifier="rls-beta")

        async with provider.get_session(t_a) as session:
            result = await session.execute(
                sa.text("SELECT current_setting('app.current_tenant', TRUE)")
            )
            assert result.scalar() == t_a.id

        # Reuse the connection for tenant B.
        async with provider.get_session(t_b) as session:
            result = await session.execute(
                sa.text("SELECT current_setting('app.current_tenant', TRUE)")
            )
            guc_value = result.scalar()
            assert guc_value == t_b.id, (
                f"Pool connection carried stale GUC from tenant A ({t_a.id!r}); "
                f"got {guc_value!r} instead of {t_b.id!r}"
            )

        await provider.close()
