"""Multi-tenant Alembic migration manager.

Runs Alembic schema migrations for individual tenants or all tenants in a
batch, supporting all four isolation strategies:

+-------------------+----------------------------------------------------------+
| Strategy          | Mechanism                                                |
+===================+==========================================================+
| SCHEMA            | Sets ``schema_name`` Alembic option — migrations run in  |
|                   | the tenant's dedicated PostgreSQL schema.                |
+-------------------+----------------------------------------------------------+
| DATABASE          | Sets ``sqlalchemy.url`` — migrations run against the     |
|                   | tenant-specific database.                                |
+-------------------+----------------------------------------------------------+
| RLS               | Migrates the shared schema once — no per-tenant change.  |
+-------------------+----------------------------------------------------------+
| HYBRID            | Delegates to the appropriate strategy per tenant.        |
+-------------------+----------------------------------------------------------+

Asyncio safety
--------------
Alembic's command API is **synchronous** blocking I/O.  Calling it directly
from an ``async`` function would block the event loop and freeze all other
concurrent requests.

Every Alembic call in this module is wrapped in
``asyncio.get_running_loop().run_in_executor(None, ...)`` which offloads the
blocking call to the default ``ThreadPoolExecutor`` without blocking the loop.

Installation
------------
Requires the ``migrations`` extra::

    pip install fastapi-tenancy[migrations]
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from fastapi_tenancy.core.exceptions import MigrationError
from fastapi_tenancy.core.types import IsolationStrategy, Tenant

if TYPE_CHECKING:
    from fastapi_tenancy.isolation.base import BaseIsolationProvider

logger = logging.getLogger(__name__)


async def _run_in_executor(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Execute a blocking synchronous callable in the default thread-pool.

    This is the canonical pattern for calling blocking I/O from async code.

    Args:
        func: Synchronous callable to execute.
        *args: Positional arguments forwarded to *func*.
        **kwargs: Keyword arguments forwarded to *func*.

    Returns:
        The return value of *func*.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


class MigrationManager:
    """Run Alembic migrations for all tenants in an isolation-aware manner.

    Args:
        alembic_ini_path: Path to the ``alembic.ini`` file.
        isolation_provider: The active isolation provider — used to determine
            how to scope each migration and to open tenant-scoped database
            sessions when reading the ``alembic_version`` table.

    Raises:
        FileNotFoundError: When *alembic_ini_path* does not exist.

    Example::

        manager = MigrationManager(
            alembic_ini_path="alembic.ini",
            isolation_provider=schema_provider,
        )

        # Migrate all tenants
        results = await manager.upgrade_all_tenants(tenants)
        print(results["success"], "succeeded")

        # Migrate one tenant
        await manager.upgrade_tenant(tenant)

        # Check migration status
        status = await manager.get_migration_status(tenant)
        print(status["current_revision"], "→", status["latest_revision"])
    """

    def __init__(
        self,
        alembic_ini_path: str | Path,
        isolation_provider: BaseIsolationProvider,
    ) -> None:
        self.alembic_ini_path = Path(alembic_ini_path)
        self.isolation_provider = isolation_provider

        if not self.alembic_ini_path.exists():
            raise FileNotFoundError(
                f"Alembic config not found: {self.alembic_ini_path}"
            )
        logger.info("MigrationManager initialised config=%s", self.alembic_ini_path)

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _alembic_config(self, tenant: Tenant | None = None) -> Config:
        """Build an Alembic :class:`~alembic.config.Config` scoped to *tenant*.

        When *tenant* is ``None``, returns the base config (used for
        generating new migration scripts).

        Args:
            tenant: Target tenant, or ``None`` for the base config.

        Returns:
            A configured :class:`~alembic.config.Config` instance.
        """
        cfg = Config(str(self.alembic_ini_path))
        if tenant is None:
            return cfg

        strategy = getattr(self.isolation_provider.config, "isolation_strategy", None)

        if strategy == IsolationStrategy.SCHEMA:
            schema_name = self.isolation_provider.get_schema_name(tenant)
            cfg.set_main_option("schema_name", schema_name)

        elif strategy == IsolationStrategy.DATABASE:
            db_url = self.isolation_provider.get_database_url(tenant)
            cfg.set_main_option("sqlalchemy.url", db_url)

        return cfg

    # ------------------------------------------------------------------
    # Single-tenant operations
    # ------------------------------------------------------------------

    async def upgrade_tenant(
        self,
        tenant: Tenant,
        revision: str = "head",
    ) -> None:
        """Migrate a single *tenant* to *revision*.

        Args:
            tenant: Target tenant.
            revision: Alembic revision string (default: ``"head"``).

        Raises:
            MigrationError: When Alembic raises any exception.
        """
        logger.info(
            "Upgrading tenant %s (id=%s) to revision=%r",
            tenant.identifier,
            tenant.id,
            revision,
        )
        cfg = self._alembic_config(tenant)
        try:
            # CRITICAL: Alembic is synchronous blocking I/O.
            # run_in_executor offloads it to the thread pool without
            # blocking the asyncio event loop.
            await _run_in_executor(command.upgrade, cfg, revision)
            logger.info("Tenant %s upgraded to %r", tenant.identifier, revision)
        except Exception as exc:
            logger.error(
                "Migration failed tenant=%s: %s", tenant.identifier, exc, exc_info=True
            )
            raise MigrationError(
                tenant_id=tenant.id,
                operation="upgrade",
                reason=str(exc),
                details={"revision": revision},
            ) from exc

    async def downgrade_tenant(self, tenant: Tenant, revision: str) -> None:
        """Roll back *tenant* to *revision*.

        .. warning::
            Data loss may occur depending on the migration content.
            Use with caution and always create a backup first.

        Args:
            tenant: Target tenant.
            revision: Alembic revision to downgrade to.

        Raises:
            MigrationError: When Alembic raises any exception.
        """
        logger.warning(
            "Downgrading tenant %s to revision=%r", tenant.identifier, revision
        )
        cfg = self._alembic_config(tenant)
        try:
            await _run_in_executor(command.downgrade, cfg, revision)
            logger.info("Tenant %s downgraded to %r", tenant.identifier, revision)
        except Exception as exc:
            raise MigrationError(
                tenant_id=tenant.id,
                operation="downgrade",
                reason=str(exc),
                details={"revision": revision},
            ) from exc

    # ------------------------------------------------------------------
    # Batch operations
    # ------------------------------------------------------------------

    async def upgrade_all_tenants(
        self,
        tenants: list[Tenant],
        revision: str = "head",
        continue_on_error: bool = True,
    ) -> dict[str, Any]:
        """Migrate all *tenants* to *revision*.

        Args:
            tenants: List of tenants to migrate.
            revision: Target Alembic revision (default: ``"head"``).
            continue_on_error: When ``True`` (default), log and skip a failing
                tenant and continue to the next.  When ``False``, abort the
                batch on the first failure.

        Returns:
            Summary dictionary::

                {
                    "success": <int>,
                    "failed": <int>,
                    "total": <int>,
                    "errors": [
                        {
                            "tenant_id": ...,
                            "identifier": ...,
                            "error": ...,
                            "operation": ...,
                        },
                        ...
                    ],
                }
        """
        results: dict[str, Any] = {
            "success": 0,
            "failed": 0,
            "total": len(tenants),
            "errors": [],
        }
        logger.info(
            "Starting migration of %d tenants to revision=%r", len(tenants), revision
        )

        for idx, tenant in enumerate(tenants, start=1):
            try:
                logger.info("Migrating %d/%d: %s", idx, len(tenants), tenant.identifier)
                await self.upgrade_tenant(tenant, revision)
                results["success"] += 1
            except MigrationError as exc:
                results["failed"] += 1
                results["errors"].append(
                    {
                        "tenant_id": tenant.id,
                        "identifier": tenant.identifier,
                        "error": str(exc),
                        "operation": exc.operation,
                    }
                )
                logger.error("Migration failed %s: %s", tenant.identifier, exc)
                if not continue_on_error:
                    logger.error("Aborting migration batch on first error")
                    break

        logger.info(
            "Migration complete: %d succeeded %d failed / %d total",
            results["success"],
            results["failed"],
            results["total"],
        )
        return results

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def get_migration_status(self, tenant: Tenant) -> dict[str, Any]:
        """Return the current and latest Alembic revision for *tenant*.

        The current revision is read directly from the ``alembic_version``
        table inside a tenant-scoped database session — not inferred from
        file system state.

        Args:
            tenant: Target tenant.

        Returns:
            Dictionary with keys:

            * ``tenant_id`` — opaque tenant ID.
            * ``tenant_identifier`` — human-readable slug.
            * ``current_revision`` — version installed in the database.
            * ``latest_revision`` — head revision from the scripts directory.
            * ``is_up_to_date`` — whether ``current == latest``.

            On error, contains an additional ``error`` key.
        """
        try:
            cfg = self._alembic_config(tenant)
            script = ScriptDirectory.from_config(cfg)
            latest: str | None = script.get_current_head()

            from sqlalchemy import text

            current: str | None = None
            async with self.isolation_provider.get_session(tenant) as session:
                result = await session.execute(
                    text("SELECT version_num FROM alembic_version LIMIT 1")
                )
                row = result.scalar_one_or_none()
                current = str(row) if row is not None else None

            return {
                "tenant_id": tenant.id,
                "tenant_identifier": tenant.identifier,
                "current_revision": current,
                "latest_revision": latest,
                "is_up_to_date": current == latest,
            }
        except Exception as exc:
            logger.error(
                "get_migration_status failed tenant=%s: %s",
                tenant.identifier,
                exc,
                exc_info=True,
            )
            return {
                "tenant_id": tenant.id,
                "tenant_identifier": tenant.identifier,
                "error": str(exc),
            }

    async def create_revision(
        self,
        message: str,
        autogenerate: bool = True,
    ) -> str:
        """Generate a new Alembic migration script.

        Args:
            message: Human-readable description for the revision.
            autogenerate: When ``True``, Alembic inspects the current database
                state and generates ``op.add_column`` / ``op.drop_table``
                statements automatically.

        Returns:
            The created revision ID (e.g. ``"a1b2c3d4e5f6"``).

        Raises:
            MigrationError: When Alembic fails to create the revision.
        """
        logger.info("Creating migration: %r autogenerate=%s", message, autogenerate)
        cfg = self._alembic_config()
        try:
            script = await _run_in_executor(
                command.revision, cfg, message=message, autogenerate=autogenerate
            )
            revision_id: str = script.revision if script is not None else "unknown"
            logger.info("Created revision %s: %r", revision_id, message)
            return revision_id
        except Exception as exc:
            logger.error("Failed to create revision: %s", exc, exc_info=True)
            raise MigrationError(
                tenant_id="all",
                operation="create_revision",
                reason=str(exc),
                details={"message": message},
            ) from exc


__all__ = ["MigrationManager"]
