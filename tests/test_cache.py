"""Tests for fastapi_tenancy.cache.tenant_cache — TenantCache."""

from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime
import time
from unittest.mock import patch

import pytest

from fastapi_tenancy.cache.tenant_cache import TenantCache
from fastapi_tenancy.core.types import Tenant, TenantStatus


def _t(tid: str, identifier: str) -> Tenant:
    ts = datetime.now(UTC)
    return Tenant(
        id=tid,
        identifier=identifier,
        name=f"T{tid}",
        status=TenantStatus.ACTIVE,
        created_at=ts,
        updated_at=ts,
    )


class TestInit:
    def test_default_params(self) -> None:
        c = TenantCache()
        assert c.size() == 0
        s = c.stats()
        assert s["max_size"] == 1000
        assert s["ttl"] == 60
        assert s["hits"] == 0
        assert s["misses"] == 0

    def test_custom_params(self) -> None:
        c = TenantCache(max_size=50, ttl=10)
        assert c.stats()["max_size"] == 50
        assert c.stats()["ttl"] == 10

    def test_invalid_max_size_raises(self) -> None:
        with pytest.raises(ValueError):
            TenantCache(max_size=0)

    def test_invalid_ttl_raises(self) -> None:
        with pytest.raises(ValueError):
            TenantCache(ttl=0)


class TestSetAndGet:
    def test_get_by_id_hit(self) -> None:
        c = TenantCache(ttl=60)
        t = _t("t1", "acme-corp")
        c.set(t)
        result = c.get("t1")
        assert result is t

    def test_get_by_identifier_hit(self) -> None:
        c = TenantCache(ttl=60)
        t = _t("t1", "acme-corp")
        c.set(t)
        result = c.get_by_identifier("acme-corp")
        assert result is t

    def test_get_by_id_miss(self) -> None:
        c = TenantCache(ttl=60)
        assert c.get("nonexistent") is None

    def test_get_by_identifier_miss(self) -> None:
        c = TenantCache(ttl=60)
        assert c.get_by_identifier("nonexistent") is None

    def test_set_updates_existing(self) -> None:
        c = TenantCache(ttl=60)
        t_old = _t("t1", "acme-corp")
        t_new = _t("t1", "acme-corp")
        c.set(t_old)
        c.set(t_new)
        assert c.get("t1") is t_new
        assert c.size() == 1

    def test_set_rotates_identifier_key_on_rename(self) -> None:
        c = TenantCache(ttl=60)
        t_old = _t("t1", "acme-corp")
        c.set(t_old)
        t_new = _t("t1", "acme-new")
        c.set(t_new)
        # Old identifier key should be gone
        assert c.get_by_identifier("acme-corp") is None
        assert c.get_by_identifier("acme-new") is t_new


class TestTTLExpiry:
    def test_expired_entry_is_a_miss(self) -> None:
        c = TenantCache(ttl=1)
        t = _t("t1", "acme-corp")
        # Fake the expiry by manipulating the entry's expires_at
        c.set(t)
        # Patch time.monotonic to simulate future
        with patch("fastapi_tenancy.cache.tenant_cache.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 10
            assert c.get("t1") is None

    def test_non_expired_entry_is_a_hit(self) -> None:
        c = TenantCache(ttl=3600)
        t = _t("t1", "acme-corp")
        c.set(t)
        assert c.get("t1") is t

    def test_expired_entry_evicted_on_access(self) -> None:
        c = TenantCache(ttl=1)
        t = _t("t1", "acme-corp")
        c.set(t)
        with patch("fastapi_tenancy.cache.tenant_cache.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 10
            c.get("t1")
        assert c.size() == 0


class TestLRUEviction:
    def test_lru_entry_evicted_when_full(self) -> None:
        c = TenantCache(max_size=3, ttl=3600)
        t1 = _t("t1", "ten-one")
        t2 = _t("t2", "ten-two")
        t3 = _t("t3", "ten-thr")
        t4 = _t("t4", "ten-fou")
        c.set(t1)
        c.set(t2)
        c.set(t3)
        # Access t1 to make it MRU; LRU is now t2
        c.get("t1")
        # Inserting t4 should evict t2
        c.set(t4)
        assert c.size() == 3
        assert c.get("t2") is None
        assert c.get("t1") is not None
        assert c.get("t4") is not None

    def test_size_never_exceeds_max(self) -> None:
        c = TenantCache(max_size=5, ttl=3600)
        for i in range(20):
            c.set(_t(f"t{i}", f"ten-{i:03d}"))
        assert c.size() <= 5

    def test_evict_lru_on_empty_cache_does_nothing(self) -> None:
        c = TenantCache(max_size=100, ttl=60)
        assert c.size() == 0
        c._evict_lru()
        assert c.size() == 0
        assert c._by_id == OrderedDict()
        assert c._id_by_ident == {}


class TestInvalidation:
    def test_invalidate_by_id(self) -> None:
        c = TenantCache(ttl=3600)
        t = _t("t1", "acme-corp")
        c.set(t)
        removed = c.invalidate("t1")
        assert removed is True
        assert c.get("t1") is None
        assert c.get_by_identifier("acme-corp") is None

    def test_invalidate_nonexistent_returns_false(self) -> None:
        c = TenantCache(ttl=3600)
        assert c.invalidate("ghost") is False

    def test_invalidate_by_identifier(self) -> None:
        c = TenantCache(ttl=3600)
        t = _t("t1", "acme-corp")
        c.set(t)
        removed = c.invalidate_by_identifier("acme-corp")
        assert removed is True
        assert c.get("t1") is None

    def test_invalidate_by_identifier_miss(self) -> None:
        c = TenantCache(ttl=3600)
        assert c.invalidate_by_identifier("ghost") is False

    def test_clear_returns_count(self) -> None:
        c = TenantCache(ttl=3600)
        for i in range(5):
            c.set(_t(f"t{i}", f"ten-{i:03d}"))
        count = c.clear()
        assert count == 5
        assert c.size() == 0


class TestStats:
    def test_hit_rate_zero_with_no_lookups(self) -> None:
        c = TenantCache()
        assert c.stats()["hit_rate_pct"] == 0

    def test_hit_rate_100_all_hits(self) -> None:
        c = TenantCache(ttl=3600)
        t = _t("t1", "acme-corp")
        c.set(t)
        c.get("t1")
        c.get("t1")
        c.get("t1")
        assert c.stats()["hit_rate_pct"] == 100

    def test_hit_rate_50(self) -> None:
        c = TenantCache(ttl=3600)
        t = _t("t1", "acme-corp")
        c.set(t)
        c.get("t1")  # hit
        c.get("ghost")  # miss
        s = c.stats()
        assert s["hit_rate_pct"] == 50

    def test_misses_incremented(self) -> None:
        c = TenantCache(ttl=3600)
        c.get("miss1")
        c.get("miss2")
        assert c.stats()["misses"] == 2


class TestPurgeExpired:
    def test_purge_removes_stale_entries(self) -> None:
        c = TenantCache(ttl=1)
        for i in range(5):
            c.set(_t(f"t{i}", f"ten-{i:03d}"))
        with patch("fastapi_tenancy.cache.tenant_cache.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 100
            evicted = c.purge_expired()
        assert evicted == 5
        assert c.size() == 0

    def test_purge_keeps_fresh_entries(self) -> None:
        c = TenantCache(ttl=3600)
        c.set(_t("t1", "fresh"))
        evicted = c.purge_expired()
        assert evicted == 0
        assert c.size() == 1
