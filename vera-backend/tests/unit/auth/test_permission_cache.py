from control_plane.auth.permission_cache import InMemoryPermissionCache
from vera_core.db import uuid7

PERMS = frozenset({"calls:read", "phi:detokenize"})


async def test_get_miss_returns_none() -> None:
    cache = InMemoryPermissionCache()
    assert await cache.get(uuid7(), "sub") is None


async def test_set_then_get() -> None:
    cache = InMemoryPermissionCache(ttl_seconds=60)
    tenant = uuid7()
    await cache.set(tenant, "sub", PERMS)
    assert await cache.get(tenant, "sub") == PERMS


async def test_entries_expire_after_ttl() -> None:
    cache = InMemoryPermissionCache(ttl_seconds=0.0)  # expires immediately
    tenant = uuid7()
    await cache.set(tenant, "sub", PERMS)
    assert await cache.get(tenant, "sub") is None


async def test_explicit_invalidation() -> None:
    cache = InMemoryPermissionCache(ttl_seconds=60)
    tenant = uuid7()
    await cache.set(tenant, "sub", PERMS)
    await cache.invalidate(tenant, "sub")
    assert await cache.get(tenant, "sub") is None


async def test_keys_are_tenant_scoped() -> None:
    cache = InMemoryPermissionCache(ttl_seconds=60)
    tenant_a, tenant_b = uuid7(), uuid7()
    await cache.set(tenant_a, "sub", PERMS)
    assert await cache.get(tenant_b, "sub") is None
