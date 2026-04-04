"""Tests for all tenant resolution strategies."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import time
from typing import Any

import jwt as pyjwt
import pytest
from starlette.requests import Request

from fastapi_tenancy.core.exceptions import TenantNotFoundError, TenantResolutionError
from fastapi_tenancy.core.types import Tenant, TenantStatus
from fastapi_tenancy.resolution.base import BaseTenantResolver
from fastapi_tenancy.resolution.header import HeaderTenantResolver
from fastapi_tenancy.resolution.jwt import JWTTenantResolver
from fastapi_tenancy.resolution.path import PathTenantResolver
from fastapi_tenancy.resolution.subdomain import SubdomainTenantResolver
from fastapi_tenancy.storage.memory import InMemoryTenantStore


def _now() -> datetime:
    return datetime.now(UTC)


def _make_tenant(identifier: str, status: TenantStatus = TenantStatus.ACTIVE) -> Tenant:
    return Tenant(
        id=f"t-{identifier}",
        identifier=identifier,
        name=identifier.title(),
        status=status,
        created_at=_now(),
        updated_at=_now(),
    )


@pytest.fixture
async def store() -> InMemoryTenantStore:
    """Seeded store with three active tenants."""
    s = InMemoryTenantStore()
    for slug in ("acme-corp", "widgets-inc", "gadgets-co"):
        await s.create(_make_tenant(slug))
    return s


def _make_request(
    path: str = "/",
    headers: dict[str, str] | None = None,
    host: str = "example.com",
) -> Request:
    """Build a minimal Starlette Request without an actual transport."""
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    # If host not already in headers, add it
    if not any(k == b"host" for k, _ in raw_headers):
        raw_headers.append((b"host", host.encode()))

    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": raw_headers,
        "server": (host.split(":", maxsplit=1)[0], 443),
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    return Request(scope, receive)


_JWT_SECRET = "a-secret-that-is-at-least-32-chars!!"


class TestHeaderTenantResolver:
    async def test_resolves_from_default_header(self, store: InMemoryTenantStore) -> None:
        resolver = HeaderTenantResolver(store)
        request = _make_request(headers={"X-Tenant-ID": "acme-corp"})
        tenant = await resolver.resolve(request)
        assert tenant.identifier == "acme-corp"

    async def test_resolves_from_custom_header(self, store: InMemoryTenantStore) -> None:
        resolver = HeaderTenantResolver(store, header_name="X-Organization-ID")
        request = _make_request(headers={"X-Organization-ID": "widgets-inc"})
        tenant = await resolver.resolve(request)
        assert tenant.identifier == "widgets-inc"

    async def test_missing_header_raises_resolution_error(self, store: InMemoryTenantStore) -> None:
        resolver = HeaderTenantResolver(store)
        request = _make_request(headers={})
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert exc_info.value.reason == "Tenant not found"

    async def test_empty_header_raises_resolution_error(self, store: InMemoryTenantStore) -> None:
        resolver = HeaderTenantResolver(store)
        request = _make_request(headers={"X-Tenant-ID": ""})
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert exc_info.value.reason == "Tenant not found"

    async def test_whitespace_only_header_raises(self, store: InMemoryTenantStore) -> None:
        resolver = HeaderTenantResolver(store)
        request = _make_request(headers={"X-Tenant-ID": "   "})
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(request)

    async def test_invalid_identifier_raises_resolution_error(
        self, store: InMemoryTenantStore
    ) -> None:
        resolver = HeaderTenantResolver(store)
        # Contains uppercase → fails validate_tenant_identifier
        request = _make_request(headers={"X-Tenant-ID": "INVALID!!!"})
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert exc_info.value.reason == "Tenant not found"

    async def test_unknown_tenant_raises_resolution_error(self, store: InMemoryTenantStore) -> None:
        resolver = HeaderTenantResolver(store)
        request = _make_request(headers={"X-Tenant-ID": "nonexistent-co"})
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert exc_info.value.reason == "Tenant not found"

    async def test_case_insensitive_header_name(self, store: InMemoryTenantStore) -> None:
        resolver = HeaderTenantResolver(store)
        # HTTP headers are case-insensitive; Starlette normalises them
        request = _make_request(headers={"x-tenant-id": "acme-corp"})
        tenant = await resolver.resolve(request)
        assert tenant.identifier == "acme-corp"

    def test_stores_store_reference(self, store: InMemoryTenantStore) -> None:
        resolver = HeaderTenantResolver(store)
        assert resolver.store is store

    def test_strategy_attribute_on_resolution_error(self, store: InMemoryTenantStore) -> None:
        resolver = HeaderTenantResolver(store)
        request = _make_request(headers={})
        with pytest.raises(TenantResolutionError) as exc_info:
            asyncio.get_event_loop().run_until_complete(resolver.resolve(request))
        assert exc_info.value.strategy == "header"


class TestSubdomainTenantResolver:
    def test_init_adds_dot_to_suffix_without_leading_dot(self, store: InMemoryTenantStore) -> None:
        resolver = SubdomainTenantResolver(store, domain_suffix="example.com")
        assert resolver._domain_suffix == ".example.com"

    def test_init_preserves_suffix_with_leading_dot(self, store: InMemoryTenantStore) -> None:
        resolver = SubdomainTenantResolver(store, domain_suffix=".example.com")
        assert resolver._domain_suffix == ".example.com"

    def test_init_empty_suffix_stays_empty(self, store: InMemoryTenantStore) -> None:
        resolver = SubdomainTenantResolver(store, domain_suffix="")
        assert resolver._domain_suffix == ""

    async def test_resolves_from_subdomain(self, store: InMemoryTenantStore) -> None:
        resolver = SubdomainTenantResolver(store, domain_suffix=".example.com")
        request = _make_request(host="acme-corp.example.com")
        tenant = await resolver.resolve(request)
        assert tenant.identifier == "acme-corp"

    async def test_strips_port_from_host(self, store: InMemoryTenantStore) -> None:
        resolver = SubdomainTenantResolver(store, domain_suffix=".example.com")
        request = _make_request(host="acme-corp.example.com:8443")
        tenant = await resolver.resolve(request)
        assert tenant.identifier == "acme-corp"

    async def test_host_without_suffix_raises(self, store: InMemoryTenantStore) -> None:
        resolver = SubdomainTenantResolver(store, domain_suffix=".example.com")
        request = _make_request(host="acme-corp.different.com")
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert "does not end with" in exc_info.value.reason.lower()

    async def test_single_label_host_raises(self, store: InMemoryTenantStore) -> None:
        """A bare hostname with no dot has no subdomain."""
        resolver = SubdomainTenantResolver(store, domain_suffix="")
        request = _make_request(host="localhost")
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert "no subdomain" in exc_info.value.reason.lower()

    async def test_invalid_subdomain_identifier_raises(self, store: InMemoryTenantStore) -> None:
        resolver = SubdomainTenantResolver(store, domain_suffix=".example.com")
        # Use "1a" which is too short (2 chars) and starts with a digit
        # - invalid even after lowercasing
        request = _make_request(host="1a.example.com")
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(request)

    async def test_no_matching_tenant_raises_not_found(self, store: InMemoryTenantStore) -> None:
        resolver = SubdomainTenantResolver(store, domain_suffix=".example.com")
        request = _make_request(host="no-such-tenant.example.com")
        with pytest.raises(TenantNotFoundError):
            await resolver.resolve(request)

    async def test_reads_x_forwarded_host_when_trusted(self, store: InMemoryTenantStore) -> None:
        resolver = SubdomainTenantResolver(
            store, domain_suffix=".example.com", trust_x_forwarded=True
        )
        request = _make_request(
            host="real-host.example.com",
            headers={"X-Forwarded-Host": "acme-corp.example.com"},
        )
        tenant = await resolver.resolve(request)
        assert tenant.identifier == "acme-corp"

    async def test_ignores_x_forwarded_host_when_untrusted(
        self, store: InMemoryTenantStore
    ) -> None:
        resolver = SubdomainTenantResolver(
            store, domain_suffix=".example.com", trust_x_forwarded=False
        )
        # X-Forwarded-Host has a valid tenant but trust_x_forwarded=False
        # → falls back to Host header which also has a valid tenant
        request = _make_request(
            host="widgets-inc.example.com",
            headers={"X-Forwarded-Host": "acme-corp.example.com"},
        )
        tenant = await resolver.resolve(request)
        # Should use Host, not X-Forwarded-Host
        assert tenant.identifier == "widgets-inc"

    async def test_no_host_header_raises(self, store: InMemoryTenantStore) -> None:
        resolver = SubdomainTenantResolver(store, domain_suffix=".example.com")
        scope: dict[str, Any] = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": [],  # no Host, no X-Forwarded-Host
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        request = Request(scope, receive)
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert "host" in exc_info.value.reason.lower()

    async def test_falls_back_to_host_when_forwarded_empty(
        self, store: InMemoryTenantStore
    ) -> None:
        """When X-Forwarded-Host is present but empty, Host is used."""
        resolver = SubdomainTenantResolver(
            store, domain_suffix=".example.com", trust_x_forwarded=True
        )
        request = _make_request(
            host="gadgets-co.example.com",
            headers={"X-Forwarded-Host": ""},
        )
        tenant = await resolver.resolve(request)
        assert tenant.identifier == "gadgets-co"


class TestPathTenantResolver:
    def test_init_normalises_prefix(self, store: InMemoryTenantStore) -> None:
        resolver = PathTenantResolver(store, path_prefix="tenants/")
        assert resolver._prefix == "/tenants"

    def test_init_default_prefix(self, store: InMemoryTenantStore) -> None:
        resolver = PathTenantResolver(store)
        assert resolver._prefix == "/tenants"

    async def test_resolves_valid_path(self, store: InMemoryTenantStore) -> None:
        resolver = PathTenantResolver(store, path_prefix="/tenants")
        request = _make_request(path="/tenants/acme-corp/orders")
        tenant = await resolver.resolve(request)
        assert tenant.identifier == "acme-corp"

    async def test_custom_prefix(self, store: InMemoryTenantStore) -> None:
        resolver = PathTenantResolver(store, path_prefix="/orgs")
        request = _make_request(path="/orgs/widgets-inc/projects")
        tenant = await resolver.resolve(request)
        assert tenant.identifier == "widgets-inc"

    async def test_path_not_matching_prefix_raises(self, store: InMemoryTenantStore) -> None:
        resolver = PathTenantResolver(store, path_prefix="/tenants")
        request = _make_request(path="/api/users")
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert "does not start with" in exc_info.value.reason.lower()

    async def test_empty_identifier_after_prefix_raises(self, store: InMemoryTenantStore) -> None:
        resolver = PathTenantResolver(store, path_prefix="/tenants")
        request = _make_request(path="/tenants/")
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert "identifier" in exc_info.value.reason.lower()

    async def test_invalid_identifier_raises(self, store: InMemoryTenantStore) -> None:
        resolver = PathTenantResolver(store, path_prefix="/tenants")
        request = _make_request(path="/tenants/INVALID_SLUG/resource")
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert "valid tenant identifier" in exc_info.value.reason.lower()

    async def test_unknown_tenant_raises_not_found(self, store: InMemoryTenantStore) -> None:
        resolver = PathTenantResolver(store, path_prefix="/tenants")
        request = _make_request(path="/tenants/nonexistent-co/resource")
        with pytest.raises(TenantNotFoundError):
            await resolver.resolve(request)

    async def test_sets_path_remainder_on_state(self, store: InMemoryTenantStore) -> None:
        resolver = PathTenantResolver(store, path_prefix="/tenants")
        request = _make_request(path="/tenants/acme-corp/orders/123")
        await resolver.resolve(request)
        assert request.state.tenant_path_remainder == "/orders/123"

    async def test_remainder_defaults_to_slash_when_no_suffix(
        self, store: InMemoryTenantStore
    ) -> None:
        resolver = PathTenantResolver(store, path_prefix="/tenants")
        request = _make_request(path="/tenants/acme-corp")
        await resolver.resolve(request)
        assert request.state.tenant_path_remainder == "/"

    def test_strategy_on_resolution_error(self, store: InMemoryTenantStore) -> None:
        resolver = PathTenantResolver(store)
        request = _make_request(path="/wrong")
        with pytest.raises(TenantResolutionError) as exc_info:
            asyncio.get_event_loop().run_until_complete(resolver.resolve(request))
        assert exc_info.value.strategy == "path"


class TestJWTTenantResolver:
    def test_init_stores_secret(self, store: InMemoryTenantStore) -> None:
        resolver = JWTTenantResolver(store, secret=_JWT_SECRET)
        assert resolver._secret == _JWT_SECRET

    def test_init_default_algorithm_hs256(self, store: InMemoryTenantStore) -> None:
        resolver = JWTTenantResolver(store, secret=_JWT_SECRET)
        assert resolver._algorithm == "HS256"

    def test_init_custom_algorithm(self, store: InMemoryTenantStore) -> None:
        resolver = JWTTenantResolver(store, secret=_JWT_SECRET, algorithm="HS512")
        assert resolver._algorithm == "HS512"

    def test_init_default_claim_is_tenant_id(self, store: InMemoryTenantStore) -> None:
        resolver = JWTTenantResolver(store, secret=_JWT_SECRET)
        assert resolver._tenant_claim == "tenant_id"

    def test_init_custom_claim(self, store: InMemoryTenantStore) -> None:
        resolver = JWTTenantResolver(store, secret=_JWT_SECRET, tenant_claim="org")
        assert resolver._tenant_claim == "org"

    def test_decode_token_valid(self, store: InMemoryTenantStore) -> None:
        resolver = JWTTenantResolver(store, secret=_JWT_SECRET)
        token = pyjwt.encode({"tenant_id": "acme-corp"}, _JWT_SECRET, algorithm="HS256")
        payload = resolver._decode_token(token)
        assert payload["tenant_id"] == "acme-corp"

    def test_decode_token_expired_raises(self, store: InMemoryTenantStore) -> None:
        resolver = JWTTenantResolver(store, secret=_JWT_SECRET)
        # Create token with exp in the past
        token = pyjwt.encode(
            {"tenant_id": "acme-corp", "exp": int(time.time()) - 3600},
            _JWT_SECRET,
            algorithm="HS256",
        )
        with pytest.raises(TenantResolutionError) as exc_info:
            resolver._decode_token(token)
        assert "expired" in exc_info.value.reason.lower()

    def test_decode_token_invalid_signature_raises(self, store: InMemoryTenantStore) -> None:
        resolver = JWTTenantResolver(store, secret=_JWT_SECRET)
        with pytest.raises(TenantResolutionError) as exc_info:
            resolver._decode_token("not.a.jwt")
        assert "invalid" in exc_info.value.reason.lower()

    async def test_resolve_no_authorization_header(self, store: InMemoryTenantStore) -> None:
        resolver = JWTTenantResolver(store, secret=_JWT_SECRET)
        request = _make_request(headers={})
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert "authorization header is missing" in exc_info.value.reason.lower()

    async def test_resolve_non_bearer_scheme(self, store: InMemoryTenantStore) -> None:
        resolver = JWTTenantResolver(store, secret=_JWT_SECRET)
        request = _make_request(headers={"Authorization": "Basic dXNlcjpwYXNz"})
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert "bearer" in exc_info.value.reason.lower()

    async def test_resolve_empty_bearer_token(self, store: InMemoryTenantStore) -> None:
        resolver = JWTTenantResolver(store, secret=_JWT_SECRET)
        request = _make_request(headers={"Authorization": "Bearer "})
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert "empty" in exc_info.value.reason.lower()

    async def test_resolve_missing_tenant_claim(self, store: InMemoryTenantStore) -> None:
        resolver = JWTTenantResolver(store, secret=_JWT_SECRET)
        token = pyjwt.encode({"user_id": "u123"}, _JWT_SECRET, algorithm="HS256")
        request = _make_request(headers={"Authorization": f"Bearer {token}"})  # type: ignore[str-bytes-safe]
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert "tenant_id" in exc_info.value.reason

    async def test_resolve_claim_not_string(self, store: InMemoryTenantStore) -> None:
        """If the claim is an integer rather than a string, resolution must fail."""
        resolver = JWTTenantResolver(store, secret=_JWT_SECRET)
        token = pyjwt.encode({"tenant_id": 42}, _JWT_SECRET, algorithm="HS256")
        request = _make_request(headers={"Authorization": f"Bearer {token}"})  # type: ignore[str-bytes-safe]
        with pytest.raises(TenantResolutionError):
            await resolver.resolve(request)

    async def test_resolve_invalid_identifier_slug(self, store: InMemoryTenantStore) -> None:
        resolver = JWTTenantResolver(store, secret=_JWT_SECRET)
        token = pyjwt.encode({"tenant_id": "UPPER-CASE!!"}, _JWT_SECRET, algorithm="HS256")
        request = _make_request(headers={"Authorization": f"Bearer {token}"})  # type: ignore[str-bytes-safe]
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert "invalid" in exc_info.value.reason.lower()

    async def test_resolve_valid_jwt(self, store: InMemoryTenantStore) -> None:
        resolver = JWTTenantResolver(store, secret=_JWT_SECRET)
        token = pyjwt.encode(
            {"tenant_id": "acme-corp", "user_id": "u123"},
            _JWT_SECRET,
            algorithm="HS256",
        )
        request = _make_request(headers={"Authorization": f"Bearer {token}"})  # type: ignore[str-bytes-safe]
        tenant = await resolver.resolve(request)
        assert tenant.identifier == "acme-corp"

    async def test_resolve_unknown_tenant_raises_not_found(
        self, store: InMemoryTenantStore
    ) -> None:
        resolver = JWTTenantResolver(store, secret=_JWT_SECRET)
        token = pyjwt.encode({"tenant_id": "no-such-tenant"}, _JWT_SECRET, algorithm="HS256")
        request = _make_request(headers={"Authorization": f"Bearer {token}"})  # type: ignore[str-bytes-safe]
        with pytest.raises(TenantNotFoundError):
            await resolver.resolve(request)

    async def test_resolve_custom_claim(self, store: InMemoryTenantStore) -> None:
        resolver = JWTTenantResolver(store, secret=_JWT_SECRET, tenant_claim="org")
        token = pyjwt.encode({"org": "widgets-inc"}, _JWT_SECRET, algorithm="HS256")
        request = _make_request(headers={"Authorization": f"Bearer {token}"})  # type: ignore[str-bytes-safe]
        tenant = await resolver.resolve(request)
        assert tenant.identifier == "widgets-inc"

    def test_strategy_on_resolution_error(self, store: InMemoryTenantStore) -> None:
        resolver = JWTTenantResolver(store, secret=_JWT_SECRET)
        request = _make_request(headers={})
        with pytest.raises(TenantResolutionError) as exc_info:
            asyncio.get_event_loop().run_until_complete(resolver.resolve(request))
        assert exc_info.value.strategy == "jwt"


class TestBaseTenantResolver:
    def test_cannot_instantiate_directly(self, store: InMemoryTenantStore) -> None:
        """BaseTenantResolver is abstract — instantiation must raise TypeError."""
        with pytest.raises(TypeError):
            BaseTenantResolver(store)  # type: ignore[abstract]

    def test_concrete_subclass_stores_reference(self, store: InMemoryTenantStore) -> None:
        class Concrete(BaseTenantResolver):
            async def resolve(self, request: Any) -> Tenant:
                return _make_tenant("dummy")

        resolver = Concrete(store)
        assert resolver.store is store


class TestHeaderResolverEnumeration:
    """FIX: Missing header, invalid format, and unknown tenant all return the same reason."""

    def _make_request(self, headers: dict[str, str]) -> Any:
        from starlette.requests import Request  # noqa: PLC0415

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "query_string": b"",
        }
        return Request(scope)

    async def test_missing_header_raises_resolution_error(self) -> None:
        store = InMemoryTenantStore()
        resolver = HeaderTenantResolver(store)
        request = _make_request(headers={})
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert exc_info.value.reason == "Tenant not found"

    async def test_invalid_identifier_raises_resolution_error(self) -> None:
        store = InMemoryTenantStore()
        resolver = HeaderTenantResolver(store)
        request = _make_request(headers={"X-Tenant-ID": "INVALID TENANT!!!"})
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert exc_info.value.reason == "Tenant not found"

    async def test_unknown_tenant_raises_resolution_error(self) -> None:
        store = InMemoryTenantStore()
        resolver = HeaderTenantResolver(store)
        request = _make_request(headers={"X-Tenant-ID": "valid-but-unknown"})
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert exc_info.value.reason == "Tenant not found"

    async def test_all_failure_modes_indistinguishable(self) -> None:
        """Missing header, invalid format and unknown tenant all produce the same reason."""
        store = InMemoryTenantStore()
        resolver = HeaderTenantResolver(store)

        reasons = []
        for headers in [
            {},
            {"X-Tenant-ID": ""},
            {"X-Tenant-ID": "BAD IDENTIFIER!!!"},
            {"X-Tenant-ID": "unknown-tenant-xyz"},
        ]:
            request = _make_request(headers=headers)
            with pytest.raises(TenantResolutionError) as exc_info:
                await resolver.resolve(request)
            reasons.append(exc_info.value.reason)

        assert len(set(reasons)) == 1, f"Expected all same reason, got: {reasons}"

    async def test_valid_tenant_resolves_successfully(self) -> None:
        store = InMemoryTenantStore()
        tenant = _make_tenant(identifier="known-tenant")
        await store.create(tenant)
        resolver = HeaderTenantResolver(store)
        request = _make_request(headers={"X-Tenant-ID": "known-tenant"})
        resolved = await resolver.resolve(request)
        assert resolved.identifier == "known-tenant"


class TestJWTAudienceValidation:
    """FIX: JWTTenantResolver must validate the 'aud' claim when
    audience= is configured, preventing cross-service token replay."""

    @pytest.fixture
    async def seeded_store(self) -> InMemoryTenantStore:
        s = InMemoryTenantStore()
        await s.create(_make_tenant("aud-tenant"))
        return s

    def _token(
        self,
        identifier: str = "aud-tenant",
        audience: str | None = None,
        secret: str = _JWT_SECRET,
    ) -> str:
        payload: dict[str, str] = {"tenant_id": identifier}
        if audience is not None:
            payload["aud"] = audience
        return str(pyjwt.encode(payload, secret, algorithm="HS256"))

    async def test_valid_audience_resolves_tenant(self, seeded_store: InMemoryTenantStore) -> None:
        """Token with matching aud claim must resolve successfully."""
        resolver = JWTTenantResolver(
            seeded_store,
            secret=_JWT_SECRET,
            audience="my-api",
        )
        token = self._token(audience="my-api")
        request = _make_request(headers={"Authorization": f"Bearer {token}"})
        tenant = await resolver.resolve(request)
        assert tenant.identifier == "aud-tenant"

    async def test_wrong_audience_raises_resolution_error(
        self, seeded_store: InMemoryTenantStore
    ) -> None:
        """Token with wrong aud claim must raise TenantResolutionError."""
        resolver = JWTTenantResolver(
            seeded_store,
            secret=_JWT_SECRET,
            audience="service-a",
        )
        # Token claims audience "service-b" — different service.
        token = self._token(audience="service-b")
        request = _make_request(headers={"Authorization": f"Bearer {token}"})
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert "audience" in exc_info.value.reason.lower()
        assert exc_info.value.strategy == "jwt"

    async def test_missing_audience_claim_raises_when_audience_configured(
        self, seeded_store: InMemoryTenantStore
    ) -> None:
        """Token without aud claim must be rejected when audience= is set."""
        resolver = JWTTenantResolver(
            seeded_store,
            secret=_JWT_SECRET,
            audience="expected-audience",
        )
        # Token has no aud claim at all.
        token = self._token(audience=None)
        request = _make_request(headers={"Authorization": f"Bearer {token}"})
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert exc_info.value.strategy == "jwt"

    async def test_no_audience_configured_still_resolves(
        self, seeded_store: InMemoryTenantStore
    ) -> None:
        """When audience=None (default), tokens without aud still resolve."""
        resolver = JWTTenantResolver(
            seeded_store,
            secret=_JWT_SECRET,
            audience=None,
        )
        # Token has no aud claim — should work fine with no audience check.
        token = self._token(audience=None)
        request = _make_request(headers={"Authorization": f"Bearer {token}"})
        tenant = await resolver.resolve(request)
        assert tenant.identifier == "aud-tenant"

    async def test_audience_mismatch_details_include_expected(
        self, seeded_store: InMemoryTenantStore
    ) -> None:
        """details dict on audience error must contain expected_audience."""
        resolver = JWTTenantResolver(
            seeded_store,
            secret=_JWT_SECRET,
            audience="correct-service",
        )
        token = self._token(audience="wrong-service")
        request = _make_request(headers={"Authorization": f"Bearer {token}"})
        with pytest.raises(TenantResolutionError) as exc_info:
            await resolver.resolve(request)
        assert exc_info.value.details.get("expected_audience") == "correct-service"

    def test_no_audience_emits_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Constructing JWTTenantResolver without audience= must emit a WARNING."""
        import logging  # noqa: PLC0415

        store = InMemoryTenantStore()
        with caplog.at_level(logging.WARNING, logger="fastapi_tenancy.resolution.jwt"):
            JWTTenantResolver(store, secret=_JWT_SECRET, audience=None)

        assert any("audience" in r.message.lower() for r in caplog.records), (
            "Expected warning about missing audience configuration"
        )

    def test_with_audience_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Configuring audience= must suppress the replay-risk warning."""
        import logging  # noqa: PLC0415

        store = InMemoryTenantStore()
        with caplog.at_level(logging.WARNING, logger="fastapi_tenancy.resolution.jwt"):
            JWTTenantResolver(store, secret=_JWT_SECRET, audience="my-service")

        audience_warnings = [r for r in caplog.records if "audience" in r.message.lower()]
        assert not audience_warnings, "No warning expected when audience is configured"

    def test_audience_stored_on_resolver(self) -> None:
        """resolver._audience must reflect the constructor argument."""
        store = InMemoryTenantStore()
        resolver = JWTTenantResolver(store, secret=_JWT_SECRET, audience="svc-x")
        assert resolver._audience == "svc-x"

    def test_none_audience_stored(self) -> None:
        store = InMemoryTenantStore()
        resolver = JWTTenantResolver(store, secret=_JWT_SECRET, audience=None)
        assert resolver._audience is None
