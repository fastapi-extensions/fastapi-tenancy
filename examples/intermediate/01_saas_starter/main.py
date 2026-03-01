"""
Intermediate Example 1 — SaaS Starter
=======================================
A production-pattern SaaS API with:
  - Row-Level Security (RLS) isolation: one table set, tenant_id column filters rows
  - JWT resolution: tenant comes from the JWT bearer token
  - Redis cache: hot tenant lookups in microseconds
  - Per-tenant rate limiting: sliding window via Redis Lua
  - Audit logging: every mutation is recorded

Architecture overview
---------------------

    ┌─────────────────────────────────────────────────────────────────┐
    │  HTTP Request                                                   │
    │  Authorization: Bearer <JWT with tenant_id claim>              │
    └─────────────────┬───────────────────────────────────────────────┘
                      │
              TenancyMiddleware
              ┌───────┴────────┐
              │ JWTResolver    │  decodes Bearer token → reads "tenant_id" claim
              │ RateLimiter    │  sliding-window check against Redis
              └───────┬────────┘
                      │ injects Tenant into ContextVar
                      ▼
              Route handler
              │   get_current_tenant()  → Tenant from ContextVar
              │   get_db()             → AsyncSession scoped to tenant
              │   audit()              → write audit log entry
              ▼
           PostgreSQL (shared schema, RLS enforces tenant_id filter)

Run with Docker
---------------
    docker compose up

    # Generate a test JWT (uses HS256 with secret "change-me-in-production")
    python generate_token.py acme-corp
    python generate_token.py globex

    # Use the token
    curl http://localhost:8000/posts \\
         -H "Authorization: Bearer <token>"

    curl -X POST "http://localhost:8000/posts" \\
         -H "Authorization: Bearer <token>" \\
         -H "Content-Type: application/json" \\
         -d '{"title": "Hello World", "body": "First post!"}'
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import os
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import String, Text, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from fastapi_tenancy import TenancyConfig, TenancyManager, TenancyMiddleware, Tenant
from fastapi_tenancy.dependencies import (
    TenantDep,
    get_current_tenant,
    make_audit_log_dependency,
    make_tenant_db_dependency,
)
from fastapi_tenancy.storage.database import SQLAlchemyTenantStore

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# ── Database models ───────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class Post(Base):
    """Blog post — shared table, isolated by RLS policy on tenant_id."""
    __tablename__ = "posts"

    id:        Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    title:     Mapped[str] = mapped_column(String(255), nullable=False)
    body:      Mapped[str] = mapped_column(Text, default="")

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "body": self.body}


# ── Config ────────────────────────────────────────────────────────────────────

_DB  = os.getenv("DATABASE_URL",  "postgresql+asyncpg://postgres:postgres@localhost/saas")
_RDS = os.getenv("REDIS_URL",     "redis://localhost:6379/0")
_JWT = os.getenv("JWT_SECRET",    "change-me-in-production")

config = TenancyConfig(
    database_url=_DB,

    # Tenant is identified from the JWT bearer token.
    # The resolver decodes the token and reads the "tenant_id" claim.
    resolution_strategy="jwt",
    jwt_secret=_JWT,
    jwt_algorithm="HS256",
    jwt_tenant_claim="tenant_id",  # which JWT claim holds the tenant identifier

    # RLS: all tenants share the same schema.  PostgreSQL RLS policies
    # add WHERE tenant_id = current_setting('app.current_tenant') automatically.
    isolation_strategy="rls",

    # Redis write-through cache — tenant lookups skip the DB on cache hit.
    redis_url=_RDS,
    cache_enabled=True,
    cache_ttl=300,          # 5 minutes

    # Per-tenant rate limiting via Redis sliding-window Lua script.
    enable_rate_limiting=True,
    rate_limit_per_minute=60,
    rate_limit_window_seconds=60,
)

store   = SQLAlchemyTenantStore(config.database_url)
manager = TenancyManager(config, store)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.initialize()

    # Seed demo tenants (idempotent in production you'd use a migration).
    existing = {t.identifier for t in await store.list()}
    for slug, name in [("acme-corp", "Acme Corp"), ("globex", "Globex Inc.")]:
        if slug not in existing:
            await manager.register_tenant(
                identifier=slug,
                name=name,
                metadata={"plan": "pro"},
                app_metadata=Base.metadata,
            )

    print("✅ SaaS starter ready")
    yield
    await manager.close()


app = FastAPI(title="SaaS Starter", lifespan=lifespan)
app.add_middleware(
    TenancyMiddleware,
    manager=manager,
    excluded_paths=["/health", "/docs", "/openapi.json"],
)

get_db    = make_tenant_db_dependency(manager)
get_audit = make_audit_log_dependency(manager)

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/posts")
async def list_posts(
    tenant:  TenantDep,
    session: Annotated[AsyncSession, Depends(get_db)],
):
    """
    PostgreSQL RLS automatically filters to tenant's rows.
    No WHERE clause needed in the query.
    """
    result = await session.execute(select(Post))
    posts  = result.scalars().all()
    return {"tenant": tenant.identifier, "count": len(posts), "posts": [p.to_dict() for p in posts]}


class PostCreate(BaseModel):
    title: str
    body:  str = ""


@app.post("/posts", status_code=201)
async def create_post(
    body:    PostCreate,
    tenant:  TenantDep,
    session: Annotated[AsyncSession, Depends(get_db)],
    audit:   Annotated[object, Depends(get_audit)],
):
    """
    Create a post.  tenant_id is stamped on the row explicitly — this is
    the defence-in-depth layer that RLS relies on.
    """
    post = Post(tenant_id=tenant.id, title=body.title, body=body.body)
    session.add(post)
    await session.commit()
    await session.refresh(post)

    # Write an audit entry for every mutation.
    await audit(
        action="create",
        resource="post",
        resource_id=str(post.id),
        metadata={"title": post.title},
    )

    return post.to_dict()


@app.delete("/posts/{post_id}", status_code=204)
async def delete_post(
    post_id: int,
    tenant:  TenantDep,
    session: Annotated[AsyncSession, Depends(get_db)],
    audit:   Annotated[object, Depends(get_audit)],
):
    # RLS guarantees this query can only find the current tenant's post.
    post = await session.get(Post, post_id)
    if post is None:
        raise HTTPException(404, "Post not found")

    await session.delete(post)
    await session.commit()
    await audit(action="delete", resource="post", resource_id=str(post_id))


@app.get("/me")
async def me(tenant: Annotated[Tenant, Depends(get_current_tenant)]):
    """Return current tenant info (no secrets exposed)."""
    return tenant.model_dump_safe()
