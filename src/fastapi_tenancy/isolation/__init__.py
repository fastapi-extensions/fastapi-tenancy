"""Data isolation strategies for fastapi-tenancy.

All isolation providers implement
:class:`~fastapi_tenancy.isolation.base.BaseIsolationProvider`.

Strategies
----------
:class:`SchemaIsolationProvider`
    Dedicated database schema per tenant (PostgreSQL / MSSQL).
    Falls back to table-name prefix on SQLite and unknown dialects.

:class:`DatabaseIsolationProvider`
    Dedicated database instance per tenant (PostgreSQL, MySQL, SQLite).

:class:`RLSIsolationProvider`
    Shared schema with PostgreSQL Row-Level Security.
    Falls back to explicit ``WHERE tenant_id`` filter on other dialects.

:class:`HybridIsolationProvider`
    Routes premium tenants to one strategy and standard tenants to another.
    Reuses a single shared connection pool.

:class:`IsolationProviderFactory`
    Build any provider from a
    :class:`~fastapi_tenancy.core.config.TenancyConfig` instance.
"""

from fastapi_tenancy.isolation.base import BaseIsolationProvider
from fastapi_tenancy.isolation.database import DatabaseIsolationProvider
from fastapi_tenancy.isolation.factory import IsolationProviderFactory
from fastapi_tenancy.isolation.hybrid import HybridIsolationProvider
from fastapi_tenancy.isolation.rls import RLSIsolationProvider
from fastapi_tenancy.isolation.schema import SchemaIsolationProvider

__all__ = [
    "BaseIsolationProvider",
    "DatabaseIsolationProvider",
    "HybridIsolationProvider",
    "IsolationProviderFactory",
    "RLSIsolationProvider",
    "SchemaIsolationProvider",
]
