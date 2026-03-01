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
``{prefix}:id:{tenant_id}``
    Primary-key lookup.  Populated on every read miss and on write.

``{prefix}:identifier:{identifier}``
    Slug lookup.  Same blob; same TTL.

Both keys are invalidated together in a single ``DEL`` call whenever the
tenant is mutated.

Serialisation
-------------
:meth:`Tenant.model_dump_json` / :meth:`Tenant.model_validate_json` are used
instead of :func:`json.dumps` / :func:`json.loads` so that
:class:`~datetime.datetime` objects, :class:`~enum.StrEnum` values, and other
Pydantic-managed types round-trip without loss.

Cache invalidation
------------------
Bulk invalidation uses ``SCAN`` with a count hint rather than ``KEYS`` to
avoid blocking the Redis event loop on large keyspaces.

Optimisation for ``update()``
------------------------------
The original implementation fetched the old tenant from the primary store on
every ``update()`` and ``set_status()`` call to learn the old identifier (for
cache invalidation).  This implementation tries the cache first (O(1)) and
only falls back to the primary store on a cache miss.

Installation
------------
Requires the ``redis`` extra::

    pip install fastapi-tenancy[redis]
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.storage.tenant_store import TenantStore

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

logger = logging.getLogger(__name__)


def _require_redis() -> Any:
    """Import ``redis.asyncio`` and raise ``ImportError`` with an actionable message on miss."""
    try:
        from redis import asyncio as aioredis  # noqa: PLC0415

    except ImportError as exc:
        raise ImportError(
            "RedisTenantStore requires the 'redis' extra:\n"
            "    pip install fastapi-tenancy[redis]"
        ) from exc
    else:
        return aioredis

class RedisTenantStore(TenantStore[Tenant]):
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
        await cache.close()
    """

    def __init__(
        self,
        redis_url: str,
        primary_store: TenantStore[Tenant],
        ttl: int = 3600,
        key_prefix: str = "tenant",
    ) -> None:
        aioredis = _require_redis()
        self._primary = primary_store
        self._ttl = ttl
        self._prefix = key_prefix
        self._redis: Any = aioredis.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=False,
        )
        logger.info(
            "RedisTenantStore initialised ttl=%ds prefix=%s", ttl, key_prefix
        )

    ############################################################################
    # Lifecycle — delegate to primary so callers only need to manage one store #
    ############################################################################

    async def initialize(self) -> None:
        """Initialise the primary store if it supports it.

        Delegates to the primary store's ``initialize()`` so callers do not
        need to initialise both stores separately, satisfying LSP: a
        ``RedisTenantStore`` can be used anywhere a ``TenantStore`` is expected
        without additional lifecycle management by the caller.
        """
        if hasattr(self._primary, "initialize"):
            await self._primary.initialize()
            logger.info("RedisTenantStore: delegated initialize() to primary store")

    async def close(self) -> None:
        """Close the Redis connection pool and the primary store.

        Both resources are closed so callers only need to call ``close()``
        on the outermost store.
        """
        await self._redis.aclose()
        if hasattr(self._primary, "close"):
            await self._primary.close()
            logger.info("RedisTenantStore: delegated close() to primary store")
        logger.info("RedisTenantStore closed")

    ####################
    # Internal helpers #
    ####################

    def _id_key(self, tenant_id: str) -> str:
        """Build the Redis key for a primary-key lookup."""
        return f"{self._prefix}:id:{tenant_id}"

    def _slug_key(self, identifier: str) -> str:
        """Build the Redis key for a slug lookup."""
        return f"{self._prefix}:identifier:{identifier}"

    def _serialize(self, tenant: Tenant) -> bytes:
        """Serialise *tenant* to bytes via Pydantic's own JSON encoder."""
        return tenant.model_dump_json().encode("utf-8")

    def _deserialize(self, data: bytes) -> Tenant:
        """Deserialise *data* using Pydantic's type-safe JSON parser."""
        return Tenant.model_validate_json(data.decode("utf-8"))

    async def _cache_set(self, tenant: Tenant) -> None:
        """Write both cache keys atomically using a Redis pipeline.

        Both the ``id`` and ``identifier`` keys store the same serialised blob
        so either lookup path can be served from cache.

        Args:
            tenant: The tenant to cache.
        """
        serialised = self._serialize(tenant)
        try:
            pipe = self._redis.pipeline()
            pipe.setex(self._id_key(tenant.id), self._ttl, serialised)
            pipe.setex(self._slug_key(tenant.identifier), self._ttl, serialised)
            await pipe.execute()
            logger.debug("Cached tenant id=%s ttl=%ds", tenant.id, self._ttl)
        except Exception as exc:
            # Log but do not re-raise: a cache write failure is not fatal.
            # The next read will be a cache miss and will fall back to the
            # primary store, keeping the system functional with degraded
            # cache performance rather than a hard failure.
            logger.warning(
                "Cache write failed for tenant id=%s: %s — operating without cache",
                tenant.id,
                exc,
            )

    async def _cache_invalidate(self, tenant_id: str, identifier: str) -> None:
        """Delete both cache keys for a tenant in a single ``DEL`` command.

        Args:
            tenant_id: The tenant's opaque ID.
            identifier: The tenant's slug.
        """
        await self._redis.delete(
            self._id_key(tenant_id),
            self._slug_key(identifier),
        )
        logger.debug("Invalidated cache tenant id=%s", tenant_id)

    async def _get_old_tenant(self, tenant_id: str) -> Tenant | None:
        """Fetch the current cached tenant for *tenant_id*, or None on miss.

        Used before mutations to learn the old slug for cache invalidation,
        avoiding an extra round-trip to the primary store on cache-warm paths.

        Args:
            tenant_id: The tenant's opaque ID.

        Returns:
            The cached tenant or ``None`` on a cache miss.
        """
        cached = await self._redis.get(self._id_key(tenant_id))
        if cached is not None:
            try:
                return self._deserialize(cached)
            except Exception:  # noqa: S110
                # Corrupt cache entry — treat as miss.
                pass
        return None

    ###################
    # Read operations #
    ###################

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
            try:
                return self._deserialize(cached)
            except Exception:
                # Corrupt cache entry — treat as miss and refetch from primary.
                logger.warning("Corrupt cache entry for id=%s, treating as miss", tenant_id)
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
    ) -> Sequence[Tenant]:
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

    ####################
    # Write operations #
    ####################

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

        Cache-first optimisation: tries to read the old identifier from the
        cache before falling back to the primary store, avoiding an extra
        database round-trip on warm cache paths.

        Args:
            tenant: Updated tenant object.

        Returns:
            Updated tenant.

        Raises:
            TenantNotFoundError: When ``tenant.id`` does not exist.
        """
        # Try cache first to avoid extra primary store round-trip.
        old = await self._get_old_tenant(tenant.id)
        if old is None:
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
        old = await self._get_old_tenant(tenant_id)
        if old is None:
            old = await self._primary.get_by_id(tenant_id)

        await self._primary.delete(tenant_id)
        await self._cache_invalidate(old.id, old.identifier)
        logger.info("Deleted and invalidated cache tenant id=%s", tenant_id)

    async def set_status(self, tenant_id: str, status: TenantStatus) -> Tenant:
        """Update status in primary and refresh cache.

        Cache-first optimisation for reading the old identifier.

        Args:
            tenant_id: ID of the tenant to update.
            status: New lifecycle status.

        Returns:
            Updated tenant.

        Raises:
            TenantNotFoundError: When *tenant_id* does not exist.
        """
        old = await self._get_old_tenant(tenant_id)
        if old is None:
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
        old = await self._get_old_tenant(tenant_id)
        if old is None:
            old = await self._primary.get_by_id(tenant_id)

        updated = await self._primary.update_metadata(tenant_id, metadata)
        await self._cache_invalidate(old.id, old.identifier)
        await self._cache_set(updated)
        return updated

    ###################
    # Batch overrides #
    ###################

    async def get_by_ids(self, tenant_ids: Iterable[str]) -> Sequence[Tenant]:
        """Fetch multiple tenants — cache hits served first; misses delegated.

        Uses a Redis pipeline to batch all cache lookups into one round-trip.
        The returned list preserves the **same order as** *tenant_ids*: IDs
        that are not found in the cache or primary store are silently omitted.

        Args:
            tenant_ids: Iterable of opaque tenant IDs.

        Returns:
            Found tenants in the same order as *tenant_ids*.
        """
        ids = list(tenant_ids)
        if not ids:
            return []

        # Batch cache lookup via pipeline.
        pipe = self._redis.pipeline()
        for tid in ids:
            pipe.get(self._id_key(tid))
        raw_results = await pipe.execute()

        # Build a map of id → Tenant for hits; collect miss ids for DB fetch.
        tenant_map: dict[str, Tenant] = {}
        miss_ids: list[str] = []

        for tid, raw in zip(ids, raw_results, strict=False):
            if raw is not None:
                try:
                    tenant_map[tid] = self._deserialize(raw)
                    continue
                except Exception:  # noqa: S110
                    pass  # Treat corrupt entry as a miss.
            miss_ids.append(tid)

        if miss_ids:
            fetched = await self._primary.get_by_ids(miss_ids)
            for tenant in fetched:
                await self._cache_set(tenant)
                tenant_map[tenant.id] = tenant

        # Return in original input order, skipping IDs not found anywhere.
        return [tenant_map[tid] for tid in ids if tid in tenant_map]

    async def bulk_update_status(
        self,
        tenant_ids: Iterable[str],
        status: TenantStatus,
    ) -> Sequence[Tenant]:
        """Update status for multiple tenants and invalidate their cache entries.

        Args:
            tenant_ids: IDs of tenants to update.
            status: New status applied to every matched tenant.

        Returns:
            Updated tenants.
        """
        ids = list(tenant_ids)
        if not ids:
            return []
        updated = await self._primary.bulk_update_status(ids, status)
        for tenant in updated:
            await self._cache_set(tenant)
        return updated

    ####################
    # Cache management #
    ####################

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
            Dictionary with ``total_keys``, ``ttl_seconds``, and ``key_prefix``.
        """
        count = 0
        async for _ in self._redis.scan_iter(match=f"{self._prefix}:*", count=100):
            count += 1
        return {
            "total_keys": count,
            "ttl_seconds": self._ttl,
            "key_prefix": self._prefix,
        }

__all__ = ["RedisTenantStore"]
