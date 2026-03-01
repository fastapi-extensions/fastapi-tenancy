---
title: Development Setup
description: Setting up a fastapi-tenancy development environment.
---

# Development Setup

## Prerequisites

- Python 3.11, 3.12, or 3.13
- Git
- Docker Desktop (for integration tests)
- GNU Make (optional — all commands work without it)

## Clone and install

```bash
git clone https://github.com/fastapi-extensions/fastapi-tenancy.git
cd fastapi-tenancy

# Install all extras in editable mode
pip install -e ".[dev]"
# or
make dev
```

## Start the test infrastructure

```bash
docker compose -f docker-compose.test.yml up -d
```

This starts PostgreSQL 16 (port 5432), MySQL 8.0 (port 3306), and Redis 7 (port 6379).

## Verify the setup

```bash
make check  # lint + type + security
make test   # unit tests (SQLite only, no Docker)
```

## Project layout

```
src/fastapi_tenancy/
├── cache/           TenantCache
├── core/            Config, Context, Exceptions, Types
├── isolation/       Schema, Database, RLS, Hybrid providers
├── middleware/      Raw ASGI TenancyMiddleware
├── migrations/      TenantMigrationManager
├── resolution/      Header, Subdomain, Path, JWT resolvers
├── storage/         TenantStore, SQLAlchemy, InMemory, Redis
└── utils/           Validation, security helpers, DB compat

tests/
├── unit/            Fast tests — SQLite + InMemoryStore
├── integration/     Live PostgreSQL + MySQL + Redis
└── e2e/             Full application stack

docs/                This documentation site
```
