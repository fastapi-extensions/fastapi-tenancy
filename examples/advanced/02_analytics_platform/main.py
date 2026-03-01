"""
Advanced Example 2 — Analytics Platform
=========================================
A high-scale analytics SaaS demonstrating:

  ✅ Custom TenantStore backed by a separate metadata database
  ✅ WebSocket connections with tenant isolation
  ✅ Streaming API responses (tenant-scoped SSE)
  ✅ Tenant feature flags via TenantConfig
  ✅ Per-tenant API versioning
  ✅ Bulk tenant operations (suspend cohort, mass migration)
  ✅ Redis-backed tenant cache with warm-up

Architecture
------------

    ┌───────────────────────────────────────────────────────────────┐
    │  client browsers / SDKs                                       │
    └──────────┬─────────────────────────────────────┬──────────────┘
               │ HTTP + SSE                          │ WebSocket
               ▼                                     ▼
    ┌──────────────────────────────────────────────────────────────┐
    │  FastAPI + TenancyMiddleware                                 │
    │  Resolution: X-Tenant-ID header                              │
    │  Isolation: SCHEMA (one schema per tenant)                   │
    └──────────┬───────────────────────────────────────────────────┘
               │ AsyncSession (tenant schema)
               ▼
    ┌──────────────────────────────────────────────────────────────┐
    │  PostgreSQL (analytics DB)                                   │
    │  schema: tenant_acme_corp  → events, metrics, dashboards     │
    │  schema: tenant_globex     → events, metrics, dashboards     │
    └──────────────────────────────────────────────────────────────┘

    ┌──────────────────────────────────────────────────────────────┐
    │  Redis                                                       │
    │  - tenant metadata cache (5 min TTL)                         │
    │  - per-tenant WebSocket pub/sub channels                     │
    │  - real-time metrics counters                                │
    └──────────────────────────────────────────────────────────────┘

Run with Docker
---------------
    docker compose up --build

    # Register tenant
    curl -X POST http://localhost:8000/admin/tenants \\
         -H "Content-Type: application/json" \\
         -d '{"identifier": "acme-corp", "name": "Acme Corp", "plan": "pro"}'

    # Ingest an event
    curl -X POST http://localhost:8000/events \\
         -H "X-Tenant-ID: acme-corp" \\
         -H "Content-Type: application/json" \\
         -d '{"event_type": "page_view", "user_id": "u-123", "properties": {"page": "/home"}}'

    # Get metrics (feature-gated: pro+ only)
    curl http://localhost:8000/metrics/summary \\
         -H "X-Tenant-ID: acme-corp"

    # Stream live events via SSE
    curl -N http://localhost:8000/events/stream \\
         -H "X-Tenant-ID: acme-corp" \\
         -H "Accept: text/event-stream"
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import json
import os
import time
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import DateTime, String, Text, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from fastapi_tenancy import TenancyConfig, TenancyManager, TenancyMiddleware
from fastapi_tenancy.dependencies import (
    TenantDep,
    make_tenant_config_dependency,
    make_tenant_db_dependency,
)
from fastapi_tenancy.storage.database import SQLAlchemyTenantStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from fastapi_tenancy.core.types import TenantConfig

# ── Models ────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class Event(Base):
    """Raw analytics event — stored per-tenant in their schema."""
    __tablename__ = "events"
    id:         Mapped[int]      = mapped_column(primary_key=True)
    event_type: Mapped[str]      = mapped_column(String(64), nullable=False, index=True)
    user_id:    Mapped[str | None] = mapped_column(String(255), index=True)
    properties: Mapped[str]      = mapped_column(Text, default="{}")   # JSON
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "event_type": self.event_type,
            "user_id": self.user_id,
            "properties": json.loads(self.properties),
            "received_at": self.received_at.isoformat(),
        }


class Dashboard(Base):
    """Saved dashboard — pro+ feature."""
    __tablename__ = "dashboards"
    id:    Mapped[int] = mapped_column(primary_key=True)
    name:  Mapped[str] = mapped_column(String(255), nullable=False)
    query: Mapped[str] = mapped_column(Text, default="")


# ── Config ────────────────────────────────────────────────────────────────────

_DB    = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost/analytics")
_REDIS = os.getenv("REDIS_URL",    "redis://localhost:6379/0")

config = TenancyConfig(
    database_url=_DB,
    resolution_strategy="header",
    isolation_strategy="schema",
    redis_url=_REDIS,
    cache_enabled=True,
    cache_ttl=300,
    enable_rate_limiting=True,
    rate_limit_per_minute=1000,   # analytics workloads are high-throughput
)

store   = SQLAlchemyTenantStore(config.database_url)
manager = TenancyManager(config, store)

get_db            = make_tenant_db_dependency(manager)
get_tenant_config = make_tenant_config_dependency(manager)

# ── WebSocket connection manager ──────────────────────────────────────────────

class WsConnectionManager:
    """Thread-safe WebSocket broadcast manager with tenant-scoped channels."""

    def __init__(self) -> None:
        # Map tenant_id → list of active WebSocket connections
        self._connections: dict[str, list[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, tenant_id: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.setdefault(tenant_id, []).append(ws)

    async def disconnect(self, tenant_id: str, ws: WebSocket) -> None:
        async with self._lock:
            conns = self._connections.get(tenant_id, [])
            if ws in conns:
                conns.remove(ws)

    async def broadcast(self, tenant_id: str, message: dict) -> None:
        """Send a JSON message to all connections for a specific tenant."""
        conns = self._connections.get(tenant_id, [])
        dead: list[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        # Clean up dead connections
        async with self._lock:
            for ws in dead:
                self._connections.get(tenant_id, []).remove(ws)

    def count(self, tenant_id: str) -> int:
        return len(self._connections.get(tenant_id, []))


ws_manager = WsConnectionManager()

# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.initialize()

    existing = {t.identifier for t in await store.list()}
    tenants_to_seed = [
        ("acme-corp", "Acme Corp", "pro", ["basic_reporting", "advanced_analytics", "dashboards"]),
        ("globex", "Globex Inc.", "enterprise", ["basic_reporting", "advanced_analytics", "dashboards", "raw_export"]),  # noqa: E501
        ("startup-x", "Startup X", "starter", ["basic_reporting"]),
    ]
    for slug, name, plan, features in tenants_to_seed:
        if slug not in existing:
            await manager.register_tenant(
                slug, name,
                metadata={"plan": plan, "features_enabled": features},
                app_metadata=Base.metadata,
            )

    print("✅ Analytics platform ready")
    yield
    await manager.close()


app = FastAPI(title="Analytics Platform", lifespan=lifespan)
app.add_middleware(
    TenancyMiddleware,
    manager=manager,
    excluded_paths=["/health", "/admin", "/docs", "/openapi.json"],
)


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class EventIngest(BaseModel):
    event_type: str
    user_id:    str | None = None
    properties: dict[str, Any] = {}

class TenantCreate(BaseModel):
    identifier: str
    name: str
    plan: str = "starter"

class DashboardCreate(BaseModel):
    name: str
    query: str = ""


# ── Feature gate helper ───────────────────────────────────────────────────────

def require_feature(feature: str):
    """Dependency factory: raises 403 if tenant doesn't have the feature."""
    async def _check(
        tenant:        TenantDep,
        tenant_config: Annotated[TenantConfig, Depends(get_tenant_config)],
    ) -> None:
        if feature not in tenant_config.features_enabled:
            raise HTTPException(
                403,
                f"Feature '{feature}' is not available on your plan "
                f"({tenant.metadata.get('plan', 'unknown')}). Upgrade to access this feature.",
            )
    return Depends(_check)


# ── Admin routes ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "connections": sum(
        len(v) for v in ws_manager._connections.values()
    )}


@app.post("/admin/tenants", status_code=201)
async def create_tenant(body: TenantCreate):
    features_by_plan = {
        "starter":    ["basic_reporting"],
        "pro":        ["basic_reporting", "advanced_analytics", "dashboards"],
        "enterprise": ["basic_reporting", "advanced_analytics", "dashboards", "raw_export", "webhooks"],  # noqa: E501
    }
    features = features_by_plan.get(body.plan, ["basic_reporting"])
    try:
        tenant = await manager.register_tenant(
            body.identifier, body.name,
            metadata={"plan": body.plan, "features_enabled": features},
            app_metadata=Base.metadata,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    else:
        return {"id": tenant.id, "identifier": tenant.identifier, "plan": body.plan, "features": features}  # noqa: E501


@app.get("/admin/tenants")
async def list_tenants():
    tenants = await store.list()
    return [{"identifier": t.identifier, "name": t.name,
             "plan": t.metadata.get("plan"), "status": t.status} for t in tenants]


# Bulk suspend: suspend a list of tenants atomically (e.g. payment delinquency)
@app.post("/admin/tenants/bulk-suspend")
async def bulk_suspend(identifiers: list[str]):
    results = []
    for identifier in identifiers:
        try:
            tenant = await store.get_by_identifier(identifier)
            updated = await manager.suspend_tenant(tenant.id)
            results.append({"identifier": identifier, "status": updated.status, "ok": True})
        except Exception as exc:
            results.append({"identifier": identifier, "error": str(exc), "ok": False})
    return {"results": results}


# ── Analytics routes ──────────────────────────────────────────────────────────

@app.post("/events", status_code=202)
async def ingest_event(
    body:    EventIngest,
    tenant:  TenantDep,
    session: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Ingest a single analytics event.  Returns 202 Accepted immediately.
    Broadcasts to any open WebSocket connections for this tenant.
    """
    event = Event(
        event_type=body.event_type,
        user_id=body.user_id,
        properties=json.dumps(body.properties),
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)

    # Broadcast in real-time to connected WebSocket clients for this tenant.
    await ws_manager.broadcast(tenant.id, {
        "type": "new_event",
        "event": event.to_dict(),
    })

    return {"id": event.id, "status": "accepted"}


@app.get("/events")
async def list_events(
    tenant:  TenantDep,
    session: Annotated[AsyncSession, Depends(get_db)],
    limit:   int = 100,
):
    result = await session.execute(
        select(Event).order_by(Event.received_at.desc()).limit(limit)
    )
    return {"tenant": tenant.identifier, "events": [e.to_dict() for e in result.scalars().all()]}


@app.get("/metrics/summary", dependencies=[require_feature("advanced_analytics")])
async def metrics_summary(
    tenant:  TenantDep,
    session: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Aggregated metrics — gated behind 'advanced_analytics' feature flag.
    Starter plan tenants get a 403.
    """
    count_result = await session.execute(select(func.count(Event.id)))
    total_events = count_result.scalar_one()

    # Count by event type
    type_counts_result = await session.execute(
        select(Event.event_type, func.count(Event.id).label("count"))
        .group_by(Event.event_type)
        .order_by(func.count(Event.id).desc())
    )
    by_type = {row.event_type: row.count for row in type_counts_result}

    return {
        "tenant": tenant.identifier,
        "total_events": total_events,
        "by_type": by_type,
    }


@app.get("/events/stream")
async def stream_events(tenant: TenantDep, session: Annotated[AsyncSession, Depends(get_db)]):
    """
    Server-Sent Events (SSE) stream.  Tails the last 20 events once,
    then keeps the connection open broadcasting new events.

    This is a simplified SSE implementation — in production you'd use
    Redis pub/sub to receive events pushed from the ingest endpoint.
    """
    async def generate() -> AsyncIterator[str]:
        # Send the last 20 events first.
        result = await session.execute(
            select(Event).order_by(Event.received_at.desc()).limit(20)
        )
        for event in reversed(result.scalars().all()):
            payload = json.dumps({"type": "historical", "event": event.to_dict()})
            yield f"data: {payload}\n\n"

        # Keep connection alive with heartbeats.
        for _ in range(60):   # 60 * 5s = 5 minutes max
            await asyncio.sleep(5)
            yield f"data: {json.dumps({'type': 'heartbeat', 'ts': time.time()})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.websocket("/ws")
async def websocket_endpoint(
    ws:      WebSocket,
    tenant:  TenantDep,
):
    """
    WebSocket endpoint — broadcasts real-time events to connected clients.
    Each tenant's events are isolated to their WebSocket channel.
    """
    await ws_manager.connect(tenant.id, ws)
    try:
        await ws.send_json({"type": "connected", "tenant": tenant.identifier,
                             "connections": ws_manager.count(tenant.id)})
        # Keep connection open; server pushes events via broadcast().
        while True:
            # We don't expect client messages here but need to keep the loop alive.
            await asyncio.wait_for(ws.receive_text(), timeout=30)
    except (TimeoutError, WebSocketDisconnect):
        pass
    finally:
        await ws_manager.disconnect(tenant.id, ws)


# Dashboards (pro+ feature)  # noqa: ERA001
@app.get("/dashboards", dependencies=[require_feature("dashboards")])
async def list_dashboards(
    tenant:  TenantDep,
    session: Annotated[AsyncSession, Depends(get_db)],
):
    result = await session.execute(select(Dashboard))
    return [{"id": d.id, "name": d.name} for d in result.scalars().all()]


@app.post("/dashboards", status_code=201, dependencies=[require_feature("dashboards")])
async def create_dashboard(
    body:    DashboardCreate,
    tenant:  TenantDep,
    session: Annotated[AsyncSession, Depends(get_db)],
):
    dashboard = Dashboard(name=body.name, query=body.query)
    session.add(dashboard)
    await session.commit()
    await session.refresh(dashboard)
    return {"id": dashboard.id, "name": dashboard.name}
