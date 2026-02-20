"""Abstract tenant storage interface — the repository pattern.

:class:`TenantStore` defines the complete CRUD contract for tenant metadata
storage.  All concrete implementations (SQLAlchemy, in-memory, Redis cache)
implement this interface, giving the rest of the library a stable dependency
target regardless of the chosen backend.

Extending
---------
To add a custom backend, subclass :class:`TenantStore` and implement every
``@abstractmethod``.  Optional overrides (``get_by_ids``, ``search``,
``bulk_update_status``) have base implementations but are worth overriding
for production backends to avoid loading all tenants into memory::

    class MyStore(TenantStore):
        async def get_by_id(self, tenant_id: str) -> Tenant:
            ...
        # implement all other abstract methods
        ...
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from fastapi_tenancy.core.exceptions import TenantNotFoundError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from fastapi_tenancy.core.types import Tenant, TenantStatus


class TenantStore(ABC):
    """Abstract base class for tenant metadata storage backends.

    Implementations must be fully async and safe for concurrent use across
    multiple asyncio tasks.  Methods must raise
    :class:`~fastapi_tenancy.core.exceptions.TenantNotFoundError` (never
    return ``None``) when a required tenant cannot be found, and wrap
    unexpected storage errors in
    :class:`~fastapi_tenancy.core.exceptions.TenancyError`.

    Thread-safety contract
    ----------------------
    * Instances are created once during application startup and shared across
      all requests.  Implementations must be thread-safe and reentrant.
    * No instance state may be mutated after initialisation (beyond internal
      connection-pool bookkeeping managed by the database driver).
    """

    # ------------------------------------------------------------------
    # Required CRUD operations
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_by_id(self, tenant_id: str) -> Tenant:
        """Fetch a tenant by its opaque unique ID.

        Args:
            tenant_id: Opaque tenant primary key.

        Returns:
            The matching :class:`~fastapi_tenancy.core.types.Tenant`.

        Raises:
            TenantNotFoundError: When no tenant with *tenant_id* exists.
        """

    @abstractmethod
    async def get_by_identifier(self, identifier: str) -> Tenant:
        """Fetch a tenant by its human-readable slug identifier.

        Args:
            identifier: The tenant slug (e.g. ``"acme-corp"``).

        Returns:
            The matching :class:`~fastapi_tenancy.core.types.Tenant`.

        Raises:
            TenantNotFoundError: When no tenant with *identifier* exists.
        """

    @abstractmethod
    async def create(self, tenant: Tenant) -> Tenant:
        """Persist a new tenant record.

        Args:
            tenant: A fully-populated :class:`~fastapi_tenancy.core.types.Tenant`.
                The ``id`` and ``identifier`` must be unique.

        Returns:
            The stored tenant (may include server-generated timestamps).

        Raises:
            ValueError: When the ``id`` or ``identifier`` already exists.
            TenancyError: On unexpected storage failure.
        """

    @abstractmethod
    async def update(self, tenant: Tenant) -> Tenant:
        """Replace all mutable fields of an existing tenant.

        Because :class:`~fastapi_tenancy.core.types.Tenant` is immutable,
        callers must first build a modified copy with :meth:`model_copy`::

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

        Behaviour depends on :attr:`~fastapi_tenancy.core.config.TenancyConfig.enable_soft_delete`:
        implementations may mark the tenant as ``DELETED`` rather than
        removing the row, depending on application configuration.

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
    ) -> Iterable[Tenant]:
        """Return a page of tenants, ordered by creation date (newest first).

        Args:
            skip: Number of records to skip (offset-based pagination).
            limit: Maximum number of records to return.
            status: When provided, only tenants with this status are returned.

        Returns:
            An iterable (usually a :class:`list`) of
            :class:`~fastapi_tenancy.core.types.Tenant` objects.
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

        More efficient than :meth:`get_by_id` when only existence is needed.

        Args:
            tenant_id: Opaque tenant primary key.

        Returns:
            ``True`` when the tenant exists; ``False`` otherwise.
        """

    @abstractmethod
    async def set_status(self, tenant_id: str, status: TenantStatus) -> Tenant:
        """Change the lifecycle status of a tenant.

        This is the preferred method for status transitions (activate,
        suspend, etc.) because it only touches the status field, leaving
        all other fields unchanged.

        Args:
            tenant_id: ID of the tenant to update.
            status: The new :class:`~fastapi_tenancy.core.types.TenantStatus`.

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
    ) -> Tenant:
        """Merge *metadata* into the tenant's existing metadata dictionary.

        The merge is shallow: top-level keys in *metadata* overwrite
        existing keys; keys absent from *metadata* are preserved.

        Args:
            tenant_id: ID of the tenant to update.
            metadata: Key-value pairs to merge.

        Returns:
            The updated tenant.

        Raises:
            TenantNotFoundError: When *tenant_id* does not exist.
        """

    # ------------------------------------------------------------------
    # Optional batch operations (base implementations provided)
    # ------------------------------------------------------------------

    async def get_by_ids(self, tenant_ids: Iterable[str]) -> Iterable[Tenant]:
        """Fetch multiple tenants by their IDs in one call.

        The base implementation issues one :meth:`get_by_id` call per ID.
        **Override this for production backends** to issue a single query.

        Args:
            tenant_ids: Iterable of opaque tenant IDs.

        Returns:
            Tenants that were found; IDs with no matching tenant are silently
            skipped.
        """
        result: list[Tenant] = []
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
    ) -> Iterable[Tenant]:
        """Search for tenants whose name or identifier contains *query*.

        The base implementation loads up to *_scan_limit* records and filters
        them in Python.  **Override for production backends** with a
        database-level ``ILIKE``/``LIKE`` query.

        Args:
            query: Case-insensitive substring to search for.
            limit: Maximum number of results to return.
            _scan_limit: Maximum records fetched for in-memory filtering.

        Returns:
            Matching tenants, up to *limit* results.

        Warning:
            The base implementation is O(n) in the number of tenants.  For
            deployments with thousands of tenants, implement a database-level
            search in the concrete subclass.
        """
        query_lower = query.lower()
        candidates = await self.list(skip=0, limit=_scan_limit)
        matches = [
            t
            for t in candidates
            if query_lower in t.identifier.lower() or query_lower in t.name.lower()
        ]
        return matches[:limit]

    async def bulk_update_status(
        self,
        tenant_ids: Iterable[str],
        status: TenantStatus,
    ) -> Iterable[Tenant]:
        """Update the status of multiple tenants in one logical operation.

        The base implementation calls :meth:`set_status` once per ID.
        **Override for production backends** to issue a single SQL ``UPDATE``
        with an ``IN`` clause.

        Args:
            tenant_ids: IDs of the tenants to update.
            status: New :class:`~fastapi_tenancy.core.types.TenantStatus`
                applied to all matched tenants.

        Returns:
            Updated tenants; IDs with no matching tenant are silently skipped.
        """
        updated: list[Tenant] = []
        for tid in tenant_ids:
            try:
                updated.append(await self.set_status(tid, status))
            except TenantNotFoundError:
                continue
        return updated


__all__ = ["TenantStore"]
