"""Redis write-through cache layer for tenant storage.

This module provides :class:`RedisTenantStore`, a transparent caching wrapper
around any :class:`~fastapi_tenancy.storage.tenant_store.TenantStore`.

Cache architecture
------------------
::

    HTTP request
        │
        ▼
    RedisTenantStore.get_by_identifier("acme")
        │
        ├── Redis HIT  ──→ deserialise → return Tenant   (< 1 ms)
        │
        └── Redis MISS
                │
                ▼
            SQLAlchemyTenantStore.get_by_identifier("acme")
                │
                ▼
            _cache_set(tenant)        ← pipeline: setex id + setex slug
                │
                ▼
            return Tenant

Cache keys
----------
* ``{prefix}:id:{tenant_id}``          — primary-key lookup
* ``{prefix}:identifier:{identifier}`` — slug lookup

Both keys store the same serialised :class:`~fastapi_tenancy.core.types.Tenant`
blob and share the same TTL.  All mutating operations invalidate both keys.

Serialisation
-------------
:meth:`Tenant.model_dump_json` / :meth:`Tenant.model_validate_json` are used
instead of :func:`json.dumps` / :func:`json.loads` so that
:class:`~datetime.datetime` objects, :class:`~enum.StrEnum` values, and other
Pydantic-managed types round-trip without loss.

Cache invalidation
------------------
Bulk invalidation uses ``SCAN`` (with a count hint) rather than ``KEYS`` to
avoid blocking the Redis event loop on large keyspaces.

Installation
------------
Requires the ``redis`` extra::

    pip install fastapi-tenancy[redis]
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from redis import asyncio as aioredis

from fastapi_tenancy.core.exceptions import TenantNotFoundError
from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.storage.tenant_store import TenantStore

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


class RedisTenantStore(TenantStore):
    """Redis write-through cache on top of a primary :class:`TenantStore`.

    Reads are served from Redis when the key is warm.  Every write goes to
    the primary store first and then populates (or invalidates) the cache —
    Redis is never the source of truth.

    Args:
        redis_url: Redis connection URL (e.g. ``redis://localhost:6379/0``).
        primary_store: Backing :class:`TenantStore` for cold reads and writes.
        ttl: Cache TTL in seconds (default 3600 = 1 hour).
        key_prefix: Prefix applied to all Redis keys.  Use a distinct prefix
            per application to avoid key collisions in a shared Redis instance.

    Example::

        from fastapi_tenancy.storage.database import SQLAlchemyTenantStore
        from fastapi_tenancy.storage.redis import RedisTenantStore

        primary = SQLAlchemyTenantStore(
            database_url="postgresql+asyncpg://user:pass@localhost/myapp"
        )
        await primary.initialize()

        cache = RedisTenantStore(
            redis_url="redis://localhost:6379/0",
            primary_store=primary,
            ttl=1800,
        )
        tenant = await cache.get_by_identifier("acme-corp")
    """

    def __init__(
        self,
        redis_url: str,
        primary_store: TenantStore,
        ttl: int = 3600,
        key_prefix: str = "tenant",
    ) -> None:
        self._primary = primary_store
        self._ttl = ttl
        self._prefix = key_prefix
        self._redis: aioredis.Redis = aioredis.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=False,
        )
        logger.info(
            "RedisTenantStore initialised ttl=%ds prefix=%s", ttl, key_prefix
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _id_key(self, tenant_id: str) -> str:
        return f"{self._prefix}:id:{tenant_id}"

    def _slug_key(self, identifier: str) -> str:
        return f"{self._prefix}:identifier:{identifier}"

    def _serialize(self, tenant: Tenant) -> bytes:
        """Serialise to bytes via Pydantic's own JSON encoder."""
        return tenant.model_dump_json().encode("utf-8")

    def _deserialize(self, data: bytes) -> Tenant:
        """Deserialise from bytes using Pydantic's type-safe JSON parser."""
        return Tenant.model_validate_json(data.decode("utf-8"))

    async def _cache_set(self, tenant: Tenant) -> None:
        """Write both cache keys atomically using a Redis pipeline."""
        serialised = self._serialize(tenant)
        pipe = self._redis.pipeline()
        pipe.setex(self._id_key(tenant.id), self._ttl, serialised)
        pipe.setex(self._slug_key(tenant.identifier), self._ttl, serialised)
        await pipe.execute()
        logger.debug("Cached tenant id=%s ttl=%ds", tenant.id, self._ttl)

    async def _cache_invalidate(self, tenant_id: str, identifier: str) -> None:
        """Delete both cache keys for a tenant."""
        await self._redis.delete(
            self._id_key(tenant_id),
            self._slug_key(identifier),
        )
        logger.debug("Invalidated cache tenant id=%s", tenant_id)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def get_by_id(self, tenant_id: str) -> Tenant:
        """Return tenant from cache; fall back to primary on miss.

        Args:
            tenant_id: Opaque tenant primary key.

        Returns:
            Matching :class:`~fastapi_tenancy.core.types.Tenant`.

        Raises:
            TenantNotFoundError: Propagated from the primary store.
        """
        cached = await self._redis.get(self._id_key(tenant_id))
        if cached is not None:
            logger.debug("Cache HIT id=%s", tenant_id)
            return self._deserialize(cached)
        logger.debug("Cache MISS id=%s", tenant_id)
        tenant = await self._primary.get_by_id(tenant_id)
        await self._cache_set(tenant)
        return tenant

    async def get_by_identifier(self, identifier: str) -> Tenant:
        """Return tenant from cache; fall back to primary on miss.

        Args:
            identifier: Human-readable tenant slug.

        Returns:
            Matching :class:`~fastapi_tenancy.core.types.Tenant`.

        Raises:
            TenantNotFoundError: Propagated from the primary store.
        """
        cached = await self._redis.get(self._slug_key(identifier))
        if cached is not None:
            logger.debug("Cache HIT identifier=%s", identifier)
            return self._deserialize(cached)
        logger.debug("Cache MISS identifier=%s", identifier)
        tenant = await self._primary.get_by_identifier(identifier)
        await self._cache_set(tenant)
        return tenant

    async def list(
        self,
        skip: int = 0,
        limit: int = 100,
        status: TenantStatus | None = None,
    ) -> Iterable[Tenant]:
        """Delegate list to primary — paginated results are not cached.

        Caching paginated list results adds complex invalidation overhead
        for negligible benefit.  Always reads from the primary store.

        Args:
            skip: Pagination offset.
            limit: Maximum number of results.
            status: Optional status filter.

        Returns:
            Tenants from the primary store.
        """
        return await self._primary.list(skip=skip, limit=limit, status=status)

    async def count(self, status: TenantStatus | None = None) -> int:
        """Delegate count to primary store.

        Args:
            status: Optional status filter.

        Returns:
            Total matching tenant count.
        """
        return await self._primary.count(status=status)

    async def exists(self, tenant_id: str) -> bool:
        """Return ``True`` if the tenant exists in cache or primary.

        Checks Redis first (O(1)); falls back to primary on a cache miss.

        Args:
            tenant_id: Opaque tenant primary key.

        Returns:
            Existence flag.
        """
        if await self._redis.exists(self._id_key(tenant_id)):
            return True
        return await self._primary.exists(tenant_id)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def create(self, tenant: Tenant) -> Tenant:
        """Create in primary, then populate cache.

        Args:
            tenant: Fully-populated tenant object.

        Returns:
            Stored tenant with server-generated timestamps.

        Raises:
            ValueError: When ``id`` or ``identifier`` already exists.
        """
        created = await self._primary.create(tenant)
        await self._cache_set(created)
        logger.info("Created and cached tenant id=%s", created.id)
        return created

    async def update(self, tenant: Tenant) -> Tenant:
        """Update primary, invalidate old cache keys, repopulate with new values.

        Fetches the old tenant first to invalidate the old slug key in case
        the ``identifier`` field changed.

        Args:
            tenant: Updated tenant object.

        Returns:
            Updated tenant.

        Raises:
            TenantNotFoundError: When ``tenant.id`` does not exist.
        """
        old = await self._primary.get_by_id(tenant.id)
        updated = await self._primary.update(tenant)
        await self._cache_invalidate(old.id, old.identifier)
        await self._cache_set(updated)
        logger.info("Updated and re-cached tenant id=%s", updated.id)
        return updated

    async def delete(self, tenant_id: str) -> None:
        """Delete from primary and invalidate cache keys.

        Args:
            tenant_id: ID of the tenant to delete.

        Raises:
            TenantNotFoundError: When *tenant_id* does not exist.
        """
        tenant = await self._primary.get_by_id(tenant_id)
        await self._primary.delete(tenant_id)
        await self._cache_invalidate(tenant.id, tenant.identifier)
        logger.info("Deleted and invalidated cache tenant id=%s", tenant_id)

    async def set_status(self, tenant_id: str, status: TenantStatus) -> Tenant:
        """Update status in primary and refresh cache.

        Args:
            tenant_id: ID of the tenant to update.
            status: New lifecycle status.

        Returns:
            Updated tenant.

        Raises:
            TenantNotFoundError: When *tenant_id* does not exist.
        """
        old = await self._primary.get_by_id(tenant_id)
        updated = await self._primary.set_status(tenant_id, status)
        await self._cache_invalidate(old.id, old.identifier)
        await self._cache_set(updated)
        return updated

    async def update_metadata(
        self,
        tenant_id: str,
        metadata: dict[str, Any],
    ) -> Tenant:
        """Merge metadata in primary and refresh cache.

        Args:
            tenant_id: ID of the tenant to update.
            metadata: Key-value pairs to shallow-merge.

        Returns:
            Updated tenant.

        Raises:
            TenantNotFoundError: When *tenant_id* does not exist.
        """
        old = await self._primary.get_by_id(tenant_id)
        updated = await self._primary.update_metadata(tenant_id, metadata)
        await self._cache_invalidate(old.id, old.identifier)
        await self._cache_set(updated)
        return updated

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    async def invalidate_all(self) -> int:
        """Delete every cache key owned by this store.

        Uses ``SCAN`` with a count hint rather than ``KEYS`` to avoid
        blocking the Redis event loop on large keyspaces.

        Returns:
            Number of Redis keys deleted.
        """
        pattern = f"{self._prefix}:*"
        keys: list[bytes] = []
        async for key in self._redis.scan_iter(match=pattern, count=100):
            keys.append(key)
        if keys:
            deleted: int = await self._redis.delete(*keys)
            logger.info("Invalidated %d cache entries (pattern=%s)", deleted, pattern)
            return deleted
        return 0

    async def cache_stats(self) -> dict[str, Any]:
        """Return lightweight cache statistics.

        Scans the keyspace via ``SCAN`` — does not block Redis.

        Returns:
            Dictionary with keys ``total_keys``, ``ttl_seconds``,
            and ``key_prefix``.
        """
        count = 0
        async for _ in self._redis.scan_iter(match=f"{self._prefix}:*", count=100):
            count += 1
        return {
            "total_keys": count,
            "ttl_seconds": self._ttl,
            "key_prefix": self._prefix,
        }

    async def close(self) -> None:
        """Close the Redis connection pool."""
        await self._redis.aclose()
        logger.info("RedisTenantStore closed")


__all__ = ["RedisTenantStore"]
