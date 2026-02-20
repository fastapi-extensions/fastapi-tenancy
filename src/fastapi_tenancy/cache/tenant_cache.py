"""Tenant-scoped Redis cache.

:class:`TenantCache` provides a strongly-isolated Redis key-value cache where
every key is namespaced under ``{prefix}:tenant:{tenant_id}:``.  This
guarantees that one tenant's cache entries can never collide with — or be
read by — another tenant, even when all tenants share the same Redis instance.

Key operations
--------------
* :meth:`get` / :meth:`set` / :meth:`delete` — standard cache CRUD.
* :meth:`exists` / :meth:`get_ttl` — inspection.
* :meth:`increment` — atomic counter increment.
* :meth:`clear_tenant` — purge all keys for a tenant (uses ``SCAN``).
* :meth:`get_keys` — list keys for a tenant (uses ``SCAN``).
* :meth:`stats` — lightweight usage statistics.

Security
--------
All ``KEYS`` operations use ``SCAN`` with a count hint to avoid blocking the
Redis event loop on large keyspaces.  :meth:`clear_tenant` and :meth:`get_keys`
are both non-blocking.

Installation
------------
Requires the ``redis`` extra::

    pip install fastapi-tenancy[redis]
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from redis import asyncio as aioredis
from redis.exceptions import RedisError

from fastapi_tenancy.core.exceptions import TenancyError

if TYPE_CHECKING:
    from fastapi_tenancy.core.types import Tenant

logger = logging.getLogger(__name__)


class _JSONEncoder(json.JSONEncoder):
    """JSON encoder that handles :class:`~datetime.datetime` and enum values."""

    def default(self, o: Any) -> Any:
        if isinstance(o, datetime):
            return o.isoformat()
        return super().default(o)


class TenantCache:
    """Tenant-scoped Redis cache with complete cross-tenant key isolation.

    All keys are namespaced as ``{prefix}:tenant:{tenant_id}:{key}`` so two
    tenants that happen to use the same *key* string never share values.

    Args:
        redis_url: Redis connection URL (e.g. ``redis://localhost:6379/0``).
        default_ttl: Default time-to-live in seconds.  Individual calls to
            :meth:`set` may override this with a per-key TTL.
        key_prefix: Namespace prefix applied to every key.  Use a distinct
            prefix per application to avoid key collisions in a shared Redis.
        max_connections: Maximum Redis connections in the pool.

    Example::

        cache = TenantCache(redis_url="redis://localhost:6379/0")

        await cache.set(tenant, "user_count", 100, ttl=300)
        count = await cache.get(tenant, "user_count")    # 100
        await cache.delete(tenant, "user_count")
        deleted = await cache.clear_tenant(tenant)       # int: keys removed
        await cache.close()
    """

    def __init__(
        self,
        redis_url: str,
        default_ttl: int = 3600,
        key_prefix: str = "tenancy",
        max_connections: int = 10,
    ) -> None:
        self._redis: aioredis.Redis = aioredis.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=max_connections,
        )
        self._default_ttl = default_ttl
        self._prefix = key_prefix
        logger.info(
            "TenantCache initialised ttl=%ds prefix=%s max_connections=%d",
            default_ttl,
            key_prefix,
            max_connections,
        )

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    def _key(self, tenant: Tenant, key: str) -> str:
        """Build the full namespaced Redis key."""
        return f"{self._prefix}:tenant:{tenant.id}:{key}"

    @staticmethod
    def _serialize(value: Any) -> str:
        """Serialise *value* to a JSON string, handling datetime and enums."""
        if isinstance(value, str):
            return value
        return json.dumps(value, cls=_JSONEncoder)

    @staticmethod
    def _deserialize(raw: str) -> Any:
        """Deserialise a JSON string, falling back to the raw string on error."""
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def get(self, tenant: Tenant, key: str) -> Any:
        """Return the cached value for *key*, or ``None`` on miss or error.

        Args:
            tenant: Owning tenant.
            key: Cache key (relative to the tenant namespace).

        Returns:
            Deserialised value, or ``None`` when absent.
        """
        full_key = self._key(tenant, key)
        try:
            raw = await self._redis.get(full_key)
            if raw is None:
                logger.debug("Cache miss key=%s", full_key)
                return None
            logger.debug("Cache hit key=%s", full_key)
            return self._deserialize(raw)
        except RedisError as exc:
            logger.error("Redis GET failed key=%s: %s", full_key, exc)
            return None

    async def set(
        self,
        tenant: Tenant,
        key: str,
        value: Any,
        ttl: int | None = None,
    ) -> bool:
        """Store *value* under *key* with a TTL.

        Args:
            tenant: Owning tenant.
            key: Cache key.
            value: Any JSON-serialisable value.
            ttl: Time-to-live in seconds.  Defaults to :attr:`_default_ttl`.

        Returns:
            ``True`` on success; ``False`` on serialisation or Redis error.
        """
        full_key = self._key(tenant, key)
        effective_ttl = ttl if ttl is not None else self._default_ttl
        try:
            serialised = self._serialize(value)
            result = await self._redis.setex(full_key, effective_ttl, serialised)
            logger.debug("Cached key=%s ttl=%ds", full_key, effective_ttl)
            return bool(result)
        except (RedisError, TypeError, ValueError) as exc:
            logger.error("Redis SET failed key=%s: %s", full_key, exc)
            return False

    async def delete(self, tenant: Tenant, key: str) -> bool:
        """Delete *key* from the cache.

        Args:
            tenant: Owning tenant.
            key: Cache key to delete.

        Returns:
            ``True`` when the key existed and was deleted; ``False`` otherwise.
        """
        full_key = self._key(tenant, key)
        try:
            result = await self._redis.delete(full_key)
            logger.debug("Deleted cache key=%s", full_key)
            return bool(result)
        except RedisError as exc:
            logger.error("Redis DELETE failed key=%s: %s", full_key, exc)
            return False

    async def exists(self, tenant: Tenant, key: str) -> bool:
        """Return ``True`` when *key* is present in the cache.

        Args:
            tenant: Owning tenant.
            key: Cache key.

        Returns:
            Existence flag.
        """
        full_key = self._key(tenant, key)
        try:
            return bool(await self._redis.exists(full_key))
        except RedisError as exc:
            logger.error("Redis EXISTS failed key=%s: %s", full_key, exc)
            return False

    async def get_ttl(self, tenant: Tenant, key: str) -> int:
        """Return remaining TTL for *key* in seconds.

        Returns:
            * Positive integer — remaining TTL.
            * ``-1`` — key exists but has no TTL.
            * ``-2`` — key does not exist.
        """
        full_key = self._key(tenant, key)
        try:
            return await self._redis.ttl(full_key)
        except RedisError as exc:
            logger.error("Redis TTL failed key=%s: %s", full_key, exc)
            return -2

    async def increment(self, tenant: Tenant, key: str, amount: int = 1) -> int:
        """Atomically increment a counter by *amount* and return the new value.

        Args:
            tenant: Owning tenant.
            key: Counter key.
            amount: Increment step (default 1).

        Returns:
            New counter value.

        Raises:
            TenancyError: When Redis raises an error.
        """
        full_key = self._key(tenant, key)
        try:
            value: int = await self._redis.incrby(full_key, amount)
            logger.debug("Incremented %s by %d → %d", full_key, amount, value)
            return value
        except RedisError as exc:
            logger.error("Redis INCRBY failed key=%s: %s", full_key, exc)
            raise TenancyError(f"Failed to increment cache key: {exc}") from exc

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    async def clear_tenant(self, tenant: Tenant) -> int:
        """Delete ALL cache entries for *tenant*.

        Uses ``SCAN`` with a count hint to avoid blocking Redis.

        Args:
            tenant: Tenant whose cache to purge.

        Returns:
            Number of Redis keys deleted.
        """
        pattern = f"{self._prefix}:tenant:{tenant.id}:*"
        keys: list[str] = []
        try:
            async for key in self._redis.scan_iter(match=pattern, count=100):
                keys.append(key)
            if keys:
                deleted: int = await self._redis.delete(*keys)
                logger.info("Cleared %d cache entries for tenant %s", deleted, tenant.id)
                return deleted
            return 0
        except RedisError as exc:
            logger.error("Redis clear_tenant failed %s: %s", tenant.id, exc)
            return 0

    async def get_keys(self, tenant: Tenant, pattern: str = "*") -> list[str]:
        """Return all cache keys for *tenant* matching *pattern*.

        The returned keys are relative to the tenant namespace (i.e. the
        ``{prefix}:tenant:{id}:`` prefix is stripped).

        Args:
            tenant: Owning tenant.
            pattern: Glob pattern (default: ``"*"`` — all keys).

        Returns:
            List of relative key strings.
        """
        full_pattern = f"{self._prefix}:tenant:{tenant.id}:{pattern}"
        prefix_len = len(f"{self._prefix}:tenant:{tenant.id}:")
        keys: list[str] = []
        try:
            async for key in self._redis.scan_iter(match=full_pattern, count=100):
                keys.append(str(key)[prefix_len:])
            return keys
        except RedisError as exc:
            logger.error("Redis get_keys failed %s: %s", tenant.id, exc)
            return []

    async def stats(self, tenant: Tenant) -> dict[str, Any]:
        """Return lightweight cache statistics for *tenant*.

        Estimates total memory usage by sampling the first 100 keys and
        extrapolating when there are more.

        Args:
            tenant: Owning tenant.

        Returns:
            Dictionary with keys ``tenant_id``, ``key_count``,
            ``memory_bytes``, ``memory_kb``, and ``memory_mb``.
        """
        pattern = f"{self._prefix}:tenant:{tenant.id}:*"
        keys: list[str] = []
        try:
            async for key in self._redis.scan_iter(match=pattern, count=100):
                keys.append(str(key))
            sample = keys[:100]
            total_memory = 0
            for key in sample:
                mem = await self._redis.memory_usage(key)
                if mem:
                    total_memory += mem
            if len(keys) > 100:
                total_memory = int(total_memory * (len(keys) / 100))
            return {
                "tenant_id": tenant.id,
                "key_count": len(keys),
                "memory_bytes": total_memory,
                "memory_kb": round(total_memory / 1024, 2),
                "memory_mb": round(total_memory / 1024 / 1024, 2),
            }
        except RedisError as exc:
            logger.error("Redis stats failed %s: %s", tenant.id, exc)
            return {"tenant_id": tenant.id, "key_count": 0, "memory_bytes": 0}

    async def close(self) -> None:
        """Close the Redis connection pool gracefully."""
        await self._redis.aclose()
        logger.info("TenantCache closed")


__all__ = ["TenantCache"]
