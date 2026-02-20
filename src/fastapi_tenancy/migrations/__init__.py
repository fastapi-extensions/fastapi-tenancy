"""Database migration management via Alembic.

Requires the ``migrations`` extra::

    pip install fastapi-tenancy[migrations]
"""

try:
    from fastapi_tenancy.migrations.manager import MigrationManager

    __all__ = ["MigrationManager"]
except ImportError:
    MigrationManager = None  # type: ignore[assignment, misc]
    __all__ = []
