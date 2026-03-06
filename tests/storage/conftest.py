"""Shared fixtures for the storage test package."""

from __future__ import annotations

from datetime import UTC, datetime
import os
import socket
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
import sqlalchemy as sa

from fastapi_tenancy.core.types import IsolationStrategy, Tenant, TenantStatus
from fastapi_tenancy.storage.database import SQLAlchemyTenantStore
from fastapi_tenancy.storage.memory import InMemoryTenantStore
from fastapi_tenancy.storage.redis import RedisTenantStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable


_SQLITE_MEM: str = "sqlite+aiosqlite:///:memory:"

_PG_URL: str = os.getenv(
    "POSTGRES_URL",
    "postgresql+asyncpg://testing:Testing123!@localhost:5432/test_db",
)
_MYSQL_URL: str = os.getenv(
    "MYSQL_URL",
    "mysql+aiomysql://testing:Testing123!@localhost:3306/test_db",
)
_MSSQL_URL: str = os.getenv(
    "MSSQL_URL",
    "mssql+aioodbc://sa:Testing123!@localhost:1433/test_db"
    "?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes",
)


def _tcp_ok(host: str, port: int, *, timeout: float = 1.0) -> bool:
    """Return True when a TCP connection to *host*:*port* succeeds within *timeout* s."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _pg_up() -> bool:
    return _tcp_ok("localhost", 5432)


def _mysql_up() -> bool:
    return _tcp_ok("localhost", 3306)


def _mssql_up() -> bool:
    return _tcp_ok("localhost", 1433)


@pytest.fixture
def make_tenant() -> Callable[..., Tenant]:
    """Return a counter-based :class:`Tenant` factory with all fields overridable.

    Each call increments an internal counter used to generate unique IDs and
    identifiers, preventing PK / unique-constraint collisions between tests.

    Example::

        def test_something(make_tenant):
            t = make_tenant(name="Acme", status=TenantStatus.SUSPENDED)
            assert t.name == "Acme"
    """
    counter: list[int] = [0]

    def _factory(
        *,
        tenant_id: str | None = None,
        identifier: str | None = None,
        name: str | None = None,
        status: TenantStatus = TenantStatus.ACTIVE,
        isolation_strategy: IsolationStrategy | None = None,
        metadata: dict[str, Any] | None = None,
        schema_name: str | None = None,
        database_url: str | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ) -> Tenant:
        counter[0] += 1
        n = counter[0]
        now = datetime.now(UTC)
        return Tenant(
            id=tenant_id or f"t-store-{n:06d}",
            identifier=identifier or f"store-tenant-{n:06d}",
            name=name or f"Store Tenant {n}",
            status=status,
            isolation_strategy=isolation_strategy,
            metadata=metadata if metadata is not None else {},
            schema_name=schema_name,
            database_url=database_url,
            created_at=created_at or now,
            updated_at=updated_at or now,
        )

    return _factory


@pytest_asyncio.fixture
async def sqlite_store() -> AsyncIterator[SQLAlchemyTenantStore]:
    """Yield an initialised :class:`SQLAlchemyTenantStore` backed by in-memory SQLite.

    Uses ``StaticPool`` so the same connection is reused across the session
    (required for ``:memory:`` SQLite).  The store is closed and the pool
    disposed in the finally block.
    """
    store = SQLAlchemyTenantStore(_SQLITE_MEM)
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


@pytest_asyncio.fixture
async def postgres_store() -> AsyncIterator[SQLAlchemyTenantStore]:
    """Yield a PostgreSQL :class:`SQLAlchemyTenantStore`; skip when PG is down.

    Marks: ``pytest.mark.e2e``.
    Creates a fresh ``tenants`` table for each test via ``initialize()`` and
    truncates it in teardown to keep tests independent.
    """
    if not _pg_up():
        pytest.skip("PostgreSQL not reachable on localhost:5432")

    store = SQLAlchemyTenantStore(
        _PG_URL,
        pool_size=2,
        max_overflow=2,
        pool_pre_ping=True,
    )
    await store.initialize()
    try:
        yield store
    finally:
        # Truncate the tenants table so tests stay independent without
        # dropping/re-creating the schema on every teardown.
        try:
            async with store._engine.begin() as conn:
                await conn.execute(sa.text("TRUNCATE TABLE tenants RESTART IDENTITY CASCADE"))
        except Exception:
            pass
        await store.close()


@pytest_asyncio.fixture
async def mysql_store() -> AsyncIterator[SQLAlchemyTenantStore]:
    """Yield a MySQL :class:`SQLAlchemyTenantStore`; skip when MySQL is down.

    Marks: ``pytest.mark.e2e``.
    """
    if not _mysql_up():
        pytest.skip("MySQL not reachable on localhost:3306")

    store = SQLAlchemyTenantStore(
        _MYSQL_URL,
        pool_size=2,
        max_overflow=2,
        pool_pre_ping=True,
    )
    await store.initialize()
    try:
        yield store
    finally:
        try:
            async with store._engine.begin() as conn:
                await conn.execute(sa.text("DELETE FROM tenants"))
        except Exception:
            pass
        await store.close()


@pytest_asyncio.fixture
async def mssql_store() -> AsyncIterator[SQLAlchemyTenantStore]:
    """Yield an MSSQL :class:`SQLAlchemyTenantStore`; skip when SQL Server is down.

    Marks: ``pytest.mark.e2e``.
    """
    if not _mssql_up():
        pytest.skip("SQL Server not reachable on localhost:1433")

    store = SQLAlchemyTenantStore(
        _MSSQL_URL,
        pool_size=2,
        max_overflow=2,
        pool_pre_ping=True,
    )
    await store.initialize()
    try:
        yield store
    finally:
        try:
            async with store._engine.begin() as conn:
                await conn.execute(sa.text("DELETE FROM tenants"))
        except Exception:
            pass
        await store.close()


@pytest_asyncio.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.e2e),
        pytest.param("mysql", id="mysql", marks=pytest.mark.e2e),
        pytest.param("mssql", id="mssql", marks=pytest.mark.e2e),
    ]
)
async def any_sqla_store(  # noqa: PLR0912, PLR0915
    request: pytest.FixtureRequest,
) -> AsyncIterator[SQLAlchemyTenantStore]:
    """Parametrised fixture that yields each available SQLAlchemy store.

    ``test_sqla_contract.py`` depends on this fixture to run the shared
    contract suite against *every* reachable database backend.

    SQLite is always available.  PostgreSQL, MySQL, and MSSQL are skipped
    automatically when their respective servers are not reachable.
    """
    backend: str = request.param

    if backend == "sqlite":
        store = SQLAlchemyTenantStore(_SQLITE_MEM)
        await store.initialize()
        try:
            yield store
        finally:
            await store.close()

    elif backend == "postgres":
        if not _pg_up():
            pytest.skip("PostgreSQL not reachable on localhost:5432")
        store = SQLAlchemyTenantStore(_PG_URL, pool_size=2, max_overflow=2)
        await store.initialize()
        try:
            yield store
        finally:
            try:
                async with store._engine.begin() as conn:
                    await conn.execute(sa.text("TRUNCATE TABLE tenants RESTART IDENTITY CASCADE"))
            except Exception:
                pass
            await store.close()

    elif backend == "mysql":
        if not _mysql_up():
            pytest.skip("MySQL not reachable on localhost:3306")
        store = SQLAlchemyTenantStore(_MYSQL_URL, pool_size=2, max_overflow=2)
        await store.initialize()
        try:
            yield store
        finally:
            try:
                async with store._engine.begin() as conn:
                    await conn.execute(sa.text("DELETE FROM tenants"))
            except Exception:
                pass
            await store.close()

    elif backend == "mssql":
        if not _mssql_up():
            pytest.skip("SQL Server not reachable on localhost:1433")
        store = SQLAlchemyTenantStore(_MSSQL_URL, pool_size=2, max_overflow=2)
        await store.initialize()
        try:
            yield store
        finally:
            try:
                async with store._engine.begin() as conn:
                    await conn.execute(sa.text("DELETE FROM tenants"))
            except Exception:
                pass
            await store.close()

    else:
        pytest.fail(f"Unknown backend parameter: {backend!r}")  # pragma: no cover


class FakeRedis:
    """Minimal in-memory Redis mock covering the API surface used by RedisTenantStore.

    Implements:
        - ``get`` / ``set`` / ``setex`` / ``delete`` / ``exists``
        - ``pipeline()`` (returns a :class:`FakePipeline` that batches setex/get)
        - ``scan_iter(match, count)`` — async generator that yields matching keys
        - ``aclose()`` — no-op coroutine

    TTLs are accepted in ``setex`` but not enforced (unit-test behaviour).
    The internal key store uses ``bytes`` keys/values to mirror the real
    redis-py client's ``decode_responses=False`` mode.
    """

    def __init__(self) -> None:
        # Use str keys so tests can look up with plain strings; values stay bytes
        # to mirror redis-py decode_responses=False.
        self._store: dict[str, bytes] = {}
        # AsyncMock so tests can call assert_awaited_once() on it.
        self.aclose: AsyncMock = AsyncMock()

    @staticmethod
    def _key(key: str | bytes) -> str:
        """Normalise *key* to str for consistent dict lookups."""
        return key.decode() if isinstance(key, bytes) else key

    async def get(self, key: str | bytes) -> bytes | None:
        return self._store.get(self._key(key))

    async def set(self, key: str | bytes, value: bytes) -> None:
        self._store[self._key(key)] = value

    async def setex(self, key: str | bytes, _ttl: int, value: bytes) -> None:
        """Store *value* under *key*; TTL is accepted but not enforced."""
        self._store[self._key(key)] = value

    async def delete(self, *keys: str | bytes) -> int:
        deleted = 0
        for key in keys:
            k = self._key(key)
            if k in self._store:
                del self._store[k]
                deleted += 1
        return deleted

    async def exists(self, *keys: str | bytes) -> int:
        return sum(1 for k in keys if self._key(k) in self._store)

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    async def scan_iter(self, match: str = "*", count: int = 100) -> AsyncIterator[bytes]:
        """Yield keys (as bytes) whose str form matches the glob *match*."""
        import fnmatch  # noqa: PLC0415

        for key in list(self._store.keys()):
            if fnmatch.fnmatchcase(key, match):
                yield key.encode()

    # aclose is initialised as AsyncMock in __init__ for testability.


class FakePipeline:
    """Batched command buffer returned by :meth:`FakeRedis.pipeline`.

    Stores ``(method_name, args)`` tuples and executes them sequentially
    when :meth:`execute` is called.  Only ``setex`` and ``get`` are
    currently required by :class:`RedisTenantStore`.
    """

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._cmds: list[tuple[str, tuple[Any, ...]]] = []

    def setex(self, key: str | bytes, ttl: int, value: bytes) -> FakePipeline:
        self._cmds.append(("setex", (key, ttl, value)))
        return self

    def get(self, key: str | bytes) -> FakePipeline:
        self._cmds.append(("get", (key,)))
        return self

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for method, args in self._cmds:
            result = await getattr(self._redis, method)(*args)
            results.append(result)
        self._cmds.clear()
        return results


@pytest.fixture
def fake_redis() -> FakeRedis:
    """Return a fresh :class:`FakeRedis` instance for each test.

    Using a function-scoped fixture guarantees complete state isolation
    between tests — no key leakage across test boundaries.
    """
    return FakeRedis()


@pytest.fixture
def redis_store(fake_redis: FakeRedis) -> RedisTenantStore:
    """Return a :class:`RedisTenantStore` wired to *fake_redis* + :class:`InMemoryTenantStore`.

    The ``key_prefix`` is set to ``"test-tenant"`` so key helper assertions
    in tests can use deterministic string comparisons.

    Lifecycle note:
        This fixture is function-scoped.  :class:`InMemoryTenantStore` holds
        no external resources, and :class:`FakeRedis` is just a dict, so
        teardown is implicit — no ``close()`` call is needed.
    """
    primary = InMemoryTenantStore()

    # Build the RedisTenantStore but swap out the internal redis client
    # with our FakeRedis before any tests run.  We do this by patching
    # ``_require_redis`` at import time so the constructor never tries to
    # connect to a real Redis server.
    store = _build_redis_store_with_fake(primary, fake_redis, key_prefix="test-tenant")
    return store


def _build_redis_store_with_fake(
    primary: InMemoryTenantStore,
    fake: FakeRedis,
    *,
    key_prefix: str = "test-tenant",
    ttl: int = 3600,
) -> RedisTenantStore:
    """Construct a :class:`RedisTenantStore` that uses *fake* instead of a real Redis connection.

    RedisTenantStore.__init__ calls ``_require_redis()`` to import ``redis.asyncio``
    and then calls ``aioredis.from_url()``.  We bypass that entirely by:
    1. Temporarily monkey-patching ``_require_redis`` to return a mock module
       whose ``from_url`` returns our :class:`FakeRedis` instance.
    2. Building the store inside the patched consa.text.
    3. Restoring the original module state.
    """

    fake_aioredis = MagicMock()
    fake_aioredis.from_url = MagicMock(return_value=fake)

    fake_redis_module = MagicMock()
    fake_redis_module.asyncio = fake_aioredis

    with patch("fastapi_tenancy.storage.redis._require_redis", return_value=fake_aioredis):
        store = RedisTenantStore(
            redis_url="redis://localhost:6379/0",
            primary_store=primary,
            ttl=ttl,
            key_prefix=key_prefix,
        )

    # Replace the internal redis client with our fake instance
    # (the constructor may have stored whatever from_url returned — ensure
    # it is exactly our FakeRedis).
    store._redis = fake
    return store
