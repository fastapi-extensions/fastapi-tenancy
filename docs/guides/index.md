---
title: Guides
description: In-depth guides for every feature of fastapi-tenancy.
---

# Guides

Comprehensive guides for building production multi-tenant applications with fastapi-tenancy.

## Isolation strategies

How tenants' data is kept separate at the database layer.

- [Overview](isolation/index.md) — choose the right strategy for your requirements
- [Schema isolation](isolation/schema.md) — dedicated schema per tenant
- [Database isolation](isolation/database.md) — dedicated database per tenant
- [Row-Level Security](isolation/rls.md) — shared tables, PostgreSQL RLS policies
- [Hybrid isolation](isolation/hybrid.md) — route by tenant tier

## Resolution strategies

How the current tenant is identified from each HTTP request.

- [Overview](resolution/index.md) — choosing a resolution strategy
- [Header](resolution/header.md) — `X-Tenant-ID` header
- [Subdomain](resolution/subdomain.md) — `acme.example.com`
- [Path](resolution/path.md) — `/tenants/acme/orders`
- [JWT](resolution/jwt.md) — bearer token claim
- [Custom](resolution/custom.md) — write your own resolver

## Tenant storage

Where tenant metadata lives.

- [Overview](storage/index.md)
- [SQLAlchemy store](storage/sqlalchemy.md) — PostgreSQL / MySQL / SQLite / MSSQL
- [Redis store](storage/redis.md) — fast in-memory storage
- [In-memory store](storage/memory.md) — for testing and prototyping
- [Custom store](storage/custom.md) — implement the protocol

## Other topics

- [Middleware](middleware.md) — raw ASGI middleware internals and configuration
- [Dependency injection](dependencies.md) — session, config, and audit-log dependencies
- [Context management](context.md) — `TenantContext`, `tenant_scope`, background tasks
- [Migrations](migrations.md) — Alembic migrations across all tenants
- [Caching](caching.md) — LRU + Redis write-through tenant cache
- [Rate limiting](rate-limiting.md) — per-tenant sliding-window limits
- [Audit logging](audit-logging.md) — structured audit trail
- [Testing](testing.md) — unit, integration, and end-to-end test patterns
- [Deployment](deployment.md) — production checklist and Docker setup
