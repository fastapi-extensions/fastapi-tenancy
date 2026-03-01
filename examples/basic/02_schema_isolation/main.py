"""
Basic Example 2 — Schema Isolation with PostgreSQL
====================================================
A real multi-tenant TODO app backed by PostgreSQL.
Each tenant gets its own schema: tenant_acme_corp, tenant_globex, etc.

What you'll learn
-----------------
- SQLAlchemyTenantStore for persisted tenant metadata
- SchemaIsolationProvider: one PostgreSQL schema per tenant
- make_tenant_db_dependency: inject a tenant-scoped SQLAlchemy session
- manager.register_tenant() to provision schema + tables atomically
- Admin endpoint to onboard new tenants at runtime

Run with Docker
---------------
    docker compose up

Run manually
------------
    # Start PostgreSQL
    docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:16

    pip install "fastapi-tenancy[postgres]"
    pip install "fastapi[standard]"

    export TENANCY_DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost/todos"
    uvicorn main:app --reload

Test
----
    # Register a tenant (creates schema + tables automatically)
    curl -X POST "http://localhost:8000/admin/tenants" \\
         -H "Content-Type: application/json" \\
         -d '{"identifier": "acme-corp", "name": "Acme Corporation"}'

    # Create a TODO for acme-corp
    curl -X POST "http://localhost:8000/todos" \\
         -H "X-Tenant-ID: acme-corp" \\
         -H "Content-Type: application/json" \\
         -d '{"title": "Buy milk", "done": false}'

    # List acme-corp's TODOs (other tenants see nothing)
    curl "http://localhost:8000/todos" -H "X-Tenant-ID: acme-corp"
"""
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import Boolean, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from fastapi_tenancy import TenancyConfig, TenancyManager, TenancyMiddleware, Tenant
from fastapi_tenancy.dependencies import get_current_tenant, make_tenant_db_dependency
from fastapi_tenancy.storage.database import SQLAlchemyTenantStore

# ── Database models ───────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class Todo(Base):
    __tablename__ = "todos"

    id:    Mapped[int]  = mapped_column(primary_key=True)
    title: Mapped[str]  = mapped_column(String(255), nullable=False)
    done:  Mapped[bool] = mapped_column(Boolean, default=False)

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "done": self.done}


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class TodoCreate(BaseModel):
    title: str
    done:  bool = False

class TodoUpdate(BaseModel):
    title: str | None = None
    done:  bool | None = None

class TenantCreate(BaseModel):
    identifier: str
    name: str

# ── fastapi-tenancy wiring ────────────────────────────────────────────────────

config = TenancyConfig(
    # Reads from TENANCY_DATABASE_URL env var if set, falls back to this default.
    database_url="postgresql+asyncpg://postgres:postgres@localhost/todos",
    resolution_strategy="header",
    isolation_strategy="schema",    # ← PostgreSQL schema per tenant
)

store   = SQLAlchemyTenantStore(config.database_url)
manager = TenancyManager(config, store)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.initialize()   # creates the "tenants" metadata table
    yield
    await manager.close()

app = FastAPI(title="Tenant TODOs", lifespan=lifespan)
app.add_middleware(
    TenancyMiddleware,
    manager=manager,
    excluded_paths=["/health", "/admin", "/docs", "/openapi.json"],
)

# Tenant-scoped DB dependency — every call yields a session whose
# search_path points at the correct tenant schema.
get_db = make_tenant_db_dependency(manager)

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


# Admin routes (no tenant middleware)
@app.post("/admin/tenants", status_code=201)
async def create_tenant(body: TenantCreate):
    """
    Register a tenant and provision its PostgreSQL schema + tables.

    This creates:
      - A row in the tenants metadata table
      - A PostgreSQL schema: tenant_<identifier>
      - All tables from Base.metadata inside that schema
    """
    try:
        tenant = await manager.register_tenant(
            identifier=body.identifier,
            name=body.name,
            app_metadata=Base.metadata,   # ← tells the provider which tables to create
        )
        return {
            "id": tenant.id,
            "identifier": tenant.identifier,
            "schema": f"tenant_{tenant.identifier.replace('-', '_')}",
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/admin/tenants")
async def list_tenants():
    """List all registered tenants."""
    tenants = await store.list()
    return [{"identifier": t.identifier, "name": t.name, "status": t.status} for t in tenants]


# Tenant-scoped TODO routes
@app.get("/todos")
async def list_todos(
    tenant: Annotated[Tenant, Depends(get_current_tenant)],
    session: Annotated[AsyncSession, Depends(get_db)],
):
    """List TODOs for the current tenant only."""
    result = await session.execute(select(Todo))
    todos = result.scalars().all()
    return {"tenant": tenant.identifier, "todos": [t.to_dict() for t in todos]}


@app.post("/todos", status_code=201)
async def create_todo(
    body: TodoCreate,
    session: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a TODO in the current tenant's schema."""
    todo = Todo(title=body.title, done=body.done)
    session.add(todo)
    await session.commit()
    await session.refresh(todo)
    return todo.to_dict()


@app.patch("/todos/{todo_id}")
async def update_todo(
    todo_id: int,
    body: TodoUpdate,
    session: Annotated[AsyncSession, Depends(get_db)],
):
    """Update a TODO (tenant-scoped — cannot update another tenant's TODO)."""
    todo = await session.get(Todo, todo_id)
    if todo is None:
        raise HTTPException(404, "Todo not found")
    if body.title is not None:
        todo.title = body.title  # type: ignore[assignment]
    if body.done is not None:
        todo.done = body.done    # type: ignore[assignment]
    await session.commit()
    return todo.to_dict()


@app.delete("/todos/{todo_id}", status_code=204)
async def delete_todo(
    todo_id: int,
    session: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete a TODO. Can only delete items in the current tenant's schema."""
    todo = await session.get(Todo, todo_id)
    if todo is None:
        raise HTTPException(404, "Todo not found")
    await session.delete(todo)
    await session.commit()
