"""In-process LRU+TTL tenant cache with optional Redis persistence.

This module provides :class:`TenantCache` — a fast in-process LRU dictionary
with time-to-live (TTL) expiry that sits in front of the Redis / DB stack.

Cache hierarchy (fastest → slowest)
------------------------------------
::

    TenantCache (in-process, μs)
        ↓ miss
    RedisTenantStore (network, ~1 ms)
        ↓ miss
    SQLAlchemyTenantStore (database, ~5-20 ms)

Use case
--------
``TenantCache`` is most valuable for high-traffic applications where the same
few thousand tenants generate the majority of requests.  It avoids even the
Redis round-trip for hot tenants.

TTL strategy
------------
Entries expire after ``ttl`` seconds regardless of access pattern.  On expiry
the next access is a cache miss and the entry is refreshed from the backing
store.  The TTL is intentionally kept short (default: 60 s) to limit the
window where a stale tenant record is served after an update.

LRU eviction
------------
When ``max_size`` entries are cached and a new tenant is inserted, the
least-recently-used entry is evicted.  The eviction order is maintained by
an ``OrderedDict`` — O(1) move_to_end and popitem.

Thread / task safety
--------------------
``asyncio`` tasks run on a single OS thread within one event loop, so the
``OrderedDict`` needs no locking.  If you use multiple event loops (e.g.
``anyio`` in trio backend), wrap cache mutation operations in an
``asyncio.Lock`` or replace with an actor-model cache.
"""

from __future__ import annotations

from collections import OrderedDict
import logging
import time
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from fastapi_tenancy.core.types import Tenant

logger = logging.getLogger(__name__)


class _Entry(NamedTuple):
    """A single cache entry.

    Attributes:
        tenant: The cached ``Tenant`` object.
        expires_at: Unix timestamp after which this entry is stale.
    """

    tenant: Tenant
    expires_at: float


class TenantCache:
    """In-process LRU cache with per-entry TTL for tenant objects.

    Args:
        max_size: Maximum number of tenant entries to hold (LRU eviction
            when full).  Default: 1000.
        ttl: Seconds before an entry is considered stale.  Default: 60.

    Example::

        cache = TenantCache(max_size=500, ttl=30)

        cache.set(tenant)
        hit = cache.get(tenant.id)           # by ID
        hit = cache.get_by_identifier("acme-corp")  # by slug

        cache.invalidate(tenant.id)
        cache.clear()
    """

    def __init__(self, max_size: int = 1000, ttl: int = 60) -> None:
        if max_size < 1:
            msg = "max_size must be >= 1"
            raise ValueError(msg)
        if ttl < 1:
            msg = "ttl must be >= 1 second"
            raise ValueError(msg)

        self._max_size = max_size
        self._ttl = ttl
        # Both dicts maintain insertion order; move_to_end keeps MRU at back.
        self._by_id: OrderedDict[str, _Entry] = OrderedDict()
        self._id_by_ident: dict[str, str] = {}  # identifier → tenant_id

        # Telemetry counters — monotonically increasing, never reset.
        self._hits: int = 0
        self._misses: int = 0

        logger.debug(
            "TenantCache initialised max_size=%d ttl=%ds", max_size, ttl
        )

    ########
    # Read #
    ########

    def get(self, tenant_id: str) -> Tenant | None:
        """Return the cached tenant for *tenant_id*, or ``None`` on miss/expiry.

        On a cache hit, the entry is promoted to the MRU position.

        Args:
            tenant_id: Opaque tenant primary key.

        Returns:
            Cached ``Tenant``, or ``None`` on miss or expiry.
        """
        entry = self._by_id.get(tenant_id)
        if entry is None:
            self._misses += 1
            return None
        if time.monotonic() > entry.expires_at:
            self._evict(tenant_id)
            self._misses += 1
            return None
        self._by_id.move_to_end(tenant_id)
        self._hits += 1
        return entry.tenant

    def get_by_identifier(self, identifier: str) -> Tenant | None:
        """Return the cached tenant for *identifier*, or ``None`` on miss/expiry.

        Args:
            identifier: Human-readable tenant slug.

        Returns:
            Cached ``Tenant``, or ``None`` on miss or expiry.
        """
        tenant_id = self._id_by_ident.get(identifier)
        if tenant_id is None:
            return None
        return self.get(tenant_id)

    #########
    # Write #
    #########

    def set(self, tenant: Tenant) -> None:
        """Insert or refresh *tenant* in the cache.

        When the cache is at capacity, the LRU entry is evicted before
        inserting the new entry.

        Args:
            tenant: The tenant to cache.  Both ID and identifier keys are
                written atomically.
        """
        expires_at = time.monotonic() + self._ttl

        # Remove existing entry under the same ID (may have a different
        # identifier if it was renamed).
        if tenant.id in self._by_id:
            old_entry = self._by_id[tenant.id]
            old_ident = old_entry.tenant.identifier
            if old_ident != tenant.identifier:
                self._id_by_ident.pop(old_ident, None)

        # Evict LRU when full and this is a new entry.
        if len(self._by_id) >= self._max_size and tenant.id not in self._by_id:
            self._evict_lru()

        self._by_id[tenant.id] = _Entry(tenant=tenant, expires_at=expires_at)
        self._by_id.move_to_end(tenant.id)  # mark as MRU
        self._id_by_ident[tenant.identifier] = tenant.id

    ################
    # Invalidation #
    ################

    def invalidate(self, tenant_id: str) -> bool:
        """Remove the cache entry for *tenant_id*.

        Args:
            tenant_id: Opaque tenant primary key.

        Returns:
            ``True`` when an entry was found and removed; ``False`` on miss.
        """
        return self._evict(tenant_id)

    def invalidate_by_identifier(self, identifier: str) -> bool:
        """Remove the cache entry for *identifier*.

        Args:
            identifier: Human-readable tenant slug.

        Returns:
            ``True`` when an entry was removed; ``False`` on miss.
        """
        tenant_id = self._id_by_ident.get(identifier)
        if tenant_id is None:
            return False
        return self._evict(tenant_id)

    def clear(self) -> int:
        """Evict all entries and reset both indices.

        Returns:
            Number of entries evicted.
        """
        count = len(self._by_id)
        self._by_id.clear()
        self._id_by_ident.clear()
        logger.debug("TenantCache cleared (%d entries evicted)", count)
        return count

    ###########################
    # Metrics / introspection #
    ###########################

    def size(self) -> int:
        """Return the number of currently cached entries."""
        return len(self._by_id)

    def stats(self) -> dict[str, int]:
        """Return a snapshot of cache state for monitoring.

        Returns:
            Dictionary with:
                - ``size``: current number of entries.
                - ``max_size``: configured maximum.
                - ``ttl``: configured TTL in seconds.
                - ``hits``: cumulative cache hit count since creation.
                - ``misses``: cumulative cache miss count since creation.
                - ``hit_rate_pct``: integer hit-rate percentage (0-100),
                  or 0 when no lookups have occurred yet.
        """
        total = self._hits + self._misses
        hit_rate = int(self._hits * 100 / total) if total > 0 else 0
        return {
            "size": len(self._by_id),
            "max_size": self._max_size,
            "ttl": self._ttl,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate_pct": hit_rate,
        }

    def purge_expired(self) -> int:
        """Eagerly remove all expired entries.

        Not required — entries are lazily evicted on access — but useful
        to call periodically in low-traffic applications to reclaim memory.

        Returns:
            Number of entries evicted.
        """
        now = time.monotonic()
        expired_keys = [
            k for k, entry in self._by_id.items() if now > entry.expires_at
        ]
        for key in expired_keys:
            self._evict(key)
        return len(expired_keys)

    ###################
    # Private helpers #
    ###################

    def _evict(self, tenant_id: str) -> bool:
        """Remove the entry for *tenant_id* from both indices.

        Args:
            tenant_id: ID of the entry to remove.

        Returns:
            ``True`` when an entry was removed.
        """
        entry = self._by_id.pop(tenant_id, None)
        if entry is None:
            return False
        self._id_by_ident.pop(entry.tenant.identifier, None)
        return True

    def _evict_lru(self) -> None:
        """Evict the least-recently-used (oldest) entry."""
        if self._by_id:
            # OrderedDict.popitem(last=False) removes the oldest entry.
            _, entry = self._by_id.popitem(last=False)
            self._id_by_ident.pop(entry.tenant.identifier, None)
            logger.debug("LRU evicted tenant id=%s", entry.tenant.id)


__all__ = ["TenantCache"]
