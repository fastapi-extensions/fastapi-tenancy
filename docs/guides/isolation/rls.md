---
title: Row-Level Security
description: PostgreSQL Row-Level Security isolation — shared tables, engine-level tenant filtering.
---

# Row-Level Security (RLS)

`isolation_strategy="rls"` uses PostgreSQL's Row-Level Security policies to enforce tenant boundaries at the database engine level. All tenants share a single schema and set of tables.

!!! note "PostgreSQL only"
    RLS is a PostgreSQL feature. For MySQL, SQLite, or MSSQL, use
    [Schema isolation](schema.md) instead.

## How it works

```mermaid
sequenceDiagram
    participant R as Request
    participant P as RLSIsolationProvider
    participant DB as PostgreSQL

    R->>P: get_session(acme)
    P->>DB: BEGIN
    P->>DB: SELECT set_config('app.current_tenant', 'acme-id', TRUE)
    Note over DB: RLS policy evaluates current_setting('app.current_tenant')<br/>on every statement automatically
    P-->>R: AsyncSession
    R->>DB: SELECT * FROM orders
    Note over DB: RLS silently rewrites to:<br/>SELECT * FROM orders WHERE tenant_id = 'acme-id'
```

## PostgreSQL setup

### 1. Add a `tenant_id` column

```sql
ALTER TABLE orders ADD COLUMN tenant_id TEXT NOT NULL;
ALTER TABLE users  ADD COLUMN tenant_id TEXT NOT NULL;
-- add to every table that should be tenant-scoped
```

### 2. Enable RLS and create policies

```sql
-- Orders table
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON orders
    AS PERMISSIVE
    FOR ALL
    USING (tenant_id = current_setting('app.current_tenant', TRUE))
    WITH CHECK (tenant_id = current_setting('app.current_tenant', TRUE));

-- Repeat for every tenant-scoped table
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON users
    USING (tenant_id = current_setting('app.current_tenant', TRUE))
    WITH CHECK (tenant_id = current_setting('app.current_tenant', TRUE));
```

!!! tip "Use `FORCE ROW LEVEL SECURITY`"
    `FORCE ROW LEVEL SECURITY` ensures policies apply even to the table owner.
    Without it, a superuser connection bypasses all policies.

### 3. Configure fastapi-tenancy

```python
config = TenancyConfig(
    database_url="postgresql+asyncpg://user:pass@localhost/myapp",
    isolation_strategy="rls",
)
```

## Defence in depth: `apply_filters`

In addition to the engine-level RLS policy, `RLSIsolationProvider.apply_filters()`
adds an explicit `WHERE tenant_id = :id` clause to every `SELECT` query. This
provides a second layer of protection if a policy is accidentally disabled:

```python
query = select(Order)
filtered = await provider.apply_filters(query, tenant)
# filtered: SELECT * FROM orders WHERE orders.tenant_id = :tenant_id_1
```

## Why `SET LOCAL` (not `SET SESSION`)

`SET SESSION` would persist the tenant setting on the pooled connection after
the request completes, causing the next request that reuses the same connection
to inherit the wrong tenant's GUC.

`SET LOCAL` expires at the end of the current transaction. The provider always
uses:

```sql
SELECT set_config('app.current_tenant', :tenant_id, TRUE)
--                                                   ^^^^ TRUE = transaction-local
```

## Insert / update with RLS

When inserting rows, your application code must set the `tenant_id` column:

```python
@app.post("/orders")
async def create_order(
    data: OrderCreate,
    tenant: Annotated[Tenant, Depends(get_current_tenant)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    order = Order(
        description=data.description,
        tenant_id=tenant.id,   # ← must set explicitly on INSERT
    )
    session.add(order)
    await session.commit()
    return order
```

!!! warning "RLS only filters reads automatically"
    `SELECT`, `UPDATE`, and `DELETE` are filtered automatically by the policy.
    `INSERT` requires you to set `tenant_id` manually; the `WITH CHECK` clause
    then verifies the value matches the current GUC.

## Provisioning

With RLS, there is no schema or database to create. `initialize_tenant()` is a
no-op — just register the tenant and start using it:

```python
tenant = await manager.register_tenant(
    identifier="acme-corp",
    name="Acme Corporation",
    # No app_metadata needed — tables already exist in the shared schema
)
```

## Destroying a tenant

`destroy_tenant()` deletes all rows belonging to the tenant from every table in
the `app_metadata`:

```python
await manager.delete_tenant(
    tenant_id="t-123",
    destroy_data=True,
    app_metadata=Base.metadata,  # required for RLS mode — passed through to destroy_tenant
)
```
