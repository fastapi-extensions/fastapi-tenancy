"""Unit tests for :class:`~fastapi_tenancy.isolation.base.BaseIsolationProvider`."""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from typing import TYPE_CHECKING, Any

import pytest

from fastapi_tenancy.core.config import TenancyConfig
from fastapi_tenancy.core.types import IsolationStrategy, SelectT, Tenant
from fastapi_tenancy.isolation.base import BaseIsolationProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    import sqlalchemy as sa


class _ConcreteProvider(BaseIsolationProvider):
    """Bare-minimum implementation for exercising the base-class surface."""

    @asynccontextmanager
    async def get_session(self, tenant: Tenant) -> AsyncGenerator[Any, Any]:
        yield None

    async def apply_filters(self, query: SelectT, tenant: Tenant) -> SelectT:
        return query

    async def initialize_tenant(self, tenant: Tenant, metadata: sa.MetaData | None = None) -> None:
        pass

    async def destroy_tenant(self, tenant: Tenant, **kwargs: Any) -> None:
        pass


def _cfg(**extra: Any) -> TenancyConfig:
    return TenancyConfig(
        database_url=extra.pop("database_url", "sqlite+aiosqlite:///:memory:"),
        isolation_strategy=extra.pop("isolation_strategy", IsolationStrategy.SCHEMA),
        schema_prefix=extra.pop("schema_prefix", "t_"),
        **extra,
    )


@pytest.mark.unit
class TestBaseIsolationProviderInit:
    def test_stores_config(self, make_tenant: Callable[..., Tenant]) -> None:
        cfg = _cfg()
        provider = _ConcreteProvider(cfg)
        assert provider.config is cfg

    def test_debug_log_on_init(
        self, caplog: pytest.LogCaptureFixture, make_tenant: Callable[..., Tenant]
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            _ConcreteProvider(_cfg())
        assert "_ConcreteProvider" in caplog.text


@pytest.mark.unit
class TestVerifyIsolation:
    async def test_base_returns_true(self, make_tenant: Callable[..., Tenant]) -> None:
        provider = _ConcreteProvider(_cfg())
        t = make_tenant()
        result = await provider.verify_isolation(t)
        assert result is True

    async def test_base_emits_warning(
        self,
        make_tenant: Callable[..., Tenant],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        provider = _ConcreteProvider(_cfg())
        t = make_tenant()
        with caplog.at_level(logging.WARNING):
            await provider.verify_isolation(t)
        assert "verify_isolation" in caplog.text


@pytest.mark.unit
class TestGetSchemaName:
    def test_delegates_to_config(self, make_tenant: Callable[..., Tenant]) -> None:
        cfg = _cfg(schema_prefix="myapp_")
        provider = _ConcreteProvider(cfg)
        t = make_tenant(identifier="acme-corp")
        expected = cfg.get_schema_name("acme-corp")
        assert provider.get_schema_name(t) == expected

    def test_prefix_applied(self, make_tenant: Callable[..., Tenant]) -> None:
        cfg = _cfg(schema_prefix="ts_")
        provider = _ConcreteProvider(cfg)
        t = make_tenant(identifier="hello-world")
        name = provider.get_schema_name(t)
        assert name.startswith("ts_")


@pytest.mark.unit
class TestGetDatabaseUrl:
    def test_returns_tenant_override_when_set(self, make_tenant: Callable[..., Tenant]) -> None:
        cfg = _cfg()
        provider = _ConcreteProvider(cfg)
        t = make_tenant(database_url="postgresql+asyncpg://u:p@host/tenant_db")
        assert provider.get_database_url(t) == "postgresql+asyncpg://u:p@host/tenant_db"

    def test_falls_back_to_config_when_no_override(
        self, make_tenant: Callable[..., Tenant]
    ) -> None:
        cfg = _cfg()
        provider = _ConcreteProvider(cfg)
        t = make_tenant()  # no database_url override
        url = provider.get_database_url(t)
        assert url is not None
        assert len(url) > 0


@pytest.mark.unit
class TestAbstractEnforcement:
    def test_cannot_instantiate_base_class_directly(self) -> None:
        cfg = _cfg()
        with pytest.raises(TypeError, match="abstract"):
            BaseIsolationProvider(cfg)  # type: ignore[abstract]
