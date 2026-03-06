---
title: Installation
description: Installing fastapi-tenancy with the right extras for your database and optional services.
---

# Installation

## Requirements

- Python **3.11**, **3.12**, or **3.13**
- FastAPI **0.111+**
- An async database driver (see the table below)

## Install the package

=== "Core only"

    Installs the library with no async driver. Useful if you're providing your
    own driver or using a custom transport layer.

    ```bash
    pip install fastapi-tenancy
    ```

=== "PostgreSQL"

    Installs [`asyncpg`](https://github.com/MagicStack/asyncpg) — the
    recommended driver for PostgreSQL.

    ```bash
    pip install "fastapi-tenancy[postgres]"
    ```

=== "MySQL / MariaDB"

    Installs [`aiomysql`](https://github.com/aio-libs/aiomysql).

    ```bash
    pip install "fastapi-tenancy[mysql]"
    ```

=== "SQLite"

    Installs [`aiosqlite`](https://github.com/omnilib/aiosqlite). Useful for
    development and testing.

    ```bash
    pip install "fastapi-tenancy[sqlite]"
    ```

=== "MSSQL"

    Installs [`aioodbc`](https://github.com/aio-libs/aioodbc).

    ```bash
    pip install "fastapi-tenancy[mssql]"
    ```

=== "Full stack"

    Installs PostgreSQL driver + Redis + JWT + Alembic migrations. The
    recommended setup for production.

    ```bash
    pip install "fastapi-tenancy[full]"
    ```

## Optional extras

Install any combination of extras alongside the driver:

```bash
pip install "fastapi-tenancy[postgres,redis,jwt,migrations]"
```

| Extra | Package installed | When you need it |
|-------|------------------|-----------------|
| `postgres` | `asyncpg>=0.29.0` | PostgreSQL async driver |
| `mysql` | `aiomysql>=0.2.0` | MySQL / MariaDB async driver |
| `sqlite` | `aiosqlite>=0.19.0` | SQLite async driver |
| `mssql` | `aioodbc>=0.5.0` | Microsoft SQL Server |
| `redis` | `redis[hiredis]>=5.0.0` | Tenant cache + rate limiting |
| `jwt` | `python-jose[cryptography]>=3.3.0` | JWT-based tenant resolution |
| `migrations` | `alembic>=1.13.0` | Per-tenant Alembic migrations |
| `full` | postgres + redis + jwt + migrations | Everything for production |

## Database URL format

fastapi-tenancy requires an **async** SQLAlchemy connection URL. The table
below shows the correct URL scheme for each database:

| Database | URL prefix | Example |
|----------|-----------|---------|
| PostgreSQL | `postgresql+asyncpg://` | `postgresql+asyncpg://user:pass@localhost/mydb` |
| MySQL | `mysql+aiomysql://` | `mysql+aiomysql://user:pass@localhost/mydb` |
| SQLite | `sqlite+aiosqlite:///` | `sqlite+aiosqlite:///./dev.db` |
| MSSQL | `mssql+aioodbc://` | `mssql+aioodbc://user:pass@host/mydb?driver=ODBC+Driver+17` |

!!! warning "Synchronous drivers are not supported"
    Using a synchronous driver (e.g. `postgresql://` instead of
    `postgresql+asyncpg://`) will trigger a `UserWarning` at startup and
    cause runtime errors in async contexts. Always use the async variants
    shown above.

## Verify the installation

```python
import fastapi_tenancy
print(fastapi_tenancy.__version__)  # 0.2.0
```

## Docker Compose for local development

A `docker-compose.test.yml` is included in the repository for spinning up
PostgreSQL 16, MySQL 8, and Redis 7 locally:

```bash
docker compose -f docker-compose.test.yml up -d
```

This starts:

- PostgreSQL 16 on port **5432**
- MySQL 8.0 on port **3306**
- Redis 7 on port **6379**

with the following test credentials:

| Service | Credentials |
|---------|-------------|
| PostgreSQL | `postgres:postgres`, database `test_tenancy` |
| MySQL | `root:root`, database `test_tenancy` |
| Redis | no auth |

## Next steps

→ [Quick Start](quickstart.md) — build a working multi-tenant app in 5 minutes
