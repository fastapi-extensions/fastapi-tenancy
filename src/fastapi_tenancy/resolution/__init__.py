"""Tenant resolution strategies.

Resolution strategies extract the tenant identifier from an incoming HTTP
request.  All strategies implement
:class:`~fastapi_tenancy.resolution.base.BaseTenantResolver`.

Built-in strategies
-------------------
:class:`HeaderTenantResolver`
    Read a named HTTP header (default: ``X-Tenant-ID``).

:class:`SubdomainTenantResolver`
    Extract the leftmost subdomain from the ``Host`` header
    (e.g. ``acme-corp.example.com`` → ``"acme-corp"``).

:class:`PathTenantResolver`
    Parse a fixed URL path prefix (e.g. ``/tenants/acme-corp/...``).

:class:`JWTTenantResolver`
    Decode a Bearer JWT and read a configured payload claim.

:class:`ResolverFactory`
    Build any built-in resolver from a
    :class:`~fastapi_tenancy.core.config.TenancyConfig` instance.

Custom resolvers
----------------
Subclass :class:`BaseTenantResolver` and pass the instance directly to
:class:`~fastapi_tenancy.manager.TenancyManager`::

    manager = TenancyManager(
        config=config,
        tenant_store=store,
        resolver=MyCookieResolver(cookie_name="session", tenant_store=store),
    )
"""

from fastapi_tenancy.resolution.base import BaseTenantResolver
from fastapi_tenancy.resolution.factory import ResolverFactory
from fastapi_tenancy.resolution.header import HeaderTenantResolver
from fastapi_tenancy.resolution.path import PathTenantResolver
from fastapi_tenancy.resolution.subdomain import SubdomainTenantResolver

# JWT resolver is optional — requires: pip install fastapi-tenancy[jwt]
try:
    from fastapi_tenancy.resolution.jwt import JWTTenantResolver
except ImportError:
    JWTTenantResolver = None  # type: ignore[assignment, misc]

__all__ = [
    "BaseTenantResolver",
    "HeaderTenantResolver",
    "JWTTenantResolver",
    "PathTenantResolver",
    "ResolverFactory",
    "SubdomainTenantResolver",
]
