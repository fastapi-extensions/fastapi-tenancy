"""Tenant-scoped Redis cache utilities.

Requires the ``redis`` extra::

    pip install fastapi-tenancy[redis]
"""

try:
    from fastapi_tenancy.cache.tenant_cache import TenantCache

    __all__ = ["TenantCache"]
except ImportError:
    TenantCache = None  # type: ignore[assignment, misc]
    __all__ = []
