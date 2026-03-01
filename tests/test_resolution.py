"""Tests for resolution strategies — header, subdomain, path, JWT."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from fastapi_tenancy.core.exceptions import TenantNotFoundError, TenantResolutionError
from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.resolution.header import HeaderTenantResolver
from fastapi_tenancy.resolution.path import PathTenantResolver
from fastapi_tenancy.resolution.subdomain import SubdomainTenantResolver
from fastapi_tenancy.storage.memory import InMemoryTenantStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant(identifier: str) -> Tenant:
    ts = datetime.now(UTC)
    return Tenant(
        id=f"t-{identifier}",
        identifier=identifier,
        name=f"T {identifier}",
        status=TenantStatus.ACTIVE,
        created_at=ts,
        updated_at=ts,
    )


def _request(headers: dict[str, str] = {}, path: str = "/", host: str = "localhost") -> MagicMock:
    req = MagicMock()
    req.headers = {**headers, "host": host}
    req.url = MagicMock()
    req.url.path = path
    req.state = MagicMock()
    return req


# ---------------------------------------------------------------------------
# HeaderTenantResolver
# ---------------------------------------------------------------------------


class TestHeaderTenantResolver:
    @pytest.fixture
    async def store_with_acme(self) -> InMemoryTenantStore:
        s = InMemoryTenantStore()
        await s.create(_tenant("acme-corp"))
        return s

    async def test_resolves_valid_header(self, store_with_acme: InMemoryTenantStore):
        resolver = HeaderTenantResolver(store_with_acme, header_name="X-Tenant-ID")
        req = _request({"X-Tenant-ID": "acme-corp"})
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"

    async def test_custom_header_name(self, store_with_acme: InMemoryTenantStore):
        resolver = HeaderTenantResolver(store_with_acme, header_name="X-My-Tenant")
        req = _request({"X-My-Tenant": "acme-corp"})
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"

    async def test_missing_header_raises_resolution_error(
        self, store_with_acme: InMemoryTenantStore
    ):
        resolver = HeaderTenantResolver(store_with_acme)
        req = _request({})  # no header
        with pytest.raises(TenantResolutionError) as exc:
            await resolver.resolve(req)
        assert exc.value.strategy == "header"

    async def test_empty_header_value_raises(self, store_with_acme: InMemoryTenantStore):
        resolver = HeaderTenantResolver(store_with_acme)
        req = _request({"X-Tenant-ID": "   "})
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_invalid_identifier_format_raises(self, store_with_acme: InMemoryTenantStore):
        resolver = HeaderTenantResolver(store_with_acme)
        req = _request({"X-Tenant-ID": "INVALID_SLUG!"})
        with pytest.raises(TenantResolutionError) as exc:
            await resolver.resolve(req)
        assert exc.value.strategy == "header"

    async def test_unknown_tenant_raises_not_found(self, store_with_acme: InMemoryTenantStore):
        resolver = HeaderTenantResolver(store_with_acme)
        req = _request({"X-Tenant-ID": "no-such-tenant"})
        with pytest.raises(TenantNotFoundError):
            await resolver.resolve(req)

    async def test_default_header_name(self, store_with_acme: InMemoryTenantStore):
        resolver = HeaderTenantResolver(store_with_acme)
        assert resolver._header_name == "X-Tenant-ID"


# ---------------------------------------------------------------------------
# SubdomainTenantResolver
# ---------------------------------------------------------------------------


class TestSubdomainTenantResolver:
    @pytest.fixture
    async def store_with_acme(self) -> InMemoryTenantStore:
        s = InMemoryTenantStore()
        await s.create(_tenant("acme-corp"))
        return s

    async def test_resolves_subdomain(self, store_with_acme: InMemoryTenantStore):
        resolver = SubdomainTenantResolver(store_with_acme, domain_suffix=".example.com")
        req = _request(host="acme-corp.example.com")
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"

    async def test_resolves_with_port(self, store_with_acme: InMemoryTenantStore):
        resolver = SubdomainTenantResolver(store_with_acme, domain_suffix=".example.com")
        req = _request(host="acme-corp.example.com:8080")
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"

    async def test_missing_host_header_raises(self, store_with_acme: InMemoryTenantStore):
        resolver = SubdomainTenantResolver(store_with_acme, domain_suffix=".example.com")
        req = MagicMock()
        req.headers = {}  # no host
        with pytest.raises(TenantResolutionError) as exc:
            await resolver.resolve(req)
        assert exc.value.strategy == "subdomain"

    async def test_wrong_domain_suffix_raises(self, store_with_acme: InMemoryTenantStore):
        resolver = SubdomainTenantResolver(store_with_acme, domain_suffix=".example.com")
        req = _request(host="acme-corp.other-domain.com")
        with pytest.raises(TenantResolutionError) as exc:
            await resolver.resolve(req)
        assert exc.value.strategy == "subdomain"

    async def test_no_subdomain_raises(self, store_with_acme: InMemoryTenantStore):
        resolver = SubdomainTenantResolver(store_with_acme, domain_suffix=".example.com")
        req = _request(host="example.com")
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_invalid_subdomain_format_raises(self, store_with_acme: InMemoryTenantStore):
        resolver = SubdomainTenantResolver(store_with_acme, domain_suffix=".example.com")
        # "-invalid" starts with a hyphen → fails validate_tenant_identifier → TenantResolutionError
        req = _request(host="-invalid.example.com")
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_x_forwarded_host_trusted_by_default(self, store_with_acme: InMemoryTenantStore):
        resolver = SubdomainTenantResolver(store_with_acme, domain_suffix=".example.com")
        req = _request(
            headers={"x-forwarded-host": "acme-corp.example.com"},
            host="nginx-internal",
        )
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"

    async def test_x_forwarded_host_disabled(self, store_with_acme: InMemoryTenantStore):
        resolver = SubdomainTenantResolver(
            store_with_acme, domain_suffix=".example.com", trust_x_forwarded=False
        )
        req = _request(
            headers={"x-forwarded-host": "acme-corp.example.com"},
            host="acme-corp.example.com",
        )
        # trust_x_forwarded=False → falls back to Host
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"

    async def test_domain_suffix_normalised_without_dot(self, store_with_acme: InMemoryTenantStore):
        resolver = SubdomainTenantResolver(store_with_acme, domain_suffix="example.com")
        req = _request(host="acme-corp.example.com")
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"

    async def test_unknown_tenant_raises_not_found(self, store_with_acme: InMemoryTenantStore):
        resolver = SubdomainTenantResolver(store_with_acme, domain_suffix=".example.com")
        req = _request(host="no-such-tenant.example.com")
        with pytest.raises(TenantNotFoundError):
            await resolver.resolve(req)


# ---------------------------------------------------------------------------
# PathTenantResolver
# ---------------------------------------------------------------------------


class TestPathTenantResolver:
    @pytest.fixture
    async def store_with_acme(self) -> InMemoryTenantStore:
        s = InMemoryTenantStore()
        await s.create(_tenant("acme-corp"))
        return s

    async def test_resolves_from_path(self, store_with_acme: InMemoryTenantStore):
        resolver = PathTenantResolver(store_with_acme, path_prefix="/tenants")
        req = _request(path="/tenants/acme-corp/orders")
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"

    async def test_stores_remainder_on_request_state(self, store_with_acme: InMemoryTenantStore):
        resolver = PathTenantResolver(store_with_acme, path_prefix="/tenants")
        req = _request(path="/tenants/acme-corp/orders/123")
        await resolver.resolve(req)
        assert req.state.tenant_path_remainder == "/orders/123"

    async def test_root_remainder_stored_as_slash(self, store_with_acme: InMemoryTenantStore):
        resolver = PathTenantResolver(store_with_acme, path_prefix="/tenants")
        req = _request(path="/tenants/acme-corp")
        await resolver.resolve(req)
        assert req.state.tenant_path_remainder == "/"

    async def test_wrong_prefix_raises(self, store_with_acme: InMemoryTenantStore):
        resolver = PathTenantResolver(store_with_acme, path_prefix="/tenants")
        req = _request(path="/api/acme-corp/orders")
        with pytest.raises(TenantResolutionError) as exc:
            await resolver.resolve(req)
        assert exc.value.strategy == "path"

    async def test_missing_identifier_after_prefix_raises(
        self, store_with_acme: InMemoryTenantStore
    ):
        resolver = PathTenantResolver(store_with_acme, path_prefix="/tenants")
        req = _request(path="/tenants/")
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_invalid_identifier_format_raises(self, store_with_acme: InMemoryTenantStore):
        resolver = PathTenantResolver(store_with_acme, path_prefix="/tenants")
        req = _request(path="/tenants/INVALID_ID/foo")
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_prefix_normalised_trailing_slash(self, store_with_acme: InMemoryTenantStore):
        resolver = PathTenantResolver(store_with_acme, path_prefix="/tenants/")
        req = _request(path="/tenants/acme-corp/foo")
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"

    async def test_custom_prefix(self, store_with_acme: InMemoryTenantStore):
        resolver = PathTenantResolver(store_with_acme, path_prefix="/api/v1/orgs")
        req = _request(path="/api/v1/orgs/acme-corp/data")
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"

    async def test_unknown_tenant_raises_not_found(self, store_with_acme: InMemoryTenantStore):
        resolver = PathTenantResolver(store_with_acme)
        req = _request(path="/tenants/no-such-tenant/foo")
        with pytest.raises(TenantNotFoundError):
            await resolver.resolve(req)


# ---------------------------------------------------------------------------
# JWTTenantResolver
# ---------------------------------------------------------------------------


class TestJWTTenantResolver:
    @pytest.fixture
    def secret(self) -> str:
        return "a-strong-secret-at-least-32-chars-!!"

    @pytest.fixture
    async def store_with_acme(self) -> InMemoryTenantStore:
        s = InMemoryTenantStore()
        await s.create(_tenant("acme-corp"))
        return s

    def _make_token(self, secret: str, payload: dict) -> str:
        import jwt
        return jwt.encode(payload, secret, algorithm="HS256")

    async def test_resolves_valid_jwt(self, store_with_acme: InMemoryTenantStore, secret: str):
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver
        resolver = JWTTenantResolver(store_with_acme, secret=secret, tenant_claim="tenant_id")
        token = self._make_token(secret, {"tenant_id": "acme-corp"})
        req = _request(headers={"authorization": f"Bearer {token}"})
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"

    async def test_missing_auth_header_raises(
        self, store_with_acme: InMemoryTenantStore, secret: str
    ):
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver
        resolver = JWTTenantResolver(store_with_acme, secret=secret)
        req = _request()
        with pytest.raises(TenantResolutionError) as exc:
            await resolver.resolve(req)
        assert exc.value.strategy == "jwt"

    async def test_non_bearer_scheme_raises(
        self, store_with_acme: InMemoryTenantStore, secret: str
    ):
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver
        resolver = JWTTenantResolver(store_with_acme, secret=secret)
        req = _request(headers={"authorization": "Basic dXNlcjpwYXNz"})
        with pytest.raises(TenantResolutionError) as exc:
            await resolver.resolve(req)
        assert "Bearer" in exc.value.reason

    async def test_empty_bearer_token_raises(
        self, store_with_acme: InMemoryTenantStore, secret: str
    ):
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver
        resolver = JWTTenantResolver(store_with_acme, secret=secret)
        req = _request(headers={"authorization": "Bearer "})
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_expired_token_raises(
        self, store_with_acme: InMemoryTenantStore, secret: str
    ):
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver
        resolver = JWTTenantResolver(store_with_acme, secret=secret)
        exp = datetime.now(UTC) - timedelta(hours=1)
        token = self._make_token(secret, {"tenant_id": "acme-corp", "exp": exp})
        req = _request(headers={"authorization": f"Bearer {token}"})
        with pytest.raises(TenantResolutionError) as exc:
            await resolver.resolve(req)
        assert "expired" in exc.value.reason.lower()

    async def test_invalid_signature_raises(
        self, store_with_acme: InMemoryTenantStore, secret: str
    ):
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver
        resolver = JWTTenantResolver(store_with_acme, secret=secret)
        token = self._make_token("wrong-secret-completely-different!", {"tenant_id": "acme-corp"})
        req = _request(headers={"authorization": f"Bearer {token}"})
        with pytest.raises(TenantResolutionError) as exc:
            await resolver.resolve(req)
        assert exc.value.strategy == "jwt"

    async def test_missing_claim_raises(
        self, store_with_acme: InMemoryTenantStore, secret: str
    ):
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver
        resolver = JWTTenantResolver(store_with_acme, secret=secret, tenant_claim="tenant_id")
        token = self._make_token(secret, {"user_id": "u1"})  # no tenant_id
        req = _request(headers={"authorization": f"Bearer {token}"})
        with pytest.raises(TenantResolutionError) as exc:
            await resolver.resolve(req)
        assert "tenant_id" in exc.value.reason

    async def test_invalid_identifier_in_claim_raises(
        self, store_with_acme: InMemoryTenantStore, secret: str
    ):
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver
        resolver = JWTTenantResolver(store_with_acme, secret=secret)
        token = self._make_token(secret, {"tenant_id": "INVALID!!!"})
        req = _request(headers={"authorization": f"Bearer {token}"})
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(req)

    async def test_unknown_tenant_raises_not_found(
        self, store_with_acme: InMemoryTenantStore, secret: str
    ):
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver
        resolver = JWTTenantResolver(store_with_acme, secret=secret)
        token = self._make_token(secret, {"tenant_id": "no-such-tenant"})
        req = _request(headers={"authorization": f"Bearer {token}"})
        with pytest.raises(TenantNotFoundError):
            await resolver.resolve(req)

    async def test_custom_claim_name(
        self, store_with_acme: InMemoryTenantStore, secret: str
    ):
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver
        resolver = JWTTenantResolver(store_with_acme, secret=secret, tenant_claim="org_id")
        token = self._make_token(secret, {"org_id": "acme-corp"})
        req = _request(headers={"authorization": f"Bearer {token}"})
        tenant = await resolver.resolve(req)
        assert tenant.identifier == "acme-corp"

    def test_import_error_when_pyjwt_missing(
        self, store_with_acme: InMemoryTenantStore, secret: str
    ):
        import sys
        from unittest.mock import patch
        from fastapi_tenancy.resolution.jwt import JWTTenantResolver
        # If PyJWT were missing, __init__ should raise ImportError
        with patch.dict(sys.modules, {"jwt": None}):
            with pytest.raises(ImportError, match="PyJWT"):
                JWTTenantResolver(store_with_acme, secret=secret)
