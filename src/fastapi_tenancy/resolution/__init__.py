"""Tenant resolution strategies: header, subdomain, path, JWT, and custom."""

from fastapi_tenancy.resolution.base import BaseTenantResolver
from fastapi_tenancy.resolution.header import HeaderTenantResolver
from fastapi_tenancy.resolution.jwt import JWTTenantResolver
from fastapi_tenancy.resolution.path import PathTenantResolver
from fastapi_tenancy.resolution.subdomain import SubdomainTenantResolver

__all__ = [
    "BaseTenantResolver",
    "HeaderTenantResolver",
    "JWTTenantResolver",
    "PathTenantResolver",
    "SubdomainTenantResolver",
]
