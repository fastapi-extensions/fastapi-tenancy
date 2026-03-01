---
title: Isolation Strategies
description: Choosing and configuring the right data isolation strategy for your multi-tenant application.
---

# Isolation Strategies

An **isolation strategy** controls how tenant data is separated at the database layer. fastapi-tenancy supports four strategies, each with different trade-offs between isolation strength, operational complexity, and resource overhead.

## Choosing a strategy

```mermaid
flowchart TD
    A[How many tenants?] -->|< 500| B{Need full isolation?}
    A -->|> 500| E[RLS]
    B -->|Yes| C{Different databases?}
    B -->|No, cost matters| E
    C -->|Yes| D[Database]
    C -->|No| F[Schema]
    F -->|Mixed tiers?| G[Hybrid]

    style F fill:#009688,color:#fff
    style D fill:#00695C,color:#fff
    style E fill:#00BFA5,color:#000
    style G fill:#004D40,color:#fff
```

## Comparison

| | Schema | Database | RLS | Hybrid |
|--|--------|----------|-----|--------|
| **Isolation level** | Strong | Strongest | Moderate | Mixed |
| **PostgreSQL** | ✅ | ✅ | ✅ | ✅ |
| **MySQL** | ✅* | ✅ | ❌ | ❌ |
| **SQLite** | ✅† | ❌ | ❌ | ❌ |
| **MSSQL** | ✅ | ❌ | ❌ | ❌ |
| **DDL per tenant** | 1 schema | 1 database | None | Mixed |
| **Connection pool** | Shared | Per-tenant (LRU) | Shared | Mixed |
| **Backup granularity** | Schema dump | Database dump | Row filter | Mixed |
| **Best for** | Most SaaS apps | Strict compliance | Large tenant counts | Tiered products |

*MySQL delegates to DatabaseIsolationProvider (uses separate databases).
†SQLite falls back to table-name prefixing.

## Quick configuration

=== "Schema"

    ```python
    config = TenancyConfig(
        database_url="postgresql+asyncpg://user:pass@localhost/myapp",
        isolation_strategy="schema",
        schema_prefix="tenant_",  # acme-corp → schema "tenant_acme_corp"
    )
    ```

=== "Database"

    ```python
    config = TenancyConfig(
        database_url="postgresql+asyncpg://user:pass@localhost/master",
        isolation_strategy="database",
        database_url_template="postgresql+asyncpg://user:pass@localhost/{database_name}",
    )
    ```

=== "RLS"

    ```python
    config = TenancyConfig(
        database_url="postgresql+asyncpg://user:pass@localhost/myapp",
        isolation_strategy="rls",
    )
    ```

=== "Hybrid"

    ```python
    config = TenancyConfig(
        database_url="postgresql+asyncpg://user:pass@localhost/myapp",
        isolation_strategy="hybrid",
        premium_isolation_strategy="schema",
        standard_isolation_strategy="rls",
        premium_tenants=["t-enterprise-1", "t-enterprise-2"],
    )
    ```

## In-depth guides

- [Schema isolation](schema.md)
- [Database isolation](database.md)
- [Row-Level Security](rls.md)
- [Hybrid isolation](hybrid.md)
