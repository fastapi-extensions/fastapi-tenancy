"""
Intermediate Example 2 — Hybrid Isolation (Premium vs Standard Tenants)
=========================================================================
Different isolation strategies for different customer tiers:

  Premium tenants  → SCHEMA isolation (dedicated PostgreSQL schema)
  Standard tenants → RLS isolation (shared tables, row-level filtering)

Business rationale
------------------
Enterprise customers pay for stronger data isolation guarantees — their data
lives in a completely separate schema.  Starter/Pro customers share tables
behind RLS, which is more cost-efficient at scale.

This is configured with a single isolation_strategy="hybrid" setting:

    PREMIUM:  isolation_strategy="schema"  (per-tenant schema)
    STANDARD: isolation_strategy="rls"     (shared tables + RLS)

Architecture
------------

    acme-corp (premium) ─┐
                          ├─► schema: tenant_acme_corp   ← dedicated schema
    globex    (premium) ─┘

    startup-a (standard) ─┐
                           ├─► public schema, tenant_id column + RLS policy
    startup-b (standard) ─┘

Run with Docker
---------------
    docker compose up

Test
----
    # Premium tenant
    curl http://localhost:8000/invoices -H "X-Tenant-ID: acme-corp"
    curl -X POST "http://localhost:8000/invoices" \\
         -H "X-Tenant-ID: acme-corp" \\
         -H "Content-Type: application/json" \\
         -d '{"amount": 9999.00, "description": "Enterprise license"}'

    # Standard tenant
    curl http://localhost:8000/invoices -H "X-Tenant-ID: startup-a"
    curl -X POST "http://localhost:8000/invoices" \\
         -H "X-Tenant-ID: startup-a" \\
         -H "Content-Type: application/json" \\
         -d '{"amount": 49.00, "description": "Monthly Pro plan"}'

    # Check which strategy each tenant uses
    curl http://localhost:8000/me -H "X-Tenant-ID: acme-corp"
    curl http://localhost:8000/me -H "X-Tenant-ID: startup-a"
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import os
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import Numeric, String, Text, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from fastapi_tenancy import TenancyConfig, TenancyManager, TenancyMiddleware
from fastapi_tenancy.core.types import IsolationStrategy
from fastapi_tenancy.dependencies import TenantDep, make_tenant_db_dependency
from fastapi_tenancy.storage.database import SQLAlchemyTenantStore

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# ── Database models ───────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class Invoice(Base):
    """
    Invoice row.  In RLS mode tenant_id enforces row-level isolation.
    In schema mode the row naturally lives in the tenant's schema,
    but we still stamp tenant_id for audit trails.
    """
    __tablename__ = "invoices"

    id:          Mapped[int]   = mapped_column(primary_key=True)
    tenant_id:   Mapped[str]   = mapped_column(String(255), nullable=False, index=True)
    amount:      Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    description: Mapped[str]   = mapped_column(Text, default="")
    paid:        Mapped[bool]  = mapped_column(default=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "amount": float(self.amount),
            "description": self.description,
            "paid": self.paid,
        }


# ── Config ────────────────────────────────────────────────────────────────────

_DB  = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost/billing")

# Premium tenant identifiers — these get dedicated schemas.
# Standard tenants (not in this list) share tables via RLS.
PREMIUM_TENANTS = ["acme-corp", "globex", "bigcorp"]

config = TenancyConfig(
    database_url=_DB,
    resolution_strategy="header",

    # HYBRID mode: dispatch to different providers based on tenant tier.
    isolation_strategy="hybrid",
    premium_tenants=PREMIUM_TENANTS,                  # ← these get schema isolation
    premium_isolation_strategy="schema",               # ← premium → dedicated schema
    standard_isolation_strategy="rls",                 # ← standard → shared + RLS

    # Engine cache — HYBRID creates many engines (one per premium tenant).
    max_cached_engines=200,
)

store   = SQLAlchemyTenantStore(config.database_url)
manager = TenancyManager(config, store)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.initialize()

    existing = {t.identifier for t in await store.list()}

    # Premium tenants
    for slug, name in [("acme-corp", "Acme Corp"), ("globex", "Globex Inc.")]:
        if slug not in existing:
            await manager.register_tenant(
                slug, name,
                metadata={"plan": "enterprise", "tier": "premium"},
                app_metadata=Base.metadata,
            )

    # Standard tenants
    for slug, name in [("startup-a", "Startup Alpha"), ("startup-b", "Startup Beta")]:
        if slug not in existing:
            await manager.register_tenant(
                slug, name,
                metadata={"plan": "starter", "tier": "standard"},
                app_metadata=Base.metadata,
            )

    print("✅ Hybrid billing app ready")
    print(f"   Premium tenants: {PREMIUM_TENANTS}")
    yield
    await manager.close()


app = FastAPI(title="Hybrid Billing", lifespan=lifespan)
app.add_middleware(
    TenancyMiddleware,
    manager=manager,
    excluded_paths=["/health", "/docs", "/openapi.json"],
)

get_db = make_tenant_db_dependency(manager)

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/me")
async def me(tenant: TenantDep):
    """Show the current tenant's info and which isolation tier they're on."""
    is_premium = config.is_premium_tenant(tenant.identifier)
    effective_strategy = config.get_isolation_strategy_for_tenant(tenant.identifier)
    return {
        "identifier": tenant.identifier,
        "name": tenant.name,
        "plan": tenant.metadata.get("plan"),
        "tier": "premium" if is_premium else "standard",
        "isolation_strategy": effective_strategy.value,
        "has_dedicated_schema": effective_strategy == IsolationStrategy.SCHEMA,
    }


@app.get("/invoices")
async def list_invoices(
    tenant:  TenantDep,
    session: Annotated[AsyncSession, Depends(get_db)],
):
    """
    For premium tenants: SELECT * FROM invoices queries the tenant's schema.
    For standard tenants: RLS adds WHERE tenant_id = 'xxx' automatically.
    The route code is identical — fastapi-tenancy handles the routing.
    """
    result = await session.execute(select(Invoice))
    rows   = result.scalars().all()
    return {
            "tenant": tenant.identifier,
            "count": len(rows),
            "invoices": [r.to_dict() for r in rows]
        }


class InvoiceCreate(BaseModel):
    amount:      float
    description: str = ""


@app.post("/invoices", status_code=201)
async def create_invoice(
    body:    InvoiceCreate,
    tenant:  TenantDep,
    session: Annotated[AsyncSession, Depends(get_db)],
):
    invoice = Invoice(tenant_id=tenant.id, amount=body.amount, description=body.description)
    session.add(invoice)
    await session.commit()
    await session.refresh(invoice)
    return invoice.to_dict()


@app.patch("/invoices/{invoice_id}/pay", status_code=200)
async def pay_invoice(
    invoice_id: int,
    tenant:     TenantDep,
    session:    Annotated[AsyncSession, Depends(get_db)],
):
    """Mark invoice as paid.  Isolation ensures only tenant's own invoices are accessible."""
    invoice = await session.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(404, "Invoice not found")
    invoice.paid = True  # type: ignore[assignment]
    await session.commit()
    return invoice.to_dict()
