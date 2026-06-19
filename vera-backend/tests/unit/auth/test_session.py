"""Unit tests for the two-key session model: a `sess` key (idle TTL, slid by
keepalive) plus a `sess_abs` companion key (absolute cap, never extended). Every
extension is capped at the abs key's remaining TTL, so the session can never
outlive the cap."""

import json
from uuid import uuid4

import pytest

from control_plane.auth.session import (
    SESSION_ABS_NS,
    SESSION_NS,
    InMemorySessionStore,
    SessionData,
    _key,
)


def _data() -> SessionData:
    return SessionData(
        user_id=uuid4(),
        tenant_id=uuid4(),
        email="u@example.com",
        subject="u@example.com",
        provider_type="password",
        mfa_passed=True,
        account_type="tenant",
    )


async def test_mint_session_sets_both_keys() -> None:
    store = InMemorySessionStore()
    token = await store.mint_session(_data(), idle_ttl=10, abs_ttl=100)
    assert _key(SESSION_NS, token) in store._entries
    assert _key(SESSION_ABS_NS, token) in store._entries
    assert (await store.get(SESSION_NS, token)) is not None


async def test_extend_session_returns_idle_when_below_cap() -> None:
    store = InMemorySessionStore()
    token = await store.mint_session(_data(), idle_ttl=10, abs_ttl=1000)
    remaining = await store.extend_session(token, idle_ttl=10)
    assert remaining is not None
    assert 9 <= remaining <= 10


async def test_extend_session_caps_at_absolute_remaining() -> None:
    store = InMemorySessionStore()
    token = await store.mint_session(_data(), idle_ttl=1000, abs_ttl=50)
    remaining = await store.extend_session(token, idle_ttl=1000)
    assert remaining is not None
    assert remaining < 1000  # capped below the idle window
    assert 49 <= remaining <= 50  # ~= absolute remaining


async def test_extend_session_none_when_absolute_expired() -> None:
    store = InMemorySessionStore()
    token = await store.mint_session(_data(), idle_ttl=10, abs_ttl=100)
    store._entries.pop(_key(SESSION_ABS_NS, token))  # simulate the abs key reaped
    assert (await store.extend_session(token, idle_ttl=10)) is None


async def test_delete_session_removes_both_keys() -> None:
    store = InMemorySessionStore()
    token = await store.mint_session(_data(), idle_ttl=10, abs_ttl=100)
    await store.delete_session(token)
    assert _key(SESSION_NS, token) not in store._entries
    assert _key(SESSION_ABS_NS, token) not in store._entries
    assert (await store.extend_session(token, idle_ttl=10)) is None


def test_session_data_from_json_raises_on_missing_account_type() -> None:
    """from_json must treat account_type as a REQUIRED field — no default fallback.
    A legacy Redis payload without the key raises KeyError immediately."""
    payload = json.dumps(
        {
            "user_id": str(uuid4()),
            "tenant_id": None,
            "email": "u@example.com",
            "subject": "u@example.com",
            "provider_type": "password",
            "mfa_passed": True,
            # account_type intentionally omitted
        }
    )
    with pytest.raises(KeyError):
        SessionData.from_json(payload)


async def test_get_expires_after_idle_window_without_keepalive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No keepalive: the `sess` key lapses once the idle window passes — a
    deterministic stand-in for real-time idle auto-logout (clock monkeypatched, no
    sleeps)."""
    clock: dict[str, float] = {"now": 1000.0}
    monkeypatch.setattr("control_plane.auth.session.time.monotonic", lambda: clock["now"])
    store = InMemorySessionStore()
    token = await store.mint_session(_data(), idle_ttl=10, abs_ttl=100)

    clock["now"] += 9  # still inside the 10s idle window
    assert (await store.get(SESSION_NS, token)) is not None

    clock["now"] += 2  # total +11 > idle window, and no keepalive slid it
    assert (await store.get(SESSION_NS, token)) is None


async def test_keepalive_slides_idle_but_absolute_cap_still_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keepalive slides the idle window so the session outlives the bare idle TTL,
    but the absolute cap is never extended: once it passes, extend_session refuses
    (None) and the session is gone — even under continuous keepalive."""
    clock: dict[str, float] = {"now": 1000.0}
    monkeypatch.setattr("control_plane.auth.session.time.monotonic", lambda: clock["now"])
    store = InMemorySessionStore()
    token = await store.mint_session(_data(), idle_ttl=3, abs_ttl=5)

    clock["now"] += 2  # t+2: keepalive within both windows → full idle, under cap
    assert (await store.extend_session(token, idle_ttl=3)) == 3

    clock["now"] += 2  # t+4: past the bare 3s idle-from-mint, but kept alive
    assert (await store.get(SESSION_NS, token)) is not None  # sliding worked
    # absolute remaining is now 1s, so the slide is capped below the idle window:
    assert (await store.extend_session(token, idle_ttl=3)) == 1

    clock["now"] += 2  # t+6: past the 5s absolute cap
    assert (await store.extend_session(token, idle_ttl=3)) is None  # cap refuses to extend
    assert (await store.get(SESSION_NS, token)) is None  # and the session is gone
