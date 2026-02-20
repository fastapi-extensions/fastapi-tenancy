"""In-memory tenant storage implementation for testing and development.

.. warning::
    This store holds all data in Python dictionaries.  All tenant records are
    **lost when the process exits**.  Use it only for unit tests, integration
    tests, local development, and demo environments.

    For production deployments use
    :class:`~fastapi_tenancy.storage.database.SQLAlchemyTenantStore` optionally
    wrapped in :class:`~fastapi_tenancy.storage.redis.RedisTenantStore`.

Design notes
------------
* **No async I/O**: all operations complete synchronously, wrapped in
  ``async def`` to satisfy the :class:`~fastapi_tenancy.storage.tenant_store.TenantStore`
  interface.  This keeps tests fast and eliminates the need for a database fixture.
* **O(1) lookups**: ``_tenants`` (id → Tenant) and ``_identifier_map``
  (identifier → id) are plain dicts — constant-time reads.
* **Sorted list**: :meth:`list` sorts in-memory by ``created_at`` descending
  to mirror the SQLAlchemy store's ``ORDER BY created_at DESC`` behaviour.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi_tenancy.core.exceptions import TenantNotFoundError
from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.storage.tenant_store import TenantStore

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


class InMemoryTenantStore(TenantStore):
    """In-memory tenant store for testing and local development.

    All state lives in two Python dictionaries:

    * ``_tenants`` — maps ``tenant_id → Tenant``
    * ``_identifier_map`` — maps ``identifier → tenant_id``

    Both indices are kept in sync by every mutating method so that
    :meth:`get_by_id` and :meth:`get_by_identifier` are always O(1).

    Example — pytest fixture::

        import pytest
        from fastapi_tenancy.storage.memory import InMemoryTenantStore

        @pytest.fixture
        async def tenant_store():
            store = InMemoryTenantStore()
            yield store
            store.clear()   # reset between tests

    Example — seeded store::

        store = InMemoryTenantStore()
        await store.create(Tenant(id="t1", identifier="acme", name="Acme"))
        await store.create(Tenant(id="t2", identifier="globex", name="Globex"))

        tenants = await store.list()
        assert len(tenants) == 2
    """

    def __init__(self) -> None:
        """Initialise an empty in-memory store."""
        self._tenants: dict[str, Tenant] = {}
        self._identifier_map: dict[str, str] = {}   # identifier → tenant_id
        logger.debug("InMemoryTenantStore initialised")

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def get_by_id(self, tenant_id: str) -> Tenant:
        """Return the tenant with *tenant_id* or raise :exc:`TenantNotFoundError`.

        Args:
            tenant_id: Opaque tenant primary key.

        Returns:
            Matching :class:`~fastapi_tenancy.core.types.Tenant`.

        Raises:
            TenantNotFoundError: When *tenant_id* is not in the store.
        """
        tenant = self._tenants.get(tenant_id)
        if tenant is None:
            raise TenantNotFoundError(identifier=tenant_id)
        return tenant

    async def get_by_identifier(self, identifier: str) -> Tenant:
        """Return the tenant with *identifier* or raise :exc:`TenantNotFoundError`.

        Args:
            identifier: Human-readable tenant slug.

        Returns:
            Matching :class:`~fastapi_tenancy.core.types.Tenant`.

        Raises:
            TenantNotFoundError: When no tenant with *identifier* exists.
        """
        tenant_id = self._identifier_map.get(identifier)
        if tenant_id is None:
            raise TenantNotFoundError(identifier=identifier)
        return self._tenants[tenant_id]

    async def list(
        self,
        skip: int = 0,
        limit: int = 100,
        status: TenantStatus | None = None,
    ) -> Iterable[Tenant]:
        """Return a page of tenants sorted by ``created_at`` descending.

        Args:
            skip: Number of records to skip.
            limit: Maximum number of records to return.
            status: Optional status filter.

        Returns:
            Filtered and paginated list of tenants.
        """
        tenants = list(self._tenants.values())
        if status is not None:
            tenants = [t for t in tenants if t.status == status]
        tenants.sort(key=lambda t: t.created_at, reverse=True)
        return tenants[skip : skip + limit]

    async def count(self, status: TenantStatus | None = None) -> int:
        """Return the number of stored tenants, optionally filtered by status.

        Args:
            status: Optional status filter.

        Returns:
            Non-negative integer count.
        """
        if status is None:
            return len(self._tenants)
        return sum(1 for t in self._tenants.values() if t.status == status)

    async def exists(self, tenant_id: str) -> bool:
        """Return ``True`` when a tenant with *tenant_id* exists.

        Args:
            tenant_id: Opaque tenant primary key.

        Returns:
            Existence flag.
        """
        return tenant_id in self._tenants

    async def get_by_ids(self, tenant_ids: Any) -> Iterable[Tenant]:
        """Return all tenants whose IDs appear in *tenant_ids*.

        IDs with no matching tenant are silently skipped.

        Args:
            tenant_ids: Iterable of opaque tenant IDs.

        Returns:
            Found tenants in the order their IDs appeared.
        """
        return [self._tenants[tid] for tid in tenant_ids if tid in self._tenants]

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def create(self, tenant: Tenant) -> Tenant:
        """Persist a new tenant in memory.

        Args:
            tenant: Fully-populated tenant object.

        Returns:
            The stored tenant (identical to the input; no server-side transforms).

        Raises:
            ValueError: When ``id`` or ``identifier`` already exists.
        """
        if tenant.id in self._tenants:
            raise ValueError(f"Tenant id={tenant.id!r} already exists.")
        if tenant.identifier in self._identifier_map:
            raise ValueError(f"Tenant identifier={tenant.identifier!r} already exists.")

        self._tenants[tenant.id] = tenant
        self._identifier_map[tenant.identifier] = tenant.id
        logger.debug("Created tenant id=%s identifier=%s", tenant.id, tenant.identifier)
        return tenant

    async def update(self, tenant: Tenant) -> Tenant:
        """Replace all mutable fields of an existing tenant.

        If the identifier changes, the old identifier key is removed from
        the index and the new one is added atomically.

        Args:
            tenant: Updated tenant object.  ``tenant.id`` must exist.

        Returns:
            Updated tenant with a refreshed ``updated_at`` timestamp.

        Raises:
            TenantNotFoundError: When ``tenant.id`` is not in the store.
        """
        old = self._tenants.get(tenant.id)
        if old is None:
            raise TenantNotFoundError(identifier=tenant.id)

        if old.identifier != tenant.identifier:
            del self._identifier_map[old.identifier]
            self._identifier_map[tenant.identifier] = tenant.id

        updated = tenant.model_copy(update={"updated_at": datetime.now(UTC)})
        self._tenants[tenant.id] = updated
        logger.debug("Updated tenant id=%s", tenant.id)
        return updated

    async def delete(self, tenant_id: str) -> None:
        """Remove a tenant from both internal indices.

        Args:
            tenant_id: ID of the tenant to delete.

        Raises:
            TenantNotFoundError: When *tenant_id* is not in the store.
        """
        tenant = self._tenants.get(tenant_id)
        if tenant is None:
            raise TenantNotFoundError(identifier=tenant_id)

        del self._identifier_map[tenant.identifier]
        del self._tenants[tenant_id]
        logger.debug("Deleted tenant id=%s", tenant_id)

    async def set_status(self, tenant_id: str, status: TenantStatus) -> Tenant:
        """Update the lifecycle status of a tenant.

        Args:
            tenant_id: ID of the tenant to update.
            status: New status value.

        Returns:
            Updated tenant with a refreshed ``updated_at`` timestamp.

        Raises:
            TenantNotFoundError: When *tenant_id* is not in the store.
        """
        tenant = await self.get_by_id(tenant_id)
        updated = tenant.model_copy(
            update={"status": status, "updated_at": datetime.now(UTC)}
        )
        self._tenants[tenant_id] = updated
        logger.debug("Set tenant %s status → %s", tenant_id, status.value)
        return updated

    async def update_metadata(
        self,
        tenant_id: str,
        metadata: dict[str, Any],
    ) -> Tenant:
        """Shallow-merge *metadata* into the tenant's metadata dictionary.

        Args:
            tenant_id: ID of the tenant to update.
            metadata: Key-value pairs to merge.

        Returns:
            Updated tenant.

        Raises:
            TenantNotFoundError: When *tenant_id* is not in the store.
        """
        tenant = await self.get_by_id(tenant_id)
        updated = tenant.model_copy(
            update={
                "metadata": {**tenant.metadata, **metadata},
                "updated_at": datetime.now(UTC),
            }
        )
        self._tenants[tenant_id] = updated
        logger.debug("Updated metadata for tenant id=%s", tenant_id)
        return updated

    async def bulk_update_status(
        self,
        tenant_ids: Any,
        status: TenantStatus,
    ) -> Iterable[Tenant]:
        """Update the status of multiple tenants in one pass.

        Uses a single shared timestamp so all ``updated_at`` values are
        identical across the batch.  IDs with no matching tenant are skipped.

        Args:
            tenant_ids: Iterable of opaque tenant IDs.
            status: New status applied to every matched tenant.

        Returns:
            Updated tenants in the order their IDs appeared.
        """
        timestamp = datetime.now(UTC)
        updated: list[Tenant] = []
        for tid in tenant_ids:
            tenant = self._tenants.get(tid)
            if tenant is not None:
                result = tenant.model_copy(
                    update={"status": status, "updated_at": timestamp}
                )
                self._tenants[tid] = result
                updated.append(result)
        logger.debug("Bulk updated %d tenants → %s", len(updated), status.value)
        return updated

    async def search(
        self,
        query: str,
        limit: int = 10,
        _scan_limit: int = 100,
    ) -> Iterable[Tenant]:
        """Search tenants by name or identifier substring.

        Results are ranked by relevance:

        1. Exact identifier match.
        2. Identifier starts with *query*.
        3. Name contains *query*.
        4. Identifier contains *query*.

        Args:
            query: Case-insensitive search string.
            limit: Maximum number of results to return.
            _scan_limit: Accepted for interface compatibility; all in-memory
                records are always scanned.

        Returns:
            Matching tenants sorted by relevance, up to *limit* results.
        """
        q = query.lower()
        matches = [
            t
            for t in self._tenants.values()
            if q in t.identifier.lower() or q in t.name.lower()
        ]
        matches.sort(
            key=lambda t: (
                t.identifier.lower() == q,
                t.identifier.lower().startswith(q),
                q in t.name.lower(),
            ),
            reverse=True,
        )
        return matches[:limit]

    # ------------------------------------------------------------------
    # Test / debug helpers
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Remove all tenants from both internal indices.

        Use in test teardown / fixture cleanup to reset state between cases::

            @pytest.fixture
            async def store():
                store = InMemoryTenantStore()
                yield store
                store.clear()
        """
        self._tenants.clear()
        self._identifier_map.clear()
        logger.debug("InMemoryTenantStore cleared")

    def get_all(self) -> dict[str, Tenant]:
        """Return a shallow-copy snapshot of all stored tenants keyed by ID.

        Returns:
            ``{tenant_id: Tenant}`` dict.  Mutating it does not affect
            the store.
        """
        return dict(self._tenants)

    def statistics(self) -> dict[str, Any]:
        """Return a summary of current store state for monitoring and debugging.

        Returns:
            Dictionary with keys:

            * ``total`` — total number of stored tenants.
            * ``by_status`` — count per status value.
            * ``identifier_index_size`` — should always equal ``total``.
        """
        by_status: dict[str, int] = {}
        for tenant in self._tenants.values():
            by_status[tenant.status.value] = by_status.get(tenant.status.value, 0) + 1

        return {
            "total": len(self._tenants),
            "by_status": by_status,
            "identifier_index_size": len(self._identifier_map),
        }


__all__ = ["InMemoryTenantStore"]
