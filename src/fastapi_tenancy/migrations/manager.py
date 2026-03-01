"""Alembic-based migration manager for per-tenant databases and schemas.

This module runs Alembic migrations for individual tenants or entire tenant
fleets using a bounded concurrency model (``asyncio.Semaphore``) to prevent
database connection exhaustion.

Why bounded concurrency matters
--------------------------------
Sequential migration of 1 000 tenants (the previous implementation) at 5 s
per migration ≈ 83 minutes.  With ``concurrency=20`` the same workload
completes in ~ 4 minutes (20x speedup) while keeping the total connection
count bounded.

Architecture
------------
::

    TenantMigrationManager
    ├── upgrade_tenant(tenant, revision)          # single tenant
    ├── upgrade_all(revision, concurrency=10)     # all tenants, bounded
    ├── downgrade_tenant(tenant, revision)        # single tenant
    └── get_current_revision(tenant)              # introspection

Alembic integration
-------------------
This manager requires an Alembic ``env.py`` that accepts a runtime
``url`` via ``context.configure(url=...)``::

    # alembic/env.py
    def run_migrations_online():
        url = context.get_x_argument(as_dictionary=True).get("url", FALLBACK_URL)
        with create_engine(url).connect() as conn:
            context.configure(connection=conn, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()

For ``SCHEMA`` isolation each tenant also receives a ``schema`` argument::

    # alembic/env.py (schema mode)
    schema = context.get_x_argument(as_dictionary=True).get("schema", "public")
    context.configure(
        connection=conn,
        target_metadata=target_metadata,
        version_table_schema=schema,
        include_schemas=True,
    )
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi_tenancy.core.exceptions import MigrationError
from fastapi_tenancy.core.types import IsolationStrategy

# Optional alembic import — available when the [migrations] extra is installed.
try:
    from alembic import command
    from alembic.config import Config as AlembicConfig
    _ALEMBIC_AVAILABLE = True
except ImportError:  # pragma: no cover
    command = None  # type: ignore[assignment]
    AlembicConfig = None  # type: ignore
    _ALEMBIC_AVAILABLE = False

if TYPE_CHECKING:
    from fastapi_tenancy.core.config import TenancyConfig
    from fastapi_tenancy.core.types import Tenant
    from fastapi_tenancy.storage.tenant_store import TenantStore

logger = logging.getLogger(__name__)


class TenantMigrationManager:
    """Alembic migration manager for multi-tenant databases.

    Supports ``SCHEMA`` and ``DATABASE`` isolation strategies.

    Args:
        config: Tenancy configuration.
        store: Tenant store used by ``upgrade_all`` / ``downgrade_all``.
        alembic_cfg_path: Path to ``alembic.ini`` (default: ``"alembic.ini"``).
        executor: Optional ``concurrent.futures.Executor`` for running
            synchronous Alembic migrations.  Defaults to ``None``, which
            uses the event loop's default ``ThreadPoolExecutor``.

            With ``concurrency=20`` workers, the default executor (size =
            ``min(32, os.cpu_count() + 4)``) may be exhausted when the CPU
            count is low.  Supply a larger executor to avoid starvation::

                from concurrent.futures import ThreadPoolExecutor
                migrator = TenantMigrationManager(
                    config, store,
                    executor=ThreadPoolExecutor(max_workers=30),
                )

    Example::

        migrator = TenantMigrationManager(config, store)

        # Migrate a single tenant
        await migrator.upgrade_tenant(tenant, revision="head")

        # Migrate all tenants with 20 concurrent workers
        results = await migrator.upgrade_all(revision="head", concurrency=20)
        failed = [r for r in results if not r["success"]]
    """

    def __init__(
        self,
        config: TenancyConfig,
        store: TenantStore[Any],
        alembic_cfg_path: str | Path = "alembic.ini",
        executor: Any | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._alembic_cfg_path = Path(alembic_cfg_path)
        # Optional custom executor for thread-pool control (see docstring).
        self._executor: Any = executor

        if not self._alembic_cfg_path.exists():
            logger.warning(
                "alembic.ini not found at %s — migration calls will fail.",
                self._alembic_cfg_path.resolve(),
            )

    ############################
    # Single-tenant operations #
    ############################

    async def upgrade_tenant(
        self,
        tenant: Tenant,
        revision: str = "head",
    ) -> None:
        """Run Alembic ``upgrade`` for *tenant*.

        Executes the migration in a thread pool executor to avoid blocking
        the asyncio event loop with synchronous Alembic I/O.

        Args:
            tenant: Target tenant.
            revision: Alembic revision target (default: ``"head"``).

        Raises:
            MigrationError: When the migration fails.
        """
        logger.info("Upgrading tenant %s to revision %r", tenant.id, revision)
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                self._executor,
                self._run_migration_sync,
                tenant,
                "upgrade",
                revision,
            )
        except MigrationError:
            raise
        except Exception as exc:
            raise MigrationError(
                tenant_id=tenant.id,
                operation="upgrade",
                reason=str(exc),
            ) from exc
        logger.info("Tenant %s upgraded to %r successfully", tenant.id, revision)

    async def downgrade_tenant(
        self,
        tenant: Tenant,
        revision: str = "-1",
    ) -> None:
        """Run Alembic ``downgrade`` for *tenant*.

        Args:
            tenant: Target tenant.
            revision: Alembic revision to downgrade to (default: ``"-1"``
                meaning one step back).

        Raises:
            MigrationError: When the migration fails.
        """
        logger.warning(
            "Downgrading tenant %s to revision %r", tenant.id, revision
        )
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                self._executor,
                self._run_migration_sync,
                tenant,
                "downgrade",
                revision,
            )
        except MigrationError:
            raise
        except Exception as exc:
            raise MigrationError(
                tenant_id=tenant.id,
                operation="downgrade",
                reason=str(exc),
            ) from exc

    async def get_current_revision(self, tenant: Tenant) -> str | None:
        """Return the current Alembic revision for *tenant*.

        Args:
            tenant: Target tenant.

        Returns:
            The current revision string (e.g. ``"abc123"``), or ``None``
            when no migrations have been applied.
        """
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self._executor,
                self._get_current_revision_sync,
                tenant,
            )
        except Exception as exc:
            logger.warning("Could not read revision for tenant %s: %s", tenant.id, exc)
            return None

    ##########################################
    # Fleet operations (bounded concurrency) #
    ##########################################

    async def upgrade_all(
        self,
        revision: str = "head",
        concurrency: int = 10,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Upgrade all active tenants to *revision* with bounded concurrency.

        Loads tenants in pages from the store to avoid loading the entire
        fleet into memory at once.  Uses ``asyncio.Semaphore(concurrency)``
        to cap parallel database connections.

        Args:
            revision: Alembic revision target.
            concurrency: Maximum concurrent migration workers.
            page_size: Number of tenants to fetch per page from the store.

        Returns:
            List of result dicts, one per tenant::

                [
                    {"tenant_id": "...", "success": True,  "revision": "head"},
                    {"tenant_id": "...", "success": False, "error": "..."},
                ]

        Example::

            results = await migrator.upgrade_all(revision="head", concurrency=20)
            failed = [r for r in results if not r["success"]]
            print(f"{len(failed)} of {len(results)} tenants failed migration")
        """
        from fastapi_tenancy.core.types import TenantStatus  # noqa: PLC0415

        semaphore = asyncio.Semaphore(concurrency)
        results: list[dict[str, Any]] = []
        skip = 0

        while True:
            page = await self._store.list(
                skip=skip, limit=page_size, status=TenantStatus.ACTIVE
            )
            if not page:
                break
            skip += len(page)

            tasks = [
                self._migrate_one(tenant, "upgrade", revision, semaphore)
                for tenant in page
            ]
            page_results = await asyncio.gather(*tasks, return_exceptions=False)
            results.extend(page_results)

            if len(page) < page_size:
                break  # Last page reached.

        success_count = sum(1 for r in results if r["success"])
        logger.info(
            "upgrade_all complete: %d/%d tenants succeeded (revision=%r)",
            success_count,
            len(results),
            revision,
        )
        return results

    async def downgrade_all(
        self,
        revision: str = "-1",
        concurrency: int = 10,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Downgrade all active tenants with bounded concurrency.

        Args:
            revision: Alembic revision target.
            concurrency: Maximum concurrent workers.
            page_size: Page size for store pagination.

        Returns:
            List of result dicts (same format as :meth:`upgrade_all`).
        """
        from fastapi_tenancy.core.types import TenantStatus  # noqa: PLC0415

        semaphore = asyncio.Semaphore(concurrency)
        results: list[dict[str, Any]] = []
        skip = 0

        while True:
            page = await self._store.list(
                skip=skip, limit=page_size, status=TenantStatus.ACTIVE
            )
            if not page:
                break
            skip += len(page)

            tasks = [
                self._migrate_one(tenant, "downgrade", revision, semaphore)
                for tenant in page
            ]
            page_results = await asyncio.gather(*tasks, return_exceptions=False)
            results.extend(page_results)

            if len(page) < page_size:
                break

        return results

    ####################
    # Internal helpers #
    ####################

    async def _migrate_one(
        self,
        tenant: Tenant,
        operation: str,
        revision: str,
        semaphore: asyncio.Semaphore,
    ) -> dict[str, Any]:
        """Run one migration for *tenant* within the semaphore.

        Args:
            tenant: Target tenant.
            operation: ``"upgrade"`` or ``"downgrade"``.
            revision: Alembic revision target.
            semaphore: Bounded concurrency lock.

        Returns:
            Result dictionary with ``tenant_id``, ``success``, and either
            ``revision`` or ``error``.
        """
        async with semaphore:
            try:
                if operation == "upgrade":
                    await self.upgrade_tenant(tenant, revision)
                else:
                    await self.downgrade_tenant(tenant, revision)
            except MigrationError as exc:
                logger.exception(
                    "Migration failed for tenant %s: %s", tenant.id, exc.reason
                )
                return {
                    "tenant_id": tenant.id,
                    "identifier": tenant.identifier,
                    "success": False,
                    "error": exc.reason,
                }
            else:
                return {
                    "tenant_id": tenant.id,
                    "identifier": tenant.identifier,
                    "success": True,
                    "revision": revision,
                }

    def _build_alembic_args(self, tenant: Tenant) -> dict[str, str]:
        """Build the ``-x`` arguments for the Alembic CLI / API.

        Strategy routing:
            - ``DATABASE``: passes the per-tenant database URL.
            - ``SCHEMA``: passes the shared database URL and the tenant schema.
            - ``RLS``: passes only the shared database URL (all tenants share
              tables — the migration runs once against the shared schema).
            - ``HYBRID``: resolves the effective strategy for this tenant
              (premium or standard) and applies the same rules as above.
            - Unknown strategies: passes no extra args (Alembic uses its own
              ``alembic.ini`` defaults).

        Args:
            tenant: Target tenant.

        Returns:
            Dictionary of ``-x key=value`` arguments.
        """
        strategy = self._config.get_isolation_strategy_for_tenant(tenant.id)
        args: dict[str, str] = {}

        if strategy == IsolationStrategy.DATABASE:
            url = (
                tenant.database_url
                or self._config.get_database_url_for_tenant(tenant.id)
            )
            args["url"] = url

        elif strategy == IsolationStrategy.SCHEMA:
            args["url"] = str(self._config.database_url)
            schema = tenant.schema_name or self._config.get_schema_name(tenant.identifier)
            args["schema"] = schema

        elif strategy == IsolationStrategy.RLS:
            # RLS uses shared tables — migrations run against the shared DB.
            # No per-tenant schema argument is needed; env.py uses the default
            # schema from alembic.ini.
            args["url"] = str(self._config.database_url)

        elif strategy == IsolationStrategy.HYBRID:
            # HYBRID resolves to a concrete strategy per tenant via
            # get_isolation_strategy_for_tenant().  If the resolved strategy
            # is still HYBRID (which should never happen — the config validator
            # prevents it), log a warning and fall through with no extra args.
            logger.warning(
                "HYBRID strategy resolved for tenant %s in _build_alembic_args — "
                "this should not happen.  Falling back to alembic.ini defaults.",
                tenant.id,
            )

        # For IsolationStrategy.HYBRID the effective non-hybrid strategy is
        # already resolved by get_isolation_strategy_for_tenant(), so the HYBRID
        # branch above is a safety net only.  Unknown strategies produce no
        # extra args and let alembic.ini take over.

        return args

    def _run_migration_sync(
        self,
        tenant: Tenant,
        operation: str,
        revision: str,
    ) -> None:
        """Execute a synchronous Alembic migration (called in thread pool).

        Thread-safety: A **new** ``AlembicConfig`` object is constructed on
        every call.  This is intentional and must not be changed to a shared
        instance.  ``upgrade_all`` submits multiple calls to the default
        ``ThreadPoolExecutor`` concurrently; sharing a single config object
        would result in races on ``cfg.attributes`` mutations.  Keeping
        construction per-call eliminates the need for any locking here.

        Args:
            tenant: Target tenant.
            operation: ``"upgrade"`` or ``"downgrade"``.
            revision: Alembic revision.

        Raises:
            MigrationError: On migration failure.
            ImportError: When Alembic is not installed.
        """
        if not _ALEMBIC_AVAILABLE:
            raise ImportError(
                "Alembic is required for migration support. "
                "Install it with: pip install 'fastapi-tenancy[migrations]'"
            )

        # A fresh AlembicConfig per call — see thread-safety note in the
        # docstring above.  Do not refactor to a shared/cached instance.
        cfg = AlembicConfig(str(self._alembic_cfg_path))

        # Pass tenant-specific connection info via Alembic's x-argument mechanism.
        # The documented way to pass runtime data to env.py is via
        # cfg.attributes, which env.py reads with:
        #   context.config.attributes.get("x_args", {})  # noqa: ERA001
        # or the equivalent context.get_x_argument(as_dictionary=True) after
        # the args are merged into the right structure.
        #
        # IMPORTANT: set_section_option("alembic", "cmd_opts.url", ...) writes
        # to the alembic.ini section and does NOT populate x-args.  It was
        # previously used here but silently failed to pass the url/schema values
        # to env.py in all Alembic versions.  The correct approach is:
        x_args = self._build_alembic_args(tenant)

        # Expose as a flat dict on cfg.attributes so env.py can read them with:
        #   cfg.attributes.get("url"), cfg.attributes.get("schema")  # noqa: ERA001
        # AND via the x_args sub-dict for env.py implementations that use
        #   context.get_x_argument(as_dictionary=True):
        cfg.attributes.update(x_args)
        cfg.attributes["x_args"] = x_args

        try:
            if operation == "upgrade":
                command.upgrade(cfg, revision)
            elif operation == "downgrade":
                command.downgrade(cfg, revision)
            else:
                msg = f"Unknown migration operation: {operation!r}"
                raise ValueError(msg)  # noqa: TRY301
        except Exception as exc:
            raise MigrationError(
                tenant_id=tenant.id,
                operation=operation,
                reason=str(exc),
            ) from exc

    def _get_current_revision_sync(self, tenant: Tenant) -> str | None:
        """Read the current Alembic revision synchronously (thread pool).

        Args:
            tenant: Target tenant.

        Returns:
            Current revision string, or ``None``.
        """
        if not _ALEMBIC_AVAILABLE:
            return None

        cfg = AlembicConfig(str(self._alembic_cfg_path))
        x_args = self._build_alembic_args(tenant)
        cfg.attributes.update(x_args)
        cfg.attributes["x_args"] = x_args

        import io  # noqa: PLC0415

        output = io.StringIO()
        cfg.stdout = output
        try:
            command.current(cfg)
            return output.getvalue().strip() or None
        except Exception:
            return None


__all__ = ["TenantMigrationManager"]
