---
title: Hybrid Isolation
description: Route tenants to different isolation strategies based on their tier — schema for premium, RLS for standard.
---

# Hybrid Isolation

`isolation_strategy="hybrid"` lets you apply different isolation strategies to different tenants based on their tier. Premium tenants get strong schema-level isolation; standard tenants share the same schema with RLS filtering.

!!! note "PostgreSQL only"
    Hybrid mode requires PostgreSQL because it combines Schema and RLS strategies.

## How it works

```mermaid
flowchart LR
    R[Request] --> M{Is premium tenant?}
    M -->|Yes| S[SchemaIsolationProvider\ntenant_acme schema]
    M -->|No| RLS[RLSIsolationProvider\nshared schema + RLS]

    style S fill:#009688,color:#fff
    style RLS fill:#00695C,color:#fff
```

The `HybridIsolationProvider` checks whether the tenant's ID appears in `config.premium_tenants`. If it does, the request is delegated to `SchemaIsolationProvider`; otherwise to `RLSIsolationProvider`.

## Configuration

```python
config = TenancyConfig(
    database_url="postgresql+asyncpg://user:pass@localhost/myapp",
    isolation_strategy="hybrid",
    premium_isolation_strategy="schema",   # default
    standard_isolation_strategy="rls",     # default
    premium_tenants=["t-enterprise-1", "t-enterprise-2"],
)
```

!!! warning "Strategies must differ"
    `premium_isolation_strategy` and `standard_isolation_strategy` must be set
    to different values. Setting both to the same value raises `ValidationError`
    at construction time.

## Promoting a tenant

To move a tenant from standard to premium tier, update the `premium_tenants`
list and provision their schema:

```python
# 1. Provision the schema for the tenant being promoted
tenant = await manager.store.get_by_id("t-123")
await manager.isolation_provider.initialize_tenant(tenant)

# 2. Update the config (or environment variable) to add to premium list
# TENANCY_PREMIUM_TENANTS=t-enterprise-1,t-enterprise-2,t-123
```

## Per-tenant override

The `Tenant.isolation_strategy` field allows individual tenants to override
the global strategy entirely, independent of the `premium_tenants` list:

```python
tenant = Tenant(
    id="t-special",
    identifier="special-corp",
    name="Special Corp",
    isolation_strategy=IsolationStrategy.DATABASE,  # always use DATABASE
)
```

When this field is set, `HybridIsolationProvider` delegates directly to the
per-tenant strategy, bypassing the premium/standard check.

## Use cases

**Tiered SaaS pricing**: Standard plan customers share infrastructure; Pro/Enterprise
customers get dedicated schemas for compliance and performance guarantees.

**Gradual migration**: Start all tenants on RLS, then migrate high-value
customers to schema isolation without changing application code.

**Compliance requirements**: Tenants under specific regulatory requirements
(e.g. healthcare, finance) can be tagged as premium to receive data-at-rest
isolation automatically.
