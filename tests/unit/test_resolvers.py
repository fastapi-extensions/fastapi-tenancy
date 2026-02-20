"""Unit tests — resolution strategies (header, subdomain, path, jwt, factory)

Coverage target: 100 %

All resolvers are tested with a mock TenantStore so no database I/O is needed.
Starlette Request is mocked with MagicMock.

Verified per resolver:
* Happy path — correct identifier extracted, store called, tenant returned
* Missing / empty field → TenantResolutionError
* Invalid identifier format → TenantResolutionError
* Store raises TenantNotFoundError — propagated
* Edge cases specific to each resolver
* ResolverFactory — each strategy produced, CUSTOM raises, missing config raises
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from fastapi_tenancy.core.exceptions import (
    ConfigurationError,
    TenantNotFoundError,
    TenantResolutionError,
)
from fastapi_tenancy.core.types import ResolutionStrategy, Tenant, TenantStatus
from fastapi_tenancy.resolution.header import HeaderTenantResolver
from fastapi_tenancy.resolution.path import PathTenantResolver
from fastapi_tenancy.resolution.subdomain import SubdomainTenantResolver

pytestmark = pytest.mark.unit

_NOW = datetime(2024, 1, 1, tzinfo=UTC)


def _tenant(identifier: str = "acme-corp") -> Tenant:
    return Tenant(
        id="t-001",
        identifier=identifier,
        name="Acme Corp",
        status=TenantStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _mock_store(tenant: Tenant | None = None, raises: Exception | None = None) -> AsyncMock:
    store = AsyncMock()
    if raises:
        store.get_by_identifier.side_effect = raises
    else:
        store.get_by_identifier.return_value = tenant or _tenant()
    return store


def _mock_request(
    headers: dict | None = None,
    host: str = "acme-corp.example.com",
    path: str = "/api/data",
) -> MagicMock:
    req = MagicMock()
    req.headers = dict(headers or {})
    req.url.hostname = host
    req.url.path = path
    return req


# ───────────────────────── HeaderTenantResolver ───────────────────────────────


class TestHeaderTenantResolver:
    async def test_happy_path_default_header(self):
        store = _mock_store()
        resolver = HeaderTenantResolver(tenant_store=store)
        req = _mock_request(headers={"X-Tenant-ID": "acme-corp"})
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"
        store.get_by_identifier.assert_awaited_once_with("acme-corp")

    async def test_custom_header_name(self):
        store = _mock_store()
        resolver = HeaderTenantResolver(header_name="X-Custom-Tenant", tenant_store=store)
        req = _mock_request(headers={"X-Custom-Tenant": "acme-corp"})
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"

    async def test_header_case_insensitive_default(self):
        store = _mock_store()
        resolver = HeaderTenantResolver(tenant_store=store)
        # lowercase variant
        req = _mock_request(headers={"x-tenant-id": "acme-corp"})
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"

    async def test_header_case_sensitive_fails_wrong_case(self):
        store = _mock_store()
        resolver = HeaderTenantResolver(tenant_store=store, case_sensitive=True)
        req = _mock_request(headers={"x-tenant-id": "acme-corp"})  # lowercase key
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_missing_header_raises_resolution_error(self):
        resolver = HeaderTenantResolver(tenant_store=_mock_store())
        req = _mock_request(headers={})
        with pytest.raises(TenantResolutionError) as exc:
            await resolver.resolve(req)
        assert exc.value.strategy == "header"

    async def test_empty_header_value_raises(self):
        resolver = HeaderTenantResolver(tenant_store=_mock_store())
        req = _mock_request(headers={"X-Tenant-ID": "   "})  # whitespace only
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_invalid_identifier_format_raises(self):
        resolver = HeaderTenantResolver(tenant_store=_mock_store())
        req = _mock_request(headers={"X-Tenant-ID": "INVALID_ID!"})
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_store_not_found_propagates(self):
        store = _mock_store(raises=TenantNotFoundError("acme-corp"))
        resolver = HeaderTenantResolver(tenant_store=store)
        req = _mock_request(headers={"X-Tenant-ID": "acme-corp"})
        with pytest.raises(TenantNotFoundError):
            await resolver.resolve(req)

    async def test_no_store_raises_value_error(self):
        resolver = HeaderTenantResolver(tenant_store=None)
        req = _mock_request(headers={"X-Tenant-ID": "acme-corp"})
        with pytest.raises(ValueError):
            await resolver.resolve(req)

    async def test_whitespace_stripped_from_header(self):
        store = _mock_store()
        resolver = HeaderTenantResolver(tenant_store=store)
        req = _mock_request(headers={"X-Tenant-ID": "  acme-corp  "})
        await resolver.resolve(req)
        store.get_by_identifier.assert_awaited_once_with("acme-corp")

    async def test_header_not_in_error_response(self):
        """Security: error should not reveal list of headers present."""
        resolver = HeaderTenantResolver(tenant_store=_mock_store())
        req = _mock_request(headers={"Authorization": "Bearer tok", "X-Foo": "bar"})
        try:
            await resolver.resolve(req)
        except TenantResolutionError as e:
            assert "Authorization" not in str(e)
            assert "X-Foo" not in str(e)


# ───────────────────────── SubdomainTenantResolver ────────────────────────────


class TestSubdomainTenantResolver:
    async def test_happy_path(self):
        store = _mock_store()
        resolver = SubdomainTenantResolver(domain_suffix=".example.com", tenant_store=store)
        req = _mock_request(host="acme-corp.example.com")
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"

    async def test_suffix_without_leading_dot_normalised(self):
        store = _mock_store()
        resolver = SubdomainTenantResolver(domain_suffix="example.com", tenant_store=store)
        req = _mock_request(host="acme-corp.example.com")
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"

    async def test_multi_level_subdomain_rightmost(self):
        store = _mock_store()
        resolver = SubdomainTenantResolver(domain_suffix=".example.com", tenant_store=store)
        req = _mock_request(host="app.acme-corp.example.com")
        await resolver.resolve(req)
        store.get_by_identifier.assert_awaited_once_with("acme-corp")

    async def test_no_hostname_raises(self):
        resolver = SubdomainTenantResolver(domain_suffix=".example.com", tenant_store=_mock_store())
        req = _mock_request(host="")
        req.url.hostname = None
        with pytest.raises(TenantResolutionError) as exc:
            await resolver.resolve(req)
        assert exc.value.strategy == "subdomain"

    async def test_wrong_suffix_raises(self):
        resolver = SubdomainTenantResolver(domain_suffix=".example.com", tenant_store=_mock_store())
        req = _mock_request(host="acme-corp.other.com")
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_apex_domain_no_subdomain_raises(self):
        resolver = SubdomainTenantResolver(domain_suffix=".example.com", tenant_store=_mock_store())
        req = _mock_request(host="example.com")
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_invalid_subdomain_format_raises(self):
        resolver = SubdomainTenantResolver(domain_suffix=".example.com", tenant_store=_mock_store())
        req = _mock_request(host="UPPER.example.com")
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_store_not_found_propagates(self):
        store = _mock_store(raises=TenantNotFoundError("acme-corp"))
        resolver = SubdomainTenantResolver(domain_suffix=".example.com", tenant_store=store)
        req = _mock_request(host="acme-corp.example.com")
        with pytest.raises(TenantNotFoundError):
            await resolver.resolve(req)


# ──────────────────────── PathTenantResolver ─────────────────────────────────


class TestPathTenantResolver:
    async def test_happy_path(self):
        store = _mock_store()
        resolver = PathTenantResolver(tenant_store=store)
        req = _mock_request(path="/tenants/acme-corp/users")
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"

    async def test_custom_prefix(self):
        store = _mock_store()
        resolver = PathTenantResolver(path_prefix="/api/v1/t", tenant_store=store)
        req = _mock_request(path="/api/v1/t/acme-corp/resource")
        await resolver.resolve(req)
        store.get_by_identifier.assert_awaited_once_with("acme-corp")

    async def test_trailing_slash_on_prefix_stripped(self):
        store = _mock_store()
        resolver = PathTenantResolver(path_prefix="/tenants/", tenant_store=store)
        req = _mock_request(path="/tenants/acme-corp/data")
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"

    async def test_wrong_prefix_raises(self):
        resolver = PathTenantResolver(path_prefix="/tenants", tenant_store=_mock_store())
        req = _mock_request(path="/api/acme-corp/data")
        with pytest.raises(TenantResolutionError) as exc:
            await resolver.resolve(req)
        assert exc.value.strategy == "path"

    async def test_no_identifier_after_prefix_raises(self):
        resolver = PathTenantResolver(path_prefix="/tenants", tenant_store=_mock_store())
        req = _mock_request(path="/tenants/")
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_invalid_identifier_raises(self):
        resolver = PathTenantResolver(path_prefix="/tenants", tenant_store=_mock_store())
        req = _mock_request(path="/tenants/INVALID!")
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_only_first_segment_used(self):
        store = _mock_store()
        resolver = PathTenantResolver(tenant_store=store)
        req = _mock_request(path="/tenants/acme-corp/users/123/orders")
        await resolver.resolve(req)
        store.get_by_identifier.assert_awaited_once_with("acme-corp")

    async def test_store_not_found_propagates(self):
        store = _mock_store(raises=TenantNotFoundError("acme-corp"))
        resolver = PathTenantResolver(tenant_store=store)
        req = _mock_request(path="/tenants/acme-corp/data")
        with pytest.raises(TenantNotFoundError):
            await resolver.resolve(req)


# ─────────────────────── JWTTenantResolver ───────────────────────────────────


class TestJWTTenantResolver:
    """JWT resolver requires python-jose — skip gracefully if not installed."""

    @pytest.fixture(autouse=True)
    def check_jose(self):
        pytest.importorskip("jose", reason="python-jose not installed")

    def _make_token(self, payload: dict, secret: str = "a" * 32) -> str:
        from jose import jwt
        return jwt.encode(payload, secret, algorithm="HS256")

    async def test_happy_path(self):
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver

        secret = "a" * 32
        token = self._make_token({"tenant_id": "acme-corp", "sub": "user"}, secret)
        store = _mock_store()
        resolver = JWTTenantResolver(secret=secret, tenant_store=store)
        req = _mock_request(headers={"Authorization": f"Bearer {token}"})
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"

    async def test_custom_claim_name(self):
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver

        secret = "b" * 32
        token = self._make_token({"org": "acme-corp"}, secret)
        store = _mock_store()
        resolver = JWTTenantResolver(secret=secret, tenant_claim="org", tenant_store=store)
        req = _mock_request(headers={"Authorization": f"Bearer {token}"})
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"

    def test_short_secret_raises(self):
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver

        with pytest.raises(ValueError, match="32"):
            JWTTenantResolver(secret="short", tenant_store=_mock_store())

    def test_empty_secret_raises(self):
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver

        with pytest.raises(ValueError):
            JWTTenantResolver(secret="", tenant_store=_mock_store())

    async def test_missing_auth_header_raises(self):
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver

        resolver = JWTTenantResolver(secret="a" * 32, tenant_store=_mock_store())
        req = _mock_request(headers={})
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_non_bearer_scheme_raises(self):
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver

        resolver = JWTTenantResolver(secret="a" * 32, tenant_store=_mock_store())
        req = _mock_request(headers={"Authorization": "Basic dXNlcjpwYXNz"})
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_invalid_token_raises(self):
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver

        resolver = JWTTenantResolver(secret="a" * 32, tenant_store=_mock_store())
        req = _mock_request(headers={"Authorization": "Bearer not.a.jwt"})
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_missing_tenant_claim_raises(self):
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver

        secret = "a" * 32
        token = self._make_token({"sub": "user-only-no-tenant"}, secret)
        resolver = JWTTenantResolver(secret=secret, tenant_store=_mock_store())
        req = _mock_request(headers={"Authorization": f"Bearer {token}"})
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_error_does_not_expose_claim_name(self):
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver

        secret = "a" * 32
        token = self._make_token({"sub": "user"}, secret)
        resolver = JWTTenantResolver(
            secret=secret, tenant_claim="secret_claim", tenant_store=_mock_store()
        )
        req = _mock_request(headers={"Authorization": f"Bearer {token}"})
        try:
            await resolver.resolve(req)
        except TenantResolutionError as e:
            # claim name should not appear in the error message
            assert "secret_claim" not in str(e)


# ─────────────────────────── ResolverFactory ─────────────────────────────────


class TestResolverFactory:
    def _cfg(self, **kw):
        from fastapi_tenancy.core.config import TenancyConfig

        defaults = {"database_url": "sqlite+aiosqlite:///:memory:"}
        defaults.update(kw)
        return TenancyConfig(**defaults)

    def test_header_strategy(self):
        from fastapi_tenancy.resolution.factory import ResolverFactory
        from fastapi_tenancy.resolution.header import HeaderTenantResolver

        cfg = self._cfg(resolution_strategy=ResolutionStrategy.HEADER)
        resolver = ResolverFactory.create(ResolutionStrategy.HEADER, cfg, _mock_store())
        assert isinstance(resolver, HeaderTenantResolver)

    def test_subdomain_strategy(self):
        from fastapi_tenancy.resolution.factory import ResolverFactory
        from fastapi_tenancy.resolution.subdomain import SubdomainTenantResolver

        cfg = self._cfg(
            resolution_strategy=ResolutionStrategy.SUBDOMAIN,
            domain_suffix=".example.com",
        )
        resolver = ResolverFactory.create(ResolutionStrategy.SUBDOMAIN, cfg, _mock_store())
        assert isinstance(resolver, SubdomainTenantResolver)

    def test_path_strategy(self):
        from fastapi_tenancy.resolution.factory import ResolverFactory
        from fastapi_tenancy.resolution.path import PathTenantResolver

        cfg = self._cfg(resolution_strategy=ResolutionStrategy.PATH)
        resolver = ResolverFactory.create(ResolutionStrategy.PATH, cfg, _mock_store())
        assert isinstance(resolver, PathTenantResolver)

    def test_custom_strategy_raises(self):
        from fastapi_tenancy.resolution.factory import ResolverFactory

        cfg = self._cfg()
        with pytest.raises(ConfigurationError) as exc:
            ResolverFactory.create(ResolutionStrategy.CUSTOM, cfg, _mock_store())
        assert exc.value.parameter == "resolution_strategy"

    def test_subdomain_without_domain_suffix_raises(self):
        from fastapi_tenancy.resolution.factory import ResolverFactory

        cfg = self._cfg(resolution_strategy=ResolutionStrategy.HEADER)
        # Manually patch domain_suffix to None
        cfg = cfg.model_copy(update={"domain_suffix": None})
        with pytest.raises(ConfigurationError):
            ResolverFactory.create(ResolutionStrategy.SUBDOMAIN, cfg, _mock_store())

    def test_jwt_without_secret_raises(self):
        from fastapi_tenancy.resolution.factory import ResolverFactory

        cfg = self._cfg()
        with pytest.raises(ConfigurationError):
            ResolverFactory.create(ResolutionStrategy.JWT, cfg, _mock_store())
