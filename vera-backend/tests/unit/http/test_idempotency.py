"""Idempotency-Key in-flight lock and the header dependency (API Contract §7 /
ADR vera2-database-design §707).

Redis is only a short concurrent-duplicate lock; durable de-dup of late retries is
a UNIQUE constraint on the resource, not tested here.
"""

import asyncio

import httpx
import pytest
from fastapi import Depends, FastAPI

from control_plane.exceptions import (
    CustomAPIException,
    DefaultExceptionCode,
    ExceptionCode,
    register_exception_handlers,
)
from control_plane.idempotency import (
    InMemoryIdempotencyStore,
    claim_or_conflict,
    idempotency_guard,
    require_idempotency_key,
)
from vera_core.db import uuid7

_TTL = 30


async def test_first_request_claims_then_concurrent_retry_is_locked() -> None:
    store = InMemoryIdempotencyStore()
    tenant, user, key = uuid7(), uuid7(), "idem-1"

    assert await store.claim(tenant, user, key, _TTL) is True
    # A second request while the first is in flight cannot claim the lock.
    assert await store.claim(tenant, user, key, _TTL) is False


async def test_expired_lock_is_reclaimable() -> None:
    store = InMemoryIdempotencyStore()
    tenant, user, key = uuid7(), uuid7(), "idem-2"
    # ttl=0 expires immediately, so the lock is re-claimable (it is the request horizon).
    assert await store.claim(tenant, user, key, 0) is True
    assert await store.claim(tenant, user, key, 0) is True


async def test_locks_are_isolated_per_tenant() -> None:
    store = InMemoryIdempotencyStore()
    user, key = uuid7(), "shared-key"
    tenant_a, tenant_b = uuid7(), uuid7()

    assert await store.claim(tenant_a, user, key, _TTL) is True
    # The same key under a different tenant is a different lock.
    assert await store.claim(tenant_b, user, key, _TTL) is True


async def test_locks_are_isolated_per_user() -> None:
    store = InMemoryIdempotencyStore()
    tenant, key = uuid7(), "shared-key"
    user_a, user_b = uuid7(), uuid7()

    assert await store.claim(tenant, user_a, key, _TTL) is True
    # The same key string from a different user within the same tenant is a different lock.
    assert await store.claim(tenant, user_b, key, _TTL) is True


async def test_claim_or_conflict_raises_on_contention() -> None:
    store = InMemoryIdempotencyStore()
    tenant, user, key = uuid7(), uuid7(), "idem-3"

    await claim_or_conflict(store, tenant, user, key, _TTL)  # first wins, no raise
    with pytest.raises(CustomAPIException) as exc:
        await claim_or_conflict(store, tenant, user, key, _TTL)
    assert exc.value.code is ExceptionCode.IDEMPOTENCY_CONFLICT


async def test_guard_releases_the_lock_when_the_body_raises() -> None:
    """A rejected request created nothing, so its key stays immediately reusable."""
    store = InMemoryIdempotencyStore()
    tenant, user, key = uuid7(), uuid7(), "idem-4"

    with pytest.raises(CustomAPIException):
        async with idempotency_guard(store, tenant, user, key, _TTL):
            raise CustomAPIException(DefaultExceptionCode.VALIDATION_ERROR)

    assert await store.claim(tenant, user, key, _TTL) is True


async def test_guard_holds_the_lock_when_the_body_succeeds() -> None:
    """The success path is what the lock is for — a duplicate retry still 409s."""
    store = InMemoryIdempotencyStore()
    tenant, user, key = uuid7(), uuid7(), "idem-5"

    async with idempotency_guard(store, tenant, user, key, _TTL):
        pass

    assert await store.claim(tenant, user, key, _TTL) is False


async def test_guard_releases_on_cancellation() -> None:
    """Cancellation (client disconnect) created nothing either — hence the
    `BaseException` catch, which `Exception` would miss."""
    store = InMemoryIdempotencyStore()
    tenant, user, key = uuid7(), uuid7(), "idem-6"

    with pytest.raises(asyncio.CancelledError):
        async with idempotency_guard(store, tenant, user, key, _TTL):
            raise asyncio.CancelledError

    assert await store.claim(tenant, user, key, _TTL) is True


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.post("/ingest")
    async def ingest(key: str = Depends(require_idempotency_key)) -> dict[str, str]:
        return {"key": key}

    register_exception_handlers(app)
    return app


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_build_app())
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_missing_idempotency_key_header_is_400(client: httpx.AsyncClient) -> None:
    async with client:
        resp = await client.post("/ingest")
    assert resp.status_code == 400
    assert resp.json()["error_code"] == "MISSING_IDEMPOTENCY_KEY"


async def test_idempotency_key_header_is_returned(client: httpx.AsyncClient) -> None:
    async with client:
        resp = await client.post("/ingest", headers={"Idempotency-Key": "abc-123"})
    assert resp.status_code == 200
    assert resp.json()["key"] == "abc-123"
