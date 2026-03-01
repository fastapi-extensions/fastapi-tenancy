"""
Advanced Example 1 — Multi-Tenant E-Commerce Platform
=======================================================
A production-ready SaaS e-commerce backend demonstrating:

  ✅ DATABASE isolation: each tenant has a completely separate PostgreSQL database
  ✅ Path-based resolution: /t/{tenant}/orders
  ✅ Alembic migrations: per-tenant schema migrations
  ✅ TenantConfig feature flags: feature gating from tenant metadata
  ✅ Custom audit store: writes audit logs to a dedicated DB table
  ✅ Background onboarding: provision + seed demo data in a BackgroundTask
  ✅ Soft delete lifecycle: ACTIVE → SUSPENDED → DELETED
  ✅ Admin API: CRUD on tenants

Isolation model
---------------
DATABASE mode gives the strongest isolation guarantee:

    Request for tenant "acme-corp"
        → TenancyMiddleware reads /t/acme-corp/orders path prefix
        → looks up acme-corp in the tenant store
        → gets connection URL: postgresql://…/tenant_acme_corp_db
        → yields AsyncSession connected to that database
        → route handler sees only acme-corp's data, physically separated

Architecture
------------

    ┌───────────────────────────────────────────────────────┐
    │  PostgreSQL (main)                                    │
    │  database: saas_platform                              │
    │  table: tenants (metadata store)                      │
    │  table: audit_logs (global audit trail)               │
    └───────────────────────────────────────────────────────┘

    ┌───────────────────────────────────────────────────────┐
    │  PostgreSQL (per tenant)                              │
    │  database: tenant_acme_corp_db                        │
    │  tables: products, orders, order_items, customers     │
    └───────────────────────────────────────────────────────┘

    ┌───────────────────────────────────────────────────────┐
    │  PostgreSQL (per tenant)                              │
    │  database: tenant_globex_db                           │
    │  tables: products, orders, order_items, customers     │
    └───────────────────────────────────────────────────────┘

Run with Docker
---------------
    docker compose up --build

    # Register tenant (creates a new database)
    curl -X POST http://localhost:8000/admin/tenants \\
         -H "Content-Type: application/json" \\
         -d '{"identifier": "acme-corp", "name": "Acme Corp", "plan": "enterprise"}'

    # Create a product
    curl -X POST http://localhost:8000/t/acme-corp/products \\
         -H "Content-Type: application/json" \\
         -d '{"name": "Widget Pro", "price": 29.99, "sku": "WGT-001"}'

    # Create an order
    curl -X POST http://localhost:8000/t/acme-corp/orders \\
         -H "Content-Type: application/json" \\
         -d '{"customer_email": "alice@example.com", "product_ids": [1]}'

    # List orders
    curl http://localhost:8000/t/acme-corp/orders

    # Suspend a tenant
    curl -X POST http://localhost:8000/admin/tenants/acme-corp/suspend

    # Suspended tenant → 403 on all tenant routes
    curl http://localhost:8000/t/acme-corp/orders
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import os
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from fastapi_tenancy import TenancyConfig, TenancyManager, TenancyMiddleware, Tenant
from fastapi_tenancy.dependencies import (
    TenantDep,
    make_tenant_config_dependency,
    make_tenant_db_dependency,
)
from fastapi_tenancy.storage.database import SQLAlchemyTenantStore

if TYPE_CHECKING:
    from fastapi_tenancy.core.types import AuditLog, TenantConfig

# ── Tenant-scoped database models (live in per-tenant DB) ─────────────────────

class TenantBase(DeclarativeBase):
    pass


class Product(TenantBase):
    __tablename__ = "products"
    id:    Mapped[int]   = mapped_column(primary_key=True)
    name:  Mapped[str]   = mapped_column(String(255), nullable=False)
    sku:   Mapped[str]   = mapped_column(String(64),  nullable=False, unique=True)
    price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    stock: Mapped[int]   = mapped_column(Integer, default=0)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "sku": self.sku,
                "price": float(self.price), "stock": self.stock}


class Order(TenantBase):
    __tablename__ = "orders"
    id:             Mapped[int]      = mapped_column(primary_key=True)
    customer_email: Mapped[str]      = mapped_column(String(255), nullable=False)
    total:          Mapped[float]    = mapped_column(Numeric(12, 2), default=0)
    status:         Mapped[str]      = mapped_column(String(32),  default="pending")
    created_at:     Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                      default=lambda: datetime.now(UTC))
    items: Mapped[list[OrderItem]] = relationship("OrderItem", back_populates="order",
                                                    lazy="selectin")

    def to_dict(self) -> dict:
        return {"id": self.id, "customer_email": self.customer_email,
                "total": float(self.total), "status": self.status,
                "items": [i.to_dict() for i in self.items],
                "created_at": self.created_at.isoformat()}


class OrderItem(TenantBase):
    __tablename__ = "order_items"
    id:         Mapped[int]   = mapped_column(primary_key=True)
    order_id:   Mapped[int]   = mapped_column(ForeignKey("orders.id"), nullable=False)
    product_id: Mapped[int]   = mapped_column(ForeignKey("products.id"), nullable=False)
    quantity:   Mapped[int]   = mapped_column(Integer, default=1)
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    order:      Mapped[Order]   = relationship("Order",   back_populates="items")
    product:    Mapped[Product] = relationship("Product")

    def to_dict(self) -> dict:
        return {"product_id": self.product_id, "quantity": self.quantity,
                "unit_price": float(self.unit_price)}


# ── Main DB models (live in saas_platform DB) ─────────────────────────────────

class MainBase(DeclarativeBase):
    pass


class AuditLogRecord(MainBase):
    """Persistent audit log — written to the main DB, not per-tenant."""
    __tablename__ = "audit_logs"
    id:          Mapped[int]      = mapped_column(primary_key=True)
    tenant_id:   Mapped[str]      = mapped_column(String(255), nullable=False, index=True)
    user_id:     Mapped[str | None] = mapped_column(String(255))
    action:      Mapped[str]      = mapped_column(String(64),  nullable=False)
    resource:    Mapped[str]      = mapped_column(String(64),  nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(255))
    timestamp:   Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                   default=lambda: datetime.now(UTC))
    details:     Mapped[str | None] = mapped_column(Text)


# ── Custom TenancyManager with persistent audit log ───────────────────────────

class PlatformManager(TenancyManager):
    """
    Extends TenancyManager to write audit logs to the platform database
    instead of just logging to stdout.
    """

    def __init__(self, *args: Any, main_db_url: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._main_engine = create_async_engine(main_db_url, echo=False)

    async def initialize(self) -> None:
        # Create audit_logs table in the main DB.
        async with self._main_engine.begin() as conn:
            await conn.run_sync(MainBase.metadata.create_all)
        await super().initialize()

    async def write_audit_log(self, entry: AuditLog) -> None:
        """Persist audit entries to the platform DB instead of just logging."""
        import json  # noqa: PLC0415
        async with AsyncSession(self._main_engine) as session, session.begin():
            record = AuditLogRecord(
                tenant_id=entry.tenant_id,
                user_id=entry.user_id,
                action=entry.action,
                resource=entry.resource,
                resource_id=entry.resource_id,
                details=json.dumps(entry.metadata) if entry.metadata else None,
            )
            session.add(record)

    async def close(self) -> None:
        await super().close()
        await self._main_engine.dispose()


# ── Config ────────────────────────────────────────────────────────────────────

_MAIN_DB = os.getenv("MAIN_DATABASE_URL",
                     "postgresql+asyncpg://postgres:postgres@localhost/saas_platform")
_DB_TMPL = os.getenv("TENANT_DATABASE_TEMPLATE",
                     "postgresql+asyncpg://postgres:postgres@localhost/{database_name}")

config = TenancyConfig(
    database_url=_MAIN_DB,
    resolution_strategy="path",   # ← tenant from /t/{tenant}/...
    path_prefix="/t",
    isolation_strategy="database",
    database_url_template=_DB_TMPL,    # ← each tenant gets tenant_<slug>_db
    max_cached_engines=50,
    enable_soft_delete=True,           # ACTIVE → DELETED (no hard purge by default)
)

store   = SQLAlchemyTenantStore(config.database_url)
manager = PlatformManager(config, store, main_db_url=_MAIN_DB)

get_db            = make_tenant_db_dependency(manager)
get_tenant_config = make_tenant_config_dependency(manager)


# ── Background onboarding ─────────────────────────────────────────────────────

async def _onboard_tenant_background(tenant: Tenant) -> None:
    """
    Runs asynchronously after the /admin/tenants POST returns 201.
    Seeds the new tenant's database with a demo product.
    """
    async with manager.isolation_provider.get_session(tenant) as session, session.begin():
        demo = Product(name="Starter Product", sku="DEMO-001", price=9.99, stock=100)
        session.add(demo)
    print(f"✅ Onboarded tenant {tenant.identifier} with demo data")


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.initialize()
    print("✅ E-commerce platform ready")
    yield
    await manager.close()


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Multi-Tenant E-Commerce", lifespan=lifespan)
app.add_middleware(
    TenancyMiddleware,
    manager=manager,
    excluded_paths=["/health", "/admin", "/docs", "/openapi.json"],
)


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class TenantCreate(BaseModel):
    identifier: str
    name: str
    plan: str = "starter"
    max_products: int = 100

class ProductCreate(BaseModel):
    name: str
    sku: str
    price: float
    stock: int = 0

class OrderCreate(BaseModel):
    customer_email: str
    product_ids: list[int]


# ── Admin routes (no tenant middleware) ───────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/admin/tenants", status_code=201)
async def create_tenant(body: TenantCreate, bg: BackgroundTasks):
    """
    Register a new tenant.  Creates a dedicated database and provisions
    tables.  Returns immediately; demo data seeding runs in background.
    """
    try:
        tenant = await manager.register_tenant(
            identifier=body.identifier,
            name=body.name,
            metadata={
                "plan": body.plan,
                "max_products": body.max_products,
                "features_enabled": ["basic_reporting"] if body.plan == "starter" else
                                    ["basic_reporting", "advanced_analytics", "webhooks"],
            },
            app_metadata=TenantBase.metadata,
        )
        # Seed demo data without blocking the response.
        bg.add_task(_onboard_tenant_background, tenant)
        return {
            "id": tenant.id,
            "identifier": tenant.identifier,
            "database": f"tenant_{tenant.identifier.replace('-', '_')}_db",
            "status": tenant.status,
        }
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/admin/tenants")
async def list_admin_tenants():
    tenants = await store.list()
    return [{"identifier": t.identifier, "name": t.name,
             "status": t.status, "plan": t.metadata.get("plan")} for t in tenants]


@app.post("/admin/tenants/{identifier}/suspend")
async def suspend_tenant(identifier: str):
    try:
        tenant = await store.get_by_identifier(identifier)
        updated = await manager.suspend_tenant(tenant.id)
    except Exception as exc:
        raise HTTPException(404, str(exc)) from exc
    else:
        return {"identifier": updated.identifier, "status": updated.status}


@app.post("/admin/tenants/{identifier}/activate")
async def activate_tenant_route(identifier: str):
    try:
        tenant = await store.get_by_identifier(identifier)
        updated = await manager.activate_tenant(tenant.id)
    except Exception as exc:
        raise HTTPException(404, str(exc)) from exc
    else:
        return {"identifier": updated.identifier, "status": updated.status}


# ── Tenant-scoped routes ──────────────────────────────────────────────────────
# URL prefix: /t/{tenant_identifier}/...
# TenancyMiddleware reads path_prefix="/t" and strips it before routing.

@app.get("/t/{tenant_id}/products")
async def list_products(
    tenant_id: str,
    tenant:    TenantDep,
    session:   Annotated[AsyncSession, Depends(get_db)],
):
    result = await session.execute(select(Product))
    rows   = result.scalars().all()
    return {"tenant": tenant.identifier, "products": [p.to_dict() for p in rows]}


@app.post("/t/{tenant_id}/products", status_code=201)
async def create_product(
    tenant_id: str,
    body:      ProductCreate,
    tenant:    TenantDep,
    config_:   Annotated[TenantConfig, Depends(get_tenant_config)],
    session:   Annotated[AsyncSession, Depends(get_db)],
):
    """Feature-gated by plan: starter tenants are limited by max_products."""
    if config_.max_users is not None:
        count_result = await session.execute(select(Product))
        current_count = len(count_result.scalars().all())
        max_products = tenant.metadata.get("max_products", 100)
        if current_count >= max_products:
            raise HTTPException(403, f"Product limit reached: {max_products} (upgrade your plan)")

    product = Product(**body.model_dump())
    session.add(product)
    await session.commit()
    await session.refresh(product)
    return product.to_dict()


@app.get("/t/{tenant_id}/orders")
async def list_orders(
    tenant_id: str,
    tenant:    TenantDep,
    session:   Annotated[AsyncSession, Depends(get_db)],
):
    result = await session.execute(select(Order))
    rows   = result.scalars().all()
    return {"tenant": tenant.identifier, "orders": [o.to_dict() for o in rows]}


@app.post("/t/{tenant_id}/orders", status_code=201)
async def create_order(
    tenant_id: str,
    body:      OrderCreate,
    tenant:    TenantDep,
    session:   Annotated[AsyncSession, Depends(get_db)],
):
    """Create an order, fetch products, compute total, decrement stock."""
    products = []
    for pid in body.product_ids:
        product = await session.get(Product, pid)
        if product is None:
            raise HTTPException(404, f"Product {pid} not found")
        if product.stock < 1:
            raise HTTPException(409, f"Product {pid} is out of stock")
        products.append(product)

    order = Order(customer_email=body.customer_email, status="confirmed")
    session.add(order)
    await session.flush()

    total = 0.0
    for product in products:
        item = OrderItem(order_id=order.id, product_id=product.id,
                         quantity=1, unit_price=product.price)
        session.add(item)
        total += float(product.price)
        product.stock -= 1  # type: ignore[assignment]

    order.total = total  # type: ignore[assignment]
    await session.commit()
    await session.refresh(order)
    return order.to_dict()
