"""Unit tests for the Redis-backed session store and permission cache, run
against fakeredis (no live Redis needed)."""

from typing import cast
from uuid import UUID

import fakeredis.aioredis
import pytest
from redis.asyncio import Redis

from control_plane.auth.permission_cache import RedisPermissionCache
from control_plane.auth.session import (
    MFA_NS,
    SESSION_ABS_NS,
    SESSION_NS,
    RedisSessionStore,
    SessionData,
    _key,
)

TENANT = UUID("00000000-0000-0000-0000-0000000000aa")
USER = UUID("00000000-0000-0000-0000-0000000000cc")


@pytest.fixture
def redis() -> Redis:
    return cast(Redis, fakeredis.aioredis.FakeRedis(decode_responses=True))


def _data() -> SessionData:
    return SessionData(
        user_id=USER,
        tenant_id=TENANT,
        email="a@example.com",
        subject="a@example.com",
        provider_type="password",
        mfa_passed=True,
        account_type="tenant",
        tenant_slug="acme",
    )


async def test_session_store_put_get_roundtrip(redis: Redis) -> None:
    store = RedisSessionStore(redis)
    data = _data()  # each SessionData carries its own session_id — compare the same one
    token = await store.put(SESSION_NS, data, 60)
    assert await store.get(SESSION_NS, token) == data


async def test_session_store_delete(redis: Redis) -> None:
    store = RedisSessionStore(redis)
    token = await store.put(SESSION_NS, _data(), 60)
    await store.delete(SESSION_NS, token)
    assert await store.get(SESSION_NS, token) is None


async def test_session_store_namespaces_isolated(redis: Redis) -> None:
    store = RedisSessionStore(redis)
    token = await store.put(SESSION_NS, _data(), 60)
    assert await store.get(MFA_NS, token) is None


async def test_session_store_miss_returns_none(redis: Redis) -> None:
    store = RedisSessionStore(redis)
    assert await store.get(SESSION_NS, "absent") is None


async def test_mint_session_sets_both_keys(redis: Redis) -> None:
    store = RedisSessionStore(redis)
    data = _data()
    token = await store.mint_session(data, idle_ttl=10, abs_ttl=100)
    assert await store.get(SESSION_NS, token) == data
    assert await redis.ttl(_key(SESSION_NS, token)) > 0
    assert await redis.ttl(_key(SESSION_ABS_NS, token)) > 0


async def test_extend_session_returns_idle_when_below_cap(redis: Redis) -> None:
    store = RedisSessionStore(redis)
    token = await store.mint_session(_data(), idle_ttl=10, abs_ttl=1000)
    remaining = await store.extend_session(token, idle_ttl=10)
    assert remaining is not None
    assert 9 <= remaining <= 10


async def test_extend_session_caps_at_absolute_remaining(redis: Redis) -> None:
    store = RedisSessionStore(redis)
    token = await store.mint_session(_data(), idle_ttl=1000, abs_ttl=50)
    remaining = await store.extend_session(token, idle_ttl=1000)
    assert remaining is not None
    assert remaining < 1000
    assert 49 <= remaining <= 50


async def test_extend_session_none_when_absolute_expired(redis: Redis) -> None:
    store = RedisSessionStore(redis)
    token = await store.mint_session(_data(), idle_ttl=10, abs_ttl=100)
    await redis.delete(_key(SESSION_ABS_NS, token))  # absolute cap key gone
    assert await store.extend_session(token, idle_ttl=10) is None


async def test_extend_session_none_when_sess_gone(redis: Redis) -> None:
    store = RedisSessionStore(redis)
    token = await store.mint_session(_data(), idle_ttl=10, abs_ttl=100)
    await redis.delete(_key(SESSION_NS, token))  # sess idle-expired, abs still present
    assert await store.extend_session(token, idle_ttl=10) is None


async def test_delete_session_removes_both_keys(redis: Redis) -> None:
    store = RedisSessionStore(redis)
    token = await store.mint_session(_data(), idle_ttl=10, abs_ttl=100)
    await store.delete_session(token)
    assert await store.get(SESSION_NS, token) is None
    assert await redis.ttl(_key(SESSION_ABS_NS, token)) < 0  # -2 = no key


async def test_permission_cache_set_get(redis: Redis) -> None:
    cache = RedisPermissionCache(redis, ttl_seconds=60)
    perms = frozenset({"calls:read", "phi:detokenize"})
    await cache.set(TENANT, "subj", perms)
    assert await cache.get(TENANT, "subj") == perms


async def test_permission_cache_miss_returns_none(redis: Redis) -> None:
    assert await RedisPermissionCache(redis).get(TENANT, "subj") is None


async def test_permission_cache_invalidate(redis: Redis) -> None:
    cache = RedisPermissionCache(redis, ttl_seconds=60)
    await cache.set(TENANT, "subj", frozenset({"calls:read"}))
    await cache.invalidate(TENANT, "subj")
    assert await cache.get(TENANT, "subj") is None


async def test_permission_cache_is_tenant_scoped(redis: Redis) -> None:
    cache = RedisPermissionCache(redis, ttl_seconds=60)
    other = UUID("00000000-0000-0000-0000-0000000000bb")
    await cache.set(TENANT, "subj", frozenset({"calls:read"}))
    assert await cache.get(other, "subj") is None
