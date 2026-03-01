---
title: Migrations
description: Running Alembic migrations across all tenants with TenantMigrationManager.
---

# Migrations

`TenantMigrationManager` runs Alembic migrations for individual tenants or entire tenant fleets using a bounded concurrency model to prevent database connection exhaustion.

## Installation

```bash
pip install "fastapi-tenancy[migrations]"
```

## Setup

### 1. Configure Alembic

Create a standard `alembic.ini` and `alembic/env.py`. The migration manager passes the target database URL and schema as `-x` arguments — your `env.py` must read them:

=== "Schema isolation"

    ```python title="alembic/env.py"
    from alembic import context

    def run_migrations_online():
        x_args = context.get_x_argument(as_dictionary=True)
        url    = x_args.get("url", config.get_main_option("sqlalchemy.url"))
        schema = x_args.get("schema", "public")

        connectable = create_async_engine(url)
        async with connectable.connect() as conn:
            await conn.execute(text(f'SET search_path TO "{schema}", public'))
            await conn.run_sync(do_run_migrations, schema=schema)
    ```

=== "Database isolation"

    ```python title="alembic/env.py"
    def run_migrations_online():
        x_args = context.get_x_argument(as_dictionary=True)
        url    = x_args.get("url", config.get_main_option("sqlalchemy.url"))

        connectable = create_async_engine(url)
        async with connectable.connect() as conn:
            await conn.run_sync(do_run_migrations)
    ```

=== "RLS isolation"

    ```python title="alembic/env.py"
    # For RLS, all tenants share the same schema
    # Run migrations once against the shared database
    def run_migrations_online():
        url = config.get_main_option("sqlalchemy.url")
        connectable = create_async_engine(url)
        async with connectable.connect() as conn:
            await conn.run_sync(do_run_migrations)
    ```

### 2. Create the manager

```python
from fastapi_tenancy.migrations.manager import TenantMigrationManager

migrator = TenantMigrationManager(
    config=config,
    store=store,
    alembic_cfg_path="alembic.ini",  # default
)
```

## Migrating a single tenant

```python
tenant = await store.get_by_identifier("acme-corp")
await migrator.upgrade_tenant(tenant, revision="head")
```

## Migrating all tenants

```python
results = await migrator.upgrade_all(
    revision="head",
    concurrency=20,   # default: 10 — run 20 tenants simultaneously
)

# Check for failures
failed = [r for r in results if not r["success"]]
if failed:
    print(f"{len(failed)} tenants failed:")
    for r in failed:
        print(f"  {r['identifier']} ({r['tenant_id']}): {r['error']}")
```

### Concurrency and performance

| Tenants | Concurrency | Estimated time (5s/tenant) |
|---------|-------------|---------------------------|
| 100 | 10 | ~50 s |
| 1 000 | 20 | ~4 min |
| 10 000 | 50 | ~17 min |

Set `concurrency` to a value that keeps the total connection count within your database's `max_connections`. A safe formula: `concurrency ≤ max_connections × 0.5`.

## Downgrading

```python
# Downgrade a single tenant
await migrator.downgrade_tenant(tenant, revision="-1")

# Downgrade all tenants
results = await migrator.downgrade_all(revision="-1")
```

## Inspecting current revision

```python
revision = await migrator.get_current_revision(tenant)
print(f"{tenant.identifier}: {revision}")  # "abc123def456" or None (never migrated)
```

## CLI integration

```python title="migrate.py"
import asyncio
import sys
from main import config, store
from fastapi_tenancy.migrations.manager import TenantMigrationManager

async def main():
    migrator = TenantMigrationManager(config, store)
    command  = sys.argv[1]  # "upgrade" | "downgrade"
    revision = sys.argv[2] if len(sys.argv) > 2 else "head"

    if command == "upgrade":
        results = await migrator.upgrade_all(revision=revision, concurrency=20)
    elif command == "downgrade":
        results = await migrator.downgrade_all(revision=revision, concurrency=20)

    failed = [r for r in results if not r["success"]]
    print(f"Done. {len(results) - len(failed)}/{len(results)} succeeded.")
    if failed:
        for r in failed:
            print(f"  FAILED {r['identifier']} ({r['tenant_id']}): {r['error']}")
        sys.exit(1)

asyncio.run(main())
```

```bash
python migrate.py upgrade head
python migrate.py downgrade -1
```
