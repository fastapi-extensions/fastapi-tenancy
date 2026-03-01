---
title: Audit Logging
description: Structured audit trail for all tenant operations.
---

# Audit Logging

fastapi-tenancy provides structured audit logging via `AuditLog` — an immutable Pydantic model that records who did what to which resource, and when.

## What gets logged

By default, `TenancyManager.write_audit_log()` logs entries at `WARNING` level to the standard Python logger. Use the `make_audit_log_dependency` factory to record application-level operations from route handlers.

## `AuditLog` structure

```python
class AuditLog(BaseModel):
    tenant_id:   str           # owning tenant ID
    user_id:     str | None    # authenticated user (None for system ops)
    action:      str           # verb: "create", "update", "delete", ...
    resource:    str           # resource type: "order", "user", ...
    resource_id: str | None    # specific resource identifier
    metadata:    dict          # supplementary context (diff, old values, …)
    ip_address:  str | None    # client IP
    user_agent:  str | None    # client user-agent
    timestamp:   datetime      # UTC timestamp
```

## Recording audit entries

```python
from fastapi_tenancy.dependencies import make_audit_log_dependency

get_audit = make_audit_log_dependency(manager)

@app.delete("/orders/{order_id}")
async def delete_order(
    order_id: str,
    tenant: TenantDep,
    session: SessionDep,
    audit: Annotated[Any, Depends(get_audit)],
):
    order = await session.get(Order, order_id)
    if not order:
        raise HTTPException(404)

    await session.delete(order)
    await session.commit()

    await audit(
        action="delete",
        resource="order",
        resource_id=order_id,
        user_id="user-from-jwt",
        metadata={"description": order.description},
    )
    return {"deleted": True}
```

## Persisting to a database

Implement the `AuditLogWriter` protocol and pass it to `TenancyManager` at construction:

```python
from fastapi_tenancy.manager import AuditLogWriter

class DatabaseAuditWriter:
    """Implements AuditLogWriter — persists entries to a dedicated audit table."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def write(self, entry: AuditLog) -> None:
        async with self._session_factory() as session:
            row = AuditLogRow(
                tenant_id=entry.tenant_id,
                user_id=entry.user_id,
                action=entry.action,
                resource=entry.resource,
                resource_id=entry.resource_id,
                metadata=entry.metadata,
                ip_address=entry.ip_address,
                user_agent=entry.user_agent,
                timestamp=entry.timestamp,
            )
            session.add(row)
            await session.commit()

manager = TenancyManager(
    config,
    store,
    audit_writer=DatabaseAuditWriter(session_factory),
)
```

## Forwarding to external systems

```python
import boto3
from fastapi_tenancy.manager import AuditLogWriter

class CloudWatchAuditWriter:
    """Implements AuditLogWriter — forwards entries to AWS CloudWatch Logs."""

    async def write(self, entry: AuditLog) -> None:
        cloudwatch = boto3.client("logs")
        cloudwatch.put_log_events(
            logGroupName="/fastapi-tenancy/audit",
            logStreamName=entry.tenant_id,
            logEvents=[{
                "timestamp": int(entry.timestamp.timestamp() * 1000),
                "message": entry.model_dump_json(),
            }],
        )

manager = TenancyManager(config, store, audit_writer=CloudWatchAuditWriter())
```

## Enabling / disabling

Audit logging is controlled by `TenancyConfig.enable_audit_logging`:

```python
config = TenancyConfig(
    database_url="...",
    enable_audit_logging=True,   # default
)
```

When `False`, calls to `write_audit_log()` are silently skipped.
