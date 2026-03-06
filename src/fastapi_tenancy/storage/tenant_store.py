"""Abstract tenant storage interface — the repository pattern.

``TenantStore`` defines the complete CRUD contract for tenant metadata storage.
All concrete implementations (SQLAlchemy, in-memory, Redis cache wrapper)
implement this interface, giving the rest of the library a stable dependency
target regardless of the chosen backend.

Generic design
--------------
``TenantStore`` is generic over ``TenantT`` (bound to ``Tenant``).  This
enables typed subclasses that store an application-specific ``Tenant``
subclass while still satisfying the base interface::

    class AppTenant(Tenant):
        subscription_tier: str

    class AppTenantStore(TenantStore[AppTenant]):
        async def get_by_id(self, tenant_id: str) -> AppTenant: ...

Extending
---------
Subclass ``TenantStore[Tenant]`` (or your own domain model) and implement
every ``@abstractmethod``.  The batch operations (``get_by_ids``, ``search``,
``bulk_update_status``) have base implementations but are worth overriding
for production backends to avoid N+1 queries::

    class MyStore(TenantStore[Tenant]):
        async def get_by_id(self, tenant_id: str) -> Tenant: ...
        # ... implement all other abstract methods
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import logging
from typing import TYPE_CHECKING, Any, Generic

from fastapi_tenancy.core.exceptions import TenantNotFoundError
from fastapi_tenancy.core.types import Tenant, TenantT

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from fastapi_tenancy.core.types import TenantStatus

logger = logging.getLogger(__name__)


class TenantStore(ABC, Generic[TenantT]):
    """Abstract base class for tenant metadata storage backends.

    Implementations must be:

    - **Fully async** — every method is a coroutine.
    - **Concurrency-safe** — instances are created once at startup and
      shared across all requests; no mutable instance state after init.
    - **Raise on not-found** — methods that look up a single tenant must
      raise ``TenantNotFoundError``, never return ``None``.
    - **Wrap unexpected errors** — unexpected storage errors should be wrapped
      in ``TenancyError`` with a ``details`` dict safe to log.

    Type parameter:
        TenantT: The concrete tenant domain model (defaults to ``Tenant``).
    """

    ############################
    # Required CRUD operations #
    ############################

    @abstractmethod
    async def get_by_id(self, tenant_id: str) -> TenantT:
        """Fetch a tenant by its opaque unique ID.

        Args:
            tenant_id: Opaque tenant primary key.

        Returns:
            The matching ``TenantT``.

        Raises:
            TenantNotFoundError: When no tenant with *tenant_id* exists.
        """

    @abstractmethod
    async def get_by_identifier(self, identifier: str) -> TenantT:
        """Fetch a tenant by its human-readable slug identifier.

        Args:
            identifier: The tenant slug (e.g. ``"acme-corp"``).

        Returns:
            The matching ``TenantT``.

        Raises:
            TenantNotFoundError: When no tenant with *identifier* exists.
        """

    @abstractmethod
    async def create(self, tenant: TenantT) -> TenantT:
        """Persist a new tenant record.

        Args:
            tenant: A fully-populated ``TenantT`` instance.  The ``id`` and
                ``identifier`` must be unique.

        Returns:
            The stored tenant (may include server-generated timestamps).

        Raises:
            ValueError: When the ``id`` or ``identifier`` already exists.
            TenancyError: On unexpected storage failure.
        """

    @abstractmethod
    async def update(self, tenant: TenantT) -> TenantT:
        """Replace all mutable fields of an existing tenant.

        Because ``Tenant`` is immutable, callers must first build a modified
        copy with ``model_copy``::

            updated = tenant.model_copy(update={"name": "New Name"})
            result  = await store.update(updated)

        Args:
            tenant: Modified tenant object.  ``tenant.id`` must already exist.

        Returns:
            The updated tenant as stored (timestamps refreshed).

        Raises:
            TenantNotFoundError: When ``tenant.id`` does not exist.
            TenancyError: On unexpected storage failure.
        """

    @abstractmethod
    async def delete(self, tenant_id: str) -> None:
        """Remove a tenant from the store.

        Behaviour depends on ``TenancyConfig.enable_soft_delete``:
        implementations should mark the tenant as ``DELETED`` rather than
        removing the row when soft-delete is enabled.

        Args:
            tenant_id: ID of the tenant to delete.

        Raises:
            TenantNotFoundError: When *tenant_id* does not exist.
            TenancyError: On unexpected storage failure.
        """

    @abstractmethod
    async def list(
        self,
        skip: int = 0,
        limit: int = 100,
        status: TenantStatus | None = None,
    ) -> Sequence[TenantT]:
        """Return a page of tenants, ordered by creation date (newest first).

        Args:
            skip: Number of records to skip (offset-based pagination).
            limit: Maximum number of records to return.
            status: When provided, only tenants with this status are returned.

        Returns:
            A sequence of ``TenantT`` objects.
        """

    @abstractmethod
    async def count(self, status: TenantStatus | None = None) -> int:
        """Return the total number of tenants, optionally filtered by status.

        Args:
            status: When provided, count only tenants with this status.

        Returns:
            Non-negative integer count.
        """

    @abstractmethod
    async def exists(self, tenant_id: str) -> bool:
        """Return ``True`` if a tenant with *tenant_id* exists.

        More efficient than ``get_by_id`` when only existence is needed.

        Args:
            tenant_id: Opaque tenant primary key.

        Returns:
            ``True`` when the tenant exists; ``False`` otherwise.
        """

    @abstractmethod
    async def set_status(self, tenant_id: str, status: TenantStatus) -> TenantT:
        """Change the lifecycle status of a tenant.

        This is the preferred method for status transitions (activate, suspend,
        etc.) because it only touches the status field.

        Args:
            tenant_id: ID of the tenant to update.
            status: The new ``TenantStatus``.

        Returns:
            The updated tenant.

        Raises:
            TenantNotFoundError: When *tenant_id* does not exist.
        """

    @abstractmethod
    async def update_metadata(
        self,
        tenant_id: str,
        metadata: dict[str, Any],
    ) -> TenantT:
        """Atomically merge *metadata* into the tenant's metadata dictionary.

        The merge is shallow: top-level keys in *metadata* overwrite existing
        keys; keys absent from *metadata* are preserved.

        Implementations should perform this as an atomic operation at the
        database level (e.g. PostgreSQL ``jsonb ||`` operator) to avoid the
        read-modify-write race that a naïve in-application merge introduces.

        Args:
            tenant_id: ID of the tenant to update.
            metadata: Key-value pairs to merge.

        Returns:
            The updated tenant.

        Raises:
            TenantNotFoundError: When *tenant_id* does not exist.
        """

    ####################################################################
    # Optional batch operations (base implementations use N+1 queries) #
    ####################################################################

    async def close(self) -> None:
        """Release any resources held by this store (connections, pools, etc.).

        The base implementation is a no-op — subclasses that hold external
        resources (database engines, Redis connections) **must** override this
        method and dispose them properly.

        Called automatically by ``TenancyManager.close()`` on application
        shutdown.  If your store holds connection pools, failing to override
        this will leak file descriptors and connections on process exit.

        Example override::

            async def close(self) -> None:
                await self._engine.dispose()
        """

    async def get_by_ids(self, tenant_ids: Iterable[str]) -> Sequence[TenantT]:
        """Fetch multiple tenants by their IDs in one logical call.

        The base implementation issues one ``get_by_id`` call per ID.
        **Override this for production backends** to issue a single query.

        Args:
            tenant_ids: Iterable of opaque tenant IDs.

        Returns:
            Tenants that were found; IDs with no matching tenant are silently
            skipped.
        """
        result: list[TenantT] = []
        for tid in tenant_ids:
            try:
                result.append(await self.get_by_id(tid))
            except TenantNotFoundError:
                continue
        return result

    async def search(
        self,
        query: str,
        limit: int = 10,
        _scan_limit: int = 100,
    ) -> Sequence[TenantT]:
        """Search for tenants whose name or identifier contains *query*.

        The base implementation loads up to *_scan_limit* records and filters
        them in Python.  **Override for production backends** with a
        database-level ``ILIKE``/``LIKE`` query.

        **Result ordering is backend-defined.**  Concrete implementations may
        apply relevance ranking (e.g. ``InMemoryTenantStore`` sorts by exact
        match → prefix match → substring match), or may return results in
        arbitrary order (e.g. the ``SQLAlchemyTenantStore`` override sorts
        alphabetically by identifier).  Do not rely on a specific ordering
        unless the concrete implementation documents it.

        Args:
            query: Case-insensitive substring to search for.
            limit: Maximum number of results to return.
            _scan_limit: Maximum records fetched for in-memory filtering.

        Returns:
            Matching tenants, up to *limit* results.

        Warning:
            The base implementation is O(n) in the number of tenants.  For
            deployments with thousands of tenants, override with a DB-level
            search in the concrete subclass.
        """
        query_lower = query.lower()
        candidates = await self.list(skip=0, limit=_scan_limit)
        # Warn only when both the scan limit AND the result limit are saturated,
        # which is the scenario where results are most likely to be silently
        # truncated.  A store with exactly _scan_limit tenants where all match
        # would hit this; a store with _scan_limit tenants where none match
        # would not — avoiding a spurious warning in the latter case.
        matches: list[TenantT] = [
            t
            for t in candidates
            if query_lower in t.identifier.lower() or query_lower in t.name.lower()
        ]
        if len(candidates) == _scan_limit and len(matches) >= limit:
            logger.warning(
                "%s.search() reached both the scan limit (%d) and the result limit (%d). "
                "Results may be incomplete.  Override search() in your concrete store with "
                "a database-level query (e.g. ILIKE) to avoid missing results for large "
                "tenant fleets.",
                type(self).__name__,
                _scan_limit,
                limit,
            )
        return matches[:limit]

    async def bulk_update_status(
        self,
        tenant_ids: Iterable[str],
        status: TenantStatus,
    ) -> Sequence[TenantT]:
        """Update the status of multiple tenants in one logical operation.

        The base implementation calls ``set_status`` once per ID.
        **Override for production backends** to issue a single SQL ``UPDATE``
        with an ``IN`` clause.

        Args:
            tenant_ids: IDs of the tenants to update.
            status: New ``TenantStatus`` applied to all matched tenants.

        Returns:
            Updated tenants; IDs with no matching tenant are silently skipped.
        """
        updated: list[TenantT] = []
        for tid in tenant_ids:
            try:
                updated.append(await self.set_status(tid, status))
            except TenantNotFoundError:
                continue
        return updated


#########################################################
# Type alias for the most common concrete instantiation #
#########################################################

#: ``TenantStore`` parametrised with the library's built-in ``Tenant`` model.
DefaultTenantStore = TenantStore[Tenant]

__all__ = ["DefaultTenantStore", "TenantStore"]
