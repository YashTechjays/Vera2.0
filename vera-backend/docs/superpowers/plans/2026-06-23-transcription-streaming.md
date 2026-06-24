# Live Transcription Streaming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream the live, de-identified call transcript out of the agent worker through a Redis stream, exposed to the Voice Lab UI via an authenticated, RBAC-gated SSE endpoint, with all transcript access going through a reusable `TranscriptService`.

**Architecture:** A shared `vera_core` module defines the event model, the Redis-stream store (transport), and a `TranscriptService` (the reusable produce/consume API). The worker publishes each finalized user/agent turn (tokenized text only) via the service. The control plane exposes `GET /voice-lab/sessions/{room}/transcript` (SSE) that authenticates + authorizes + audits up front, releases its DB session, then `service.consume(room)` replays from `0` and tails. A `fetch`+`ReadableStream` client renders it in Voice Lab. The worker owns stream lifecycle (ended sentinel + grace TTL + rolling backstop TTL).

**Tech Stack:** Python 3.12, `redis.asyncio` (Redis streams: XADD/XREAD), FastAPI `StreamingResponse`, livekit-agents `AgentSession` events, React 19 + `fetch`/`ReadableStream`, pytest + vitest.

**Spec:** `docs/superpowers/specs/2026-06-23-transcription-streaming-design.md`

## Global Constraints

- **PHI:** the stream carries **only** post-redaction (tokenized) text — user turns from `user_input_transcribed` (already redacted by `stt_node`), agent turns from `conversation_item_added` (the LLM's token-only output, pre-`tts_node`). Never publish hydrated raw text. Never log transcript text.
- **Async runtime is `asyncio` only** — stdlib `asyncio.TaskGroup`/`asyncio.timeout`, never `anyio`, never add `anyio` to a `pyproject.toml`.
- **Type params:** PEP 695 (`def f[T]`, `class C[T]`) — ruff rejects `TypeVar`/`Generic[T]`.
- **Line length 100** (ruff). **mypy --strict** must pass.
- **Worker stays DB-less** — it talks to Redis + LiveKit only, never Postgres.
- **Reuse via the service:** all transcript access (publish + consume) goes through `TranscriptService` — callers never touch a `TranscriptStore` or raw Redis directly.
- **CI gate:** `just check` for backend; `npx tsc -b` + `npx eslint .` + `npx vitest run` for frontend. No `Co-Authored-By` in commits.
- **Error contract:** raise `CustomAPIException`/subclasses, never `HTTPException`; PHI-touching responses set `Cache-Control: no-store`.
- Backend commands run from `vera-backend/`; frontend from `vera-frontend/`. Use `uv run <cmd>` for Python.

---

### Task 1: Shared transcript module — event, key, store, service

**Files:**
- Create: `vera-backend/packages/vera_core/src/vera_core/transcript.py`
- Create: `vera-backend/tests/unit/transcript/__init__.py`
- Create: `vera-backend/tests/unit/transcript/test_transcript.py`

**Interfaces:**
- Produces:
  - `transcript_stream_key(room_name: str) -> str` → `f"vera:transcript:{room_name}"`
  - `TranscriptEvent(BaseModel)`: `role: Literal["user","agent"]`, `text: str`, `ts: int`
  - `ROLE_USER: Literal["user"]`, `ROLE_AGENT: Literal["agent"]`
  - `TranscriptStore` (Protocol): `async publish(room_name, event)`, `async mark_ended(room_name)`, `async delete(room_name)`, `read(room_name) -> AsyncIterator[tuple[str, TranscriptEvent]]`
  - `InMemoryTranscriptStore` implementing the Protocol
  - `TranscriptService(store)`: `async publish_turn(room_name, role, text, *, ts)`, `consume(room_name) -> AsyncIterator[tuple[str, TranscriptEvent]]`, `async collect(room_name) -> list[TranscriptEvent]`, `async end(room_name)`, `async clear(room_name)`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/transcript/__init__.py` (empty), then `tests/unit/transcript/test_transcript.py`:

```python
import asyncio

import pytest

from vera_core.transcript import (
    ROLE_AGENT,
    ROLE_USER,
    InMemoryTranscriptStore,
    TranscriptService,
    transcript_stream_key,
)


def test_stream_key_pattern() -> None:
    assert transcript_stream_key("call--t--c") == "vera:transcript:call--t--c"


def _service() -> TranscriptService:
    return TranscriptService(InMemoryTranscriptStore())


@pytest.mark.asyncio
async def test_publish_then_collect_in_order() -> None:
    svc = _service()
    await svc.publish_turn("room", ROLE_USER, "hi", ts=1)
    await svc.publish_turn("room", ROLE_AGENT, "hello", ts=2)
    await svc.end("room")
    got = await svc.collect("room")
    assert [(e.role, e.text) for e in got] == [(ROLE_USER, "hi"), (ROLE_AGENT, "hello")]


@pytest.mark.asyncio
async def test_consume_tails_live_then_ends() -> None:
    svc = _service()
    seen: list[str] = []

    async def consume() -> None:
        async for _id, event in svc.consume("room"):
            seen.append(event.text)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # reader starts and blocks (stream empty)
    await svc.publish_turn("room", ROLE_USER, "live", ts=1)
    await svc.end("room")
    await asyncio.wait_for(task, timeout=1.0)
    assert seen == ["live"]


@pytest.mark.asyncio
async def test_consume_yields_unique_entry_ids() -> None:
    svc = _service()
    await svc.publish_turn("room", ROLE_USER, "a", ts=1)
    await svc.publish_turn("room", ROLE_USER, "b", ts=2)
    await svc.end("room")
    ids = [entry_id async for entry_id, _e in svc.consume("room")]
    assert len(ids) == len(set(ids)) == 2
```

- [ ] **Step 2: Run it, expect failure**

Run: `uv run pytest tests/unit/transcript/test_transcript.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'vera_core.transcript'`.

- [ ] **Step 3: Implement the module**

Create `packages/vera_core/src/vera_core/transcript.py`:

```python
"""Live transcript stream — event model, Redis-stream transport, and the reusable
TranscriptService.

The worker publishes finalized, de-identified turns; consumers (the SSE endpoint today,
the persistence finalizer + analytics later) read them. Everyone goes through
TranscriptService so the consume/publish surface is defined once and no caller touches
raw Redis. The stream carries only tokenized text (the de-identified side of the PHI
wall) — never hydrated raw PHI (see repo CLAUDE.md).
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Literal, Protocol

from pydantic import BaseModel

ROLE_USER: Literal["user"] = "user"
ROLE_AGENT: Literal["agent"] = "agent"

_KEY_PREFIX = "vera:transcript:"
_ENDED_FIELD = "event"
_ENDED_VALUE = "ended"


def transcript_stream_key(room_name: str) -> str:
    """Redis stream key for a room's live transcript (mirrors vera:sess:/vera:perms:)."""
    return f"{_KEY_PREFIX}{room_name}"


class TranscriptEvent(BaseModel):
    """One finalized turn. `text` is always tokenized / de-identified."""

    role: Literal["user", "agent"]
    text: str
    ts: int  # epoch milliseconds


class TranscriptStore(Protocol):
    """Low-level transport. Callers use TranscriptService, not this directly."""

    async def publish(self, room_name: str, event: TranscriptEvent) -> None: ...
    async def mark_ended(self, room_name: str) -> None: ...
    async def delete(self, room_name: str) -> None: ...
    def read(self, room_name: str) -> AsyncIterator[tuple[str, TranscriptEvent]]: ...


class InMemoryTranscriptStore:
    """Reference impl: replay all entries, then tail until the ended sentinel."""

    def __init__(self) -> None:
        self._entries: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._ended: set[str] = set()
        self._seq = 0
        self._cond = asyncio.Condition()

    async def _append(self, key: str, fields: dict[str, str]) -> None:
        async with self._cond:
            self._seq += 1
            self._entries.setdefault(key, []).append((f"{self._seq}-0", fields))
            self._cond.notify_all()

    async def publish(self, room_name: str, event: TranscriptEvent) -> None:
        await self._append(
            transcript_stream_key(room_name),
            {"role": event.role, "text": event.text, "ts": str(event.ts)},
        )

    async def mark_ended(self, room_name: str) -> None:
        key = transcript_stream_key(room_name)
        await self._append(key, {_ENDED_FIELD: _ENDED_VALUE})
        async with self._cond:
            self._ended.add(key)
            self._cond.notify_all()

    async def delete(self, room_name: str) -> None:
        key = transcript_stream_key(room_name)
        async with self._cond:
            self._entries.pop(key, None)
            self._ended.discard(key)
            self._cond.notify_all()

    async def read(self, room_name: str) -> AsyncIterator[tuple[str, TranscriptEvent]]:
        key = transcript_stream_key(room_name)
        idx = 0
        while True:
            async with self._cond:
                entries = self._entries.get(key, [])
                while idx >= len(entries):
                    if key in self._ended:
                        return
                    await self._cond.wait()
                    entries = self._entries.get(key, [])
                entry_id, fields = entries[idx]
                idx += 1
            if fields.get(_ENDED_FIELD) == _ENDED_VALUE:
                return
            yield entry_id, TranscriptEvent(
                role=fields["role"], text=fields["text"], ts=int(fields["ts"])  # type: ignore[arg-type]
            )


class TranscriptService:
    """The reusable produce/consume API over a TranscriptStore. Producers
    (the worker) and consumers (the SSE endpoint today; the finalizer + analytics
    later) all go through this one surface — never raw Redis."""

    def __init__(self, store: TranscriptStore) -> None:
        self._store = store

    async def publish_turn(
        self, room_name: str, role: Literal["user", "agent"], text: str, *, ts: int
    ) -> None:
        await self._store.publish(room_name, TranscriptEvent(role=role, text=text, ts=ts))

    def consume(self, room_name: str) -> AsyncIterator[tuple[str, TranscriptEvent]]:
        """Replay from the start, then tail until the stream ends. The single shared
        consume method — the SSE endpoint frames over it, the finalizer drains it."""
        return self._store.read(room_name)

    async def collect(self, room_name: str) -> list[TranscriptEvent]:
        """Drain an ended stream into a list (finalizer/tests)."""
        return [event async for _id, event in self._store.read(room_name)]

    async def end(self, room_name: str) -> None:
        await self._store.mark_ended(room_name)

    async def clear(self, room_name: str) -> None:
        await self._store.delete(room_name)
```

- [ ] **Step 4: Run tests, expect pass**

Run: `uv run pytest tests/unit/transcript/ -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Lint + types + commit**

```bash
cd vera-backend && uv run ruff format . && uv run ruff check packages/vera_core/src/vera_core/transcript.py tests/unit/transcript/ && uv run mypy
git add packages/vera_core/src/vera_core/transcript.py tests/unit/transcript/
git commit -m "feat(transcript): event model, in-memory store, TranscriptService"
```

---

### Task 2: Redis-backed transcript store

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/transcript.py`
- Create: `vera-backend/tests/integration/transcript/__init__.py`
- Create: `vera-backend/tests/integration/transcript/test_redis_transcript_store.py`

**Interfaces:**
- Consumes: `create_redis(redis_url)` from `vera_core.redis` (`decode_responses=True`).
- Produces: `RedisTranscriptStore(redis: Redis, *, ttl_seconds: int, end_grace_seconds: int)` implementing `TranscriptStore`.

- [ ] **Step 1: Write the failing integration test**

Create `tests/integration/transcript/__init__.py` (empty), then `tests/integration/transcript/test_redis_transcript_store.py`:

```python
import asyncio

import pytest

from vera_core.redis import create_redis
from vera_core.transcript import (
    ROLE_AGENT,
    ROLE_USER,
    RedisTranscriptStore,
    TranscriptService,
    transcript_stream_key,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def svc():
    redis = create_redis("redis://localhost:6379/0")
    await redis.delete(transcript_stream_key("itroom"))
    service = TranscriptService(
        RedisTranscriptStore(redis, ttl_seconds=3600, end_grace_seconds=60)
    )
    yield service, redis
    await redis.delete(transcript_stream_key("itroom"))
    await redis.aclose()


async def test_publish_replay_and_end(svc) -> None:
    service, _redis = svc
    await service.publish_turn("itroom", ROLE_USER, "hi", ts=1)
    await service.publish_turn("itroom", ROLE_AGENT, "hello", ts=2)
    await service.end("itroom")
    got = await service.collect("itroom")
    assert [(e.role, e.text) for e in got] == [(ROLE_USER, "hi"), (ROLE_AGENT, "hello")]


async def test_publish_sets_backstop_ttl(svc) -> None:
    service, redis = svc
    await service.publish_turn("itroom", ROLE_USER, "hi", ts=1)
    ttl = await redis.ttl(transcript_stream_key("itroom"))
    assert 0 < ttl <= 3600


async def test_consume_tails_live(svc) -> None:
    service, _redis = svc
    seen: list[str] = []

    async def consume() -> None:
        async for _id, e in service.consume("itroom"):
            seen.append(e.text)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    await service.publish_turn("itroom", ROLE_USER, "live", ts=1)
    await service.end("itroom")
    await asyncio.wait_for(task, timeout=2.0)
    assert seen == ["live"]
```

- [ ] **Step 2: Run it, expect failure** (requires `just up` for local Redis)

Run: `uv run pytest tests/integration/transcript/ -q`
Expected: FAIL — `ImportError: cannot import name 'RedisTranscriptStore'`.

- [ ] **Step 3: Implement `RedisTranscriptStore`**

Add `from redis.asyncio import Redis` to the imports in `transcript.py`, then append:

```python
class RedisTranscriptStore:
    """Redis Streams transport. XADD on publish (refreshing a rolling backstop TTL so an
    abandoned stream self-clears); `mark_ended` appends the sentinel + a short grace TTL
    so connected readers drain then the key clears. `read` replays from `0` via XREAD
    then tails (BLOCK), stopping on the sentinel or when the key disappears."""

    _BLOCK_MS = 1000

    def __init__(self, redis: Redis, *, ttl_seconds: int, end_grace_seconds: int) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._end_grace_seconds = end_grace_seconds

    async def publish(self, room_name: str, event: TranscriptEvent) -> None:
        key = transcript_stream_key(room_name)
        await self._redis.xadd(key, {"role": event.role, "text": event.text, "ts": str(event.ts)})
        await self._redis.expire(key, self._ttl_seconds)

    async def mark_ended(self, room_name: str) -> None:
        key = transcript_stream_key(room_name)
        await self._redis.xadd(key, {_ENDED_FIELD: _ENDED_VALUE})
        await self._redis.expire(key, self._end_grace_seconds)

    async def delete(self, room_name: str) -> None:
        await self._redis.delete(transcript_stream_key(room_name))

    async def read(self, room_name: str) -> AsyncIterator[tuple[str, TranscriptEvent]]:
        key = transcript_stream_key(room_name)
        last_id = "0"
        while True:
            result = await self._redis.xread({key: last_id}, block=self._BLOCK_MS)
            if not result:
                if not await self._redis.exists(key):
                    return  # stream cleared (grace TTL elapsed / deleted)
                continue
            _stream, entries = result[0]
            for entry_id, fields in entries:
                last_id = entry_id
                if fields.get(_ENDED_FIELD) == _ENDED_VALUE:
                    return
                yield entry_id, TranscriptEvent(
                    role=fields["role"], text=fields["text"], ts=int(fields["ts"])  # type: ignore[arg-type]
                )
```

- [ ] **Step 4: Run tests, expect pass**

Run: `just up && uv run pytest tests/integration/transcript/ -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Lint + types + commit**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy
git add packages/vera_core/src/vera_core/transcript.py tests/integration/transcript/
git commit -m "feat(transcript): Redis Streams store (publish/read/ended/ttl)"
```

---

### Task 3: Settings — stream TTL knobs

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/config/settings.py` (after `redis_url`, ~line 31)
- Modify: `vera-backend/env.example`
- Create: `vera-backend/tests/unit/config/test_transcript_settings.py`

**Interfaces:**
- Produces: `settings.transcript_stream_ttl_seconds: int` (default `3600`), `settings.transcript_end_grace_seconds: int` (default `60`).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/config/test_transcript_settings.py`:

```python
from vera_core.config.settings import Settings


def test_transcript_ttl_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.transcript_stream_ttl_seconds == 3600
    assert s.transcript_end_grace_seconds == 60


def test_transcript_ttl_env_override(monkeypatch) -> None:
    monkeypatch.setenv("VERA_TRANSCRIPT_END_GRACE_SECONDS", "30")
    assert Settings(_env_file=None).transcript_end_grace_seconds == 30
```

- [ ] **Step 2: Run it, expect failure**

Run: `uv run pytest tests/unit/config/test_transcript_settings.py -q`
Expected: FAIL — `AttributeError: ... 'transcript_stream_ttl_seconds'`.

- [ ] **Step 3: Add the settings**

In `settings.py`, immediately after the `redis_url` field:

```python
    # Live-transcript Redis stream lifetime (Voice Lab / SSE). The rolling backstop
    # TTL is refreshed on every publish so an abandoned stream self-clears; the end
    # grace TTL lets connected readers drain the `ended` sentinel before it clears.
    transcript_stream_ttl_seconds: int = 3600  # VERA_TRANSCRIPT_STREAM_TTL_SECONDS
    transcript_end_grace_seconds: int = 60     # VERA_TRANSCRIPT_END_GRACE_SECONDS
```

In `env.example`, near the Redis section:

```
# Live-transcript Redis stream lifetime (Voice Lab live transcription).
# VERA_TRANSCRIPT_STREAM_TTL_SECONDS=3600
# VERA_TRANSCRIPT_END_GRACE_SECONDS=60
```

- [ ] **Step 4: Run tests, expect pass**

Run: `uv run pytest tests/unit/config/test_transcript_settings.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run mypy
git add packages/vera_core/src/vera_core/config/settings.py env.example tests/unit/config/test_transcript_settings.py
git commit -m "feat(config): transcript stream TTL settings"
```

---

### Task 4: Worker transcript publisher (over the service)

**Files:**
- Modify: `vera-backend/apps/agent_worker/pyproject.toml` (dependencies)
- Create: `vera-backend/apps/agent_worker/src/agent_worker/transcript_publisher.py`
- Create: `vera-backend/tests/unit/worker/test_transcript_publisher.py`

**Interfaces:**
- Consumes: `TranscriptService`, `ROLE_USER`, `ROLE_AGENT` (Task 1); livekit `UserInputTranscribedEvent` (`.transcript: str`, `.is_final: bool`), `ConversationItemAddedEvent` (`.item`), `ChatMessage` (`.role: str`, `.text_content: str | None`).
- Produces: `attach_transcript_publisher(session, service: TranscriptService, room_name: str) -> None`; `_publish_user(service, room_name, ev) -> None`; `_publish_agent(service, room_name, ev) -> None` (async, unit-tested directly).

- [ ] **Step 1: Add the redis dependency**

In `apps/agent_worker/pyproject.toml`, add `"redis>=5.2",` to the `dependencies` list (the worker already depends on `vera_core`). Then `cd vera-backend && uv sync`.

- [ ] **Step 2: Write the failing test**

Create `tests/unit/worker/test_transcript_publisher.py`:

```python
import pytest

from agent_worker.transcript_publisher import (
    _publish_agent,
    _publish_user,
    attach_transcript_publisher,
)
from vera_core.transcript import ROLE_AGENT, ROLE_USER, InMemoryTranscriptStore, TranscriptService


class _UserEvent:
    def __init__(self, transcript: str, is_final: bool) -> None:
        self.transcript = transcript
        self.is_final = is_final


class _Item:
    def __init__(self, role: str, text: str) -> None:
        self.role = role
        self.text_content = text


class _ItemEvent:
    def __init__(self, item: _Item) -> None:
        self.item = item


def _service() -> TranscriptService:
    return TranscriptService(InMemoryTranscriptStore())


async def _drain(svc: TranscriptService, room: str) -> list[tuple[str, str]]:
    await svc.end(room)
    return [(e.role, e.text) for e in await svc.collect(room)]


@pytest.mark.asyncio
async def test_publishes_final_user_turn() -> None:
    svc = _service()
    await _publish_user(svc, "room", _UserEvent("my id is [[ID_1]]", is_final=True))
    assert await _drain(svc, "room") == [(ROLE_USER, "my id is [[ID_1]]")]


@pytest.mark.asyncio
async def test_skips_interim_and_empty_user_turns() -> None:
    svc = _service()
    await _publish_user(svc, "room", _UserEvent("partial", is_final=False))
    await _publish_user(svc, "room", _UserEvent("   ", is_final=True))
    assert await _drain(svc, "room") == []


@pytest.mark.asyncio
async def test_publishes_assistant_item_only() -> None:
    svc = _service()
    await _publish_agent(svc, "room", _ItemEvent(_Item("assistant", "hello there")))
    await _publish_agent(svc, "room", _ItemEvent(_Item("user", "ignored echo")))
    assert await _drain(svc, "room") == [(ROLE_AGENT, "hello there")]


def test_attach_registers_both_handlers() -> None:
    registered: list[str] = []

    class _FakeSession:
        def on(self, event: str, cb: object) -> None:
            registered.append(event)

    attach_transcript_publisher(_FakeSession(), _service(), "room")
    assert set(registered) == {"user_input_transcribed", "conversation_item_added"}
```

- [ ] **Step 3: Run it, expect failure**

Run: `uv run pytest tests/unit/worker/test_transcript_publisher.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_worker.transcript_publisher'`.

- [ ] **Step 4: Implement the publisher**

Create `apps/agent_worker/src/agent_worker/transcript_publisher.py`:

```python
"""Publish finalized, de-identified transcript turns via the TranscriptService.

Taps AgentSession events on the de-identified side of the PHI wall: user turns are the
redacted FINAL transcript (post stt_node); agent turns are the LLM's token-only output
(pre tts_node hydration). Best-effort — a Redis failure logs and is swallowed, never
breaking the call.
"""

import asyncio
import logging
import time
from typing import Any

from vera_core.transcript import ROLE_AGENT, ROLE_USER, TranscriptService

logger = logging.getLogger("agent_worker")


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _publish_user(service: TranscriptService, room_name: str, ev: Any) -> None:
    if not ev.is_final:
        return
    text = (ev.transcript or "").strip()
    if not text:
        return
    await service.publish_turn(room_name, ROLE_USER, text, ts=_now_ms())


async def _publish_agent(service: TranscriptService, room_name: str, ev: Any) -> None:
    item = ev.item
    if getattr(item, "role", None) != "assistant":
        return
    text = (getattr(item, "text_content", None) or "").strip()
    if not text:
        return
    await service.publish_turn(room_name, ROLE_AGENT, text, ts=_now_ms())


def _spawn(coro: Any) -> None:
    task = asyncio.create_task(coro)

    def _log_exc(t: "asyncio.Task[None]") -> None:
        exc = t.exception()
        if exc is not None:
            logger.warning("transcript publish failed: %r", exc)

    task.add_done_callback(_log_exc)


def attach_transcript_publisher(
    session: Any, service: TranscriptService, room_name: str
) -> None:
    """Register session handlers that publish finalized user/agent turns via `service`."""

    def _on_user(ev: Any) -> None:
        _spawn(_publish_user(service, room_name, ev))

    def _on_item(ev: Any) -> None:
        _spawn(_publish_agent(service, room_name, ev))

    session.on("user_input_transcribed", _on_user)
    session.on("conversation_item_added", _on_item)
```

- [ ] **Step 5: Run tests, expect pass**

Run: `uv run pytest tests/unit/worker/test_transcript_publisher.py -q`
Expected: PASS (4 tests).

- [ ] **Step 6: Lint + types + commit**

```bash
uv run ruff format . && uv run ruff check apps/agent_worker tests/unit/worker && uv run mypy
git add apps/agent_worker/pyproject.toml apps/agent_worker/src/agent_worker/transcript_publisher.py tests/unit/worker/test_transcript_publisher.py uv.lock
git commit -m "feat(worker): transcript publisher over TranscriptService"
```

---

### Task 5: Wire transcript publishing into the worker entrypoint

**Files:**
- Modify: `vera-backend/apps/agent_worker/src/agent_worker/main.py` (`entrypoint`)

**Interfaces:**
- Consumes: `attach_transcript_publisher` (Task 4), `TranscriptService` + `RedisTranscriptStore` (Tasks 1-2), `create_redis`, `settings.transcript_*` (Task 3).
- Produces: `entrypoint` builds a `TranscriptService` and attaches the publisher when `meta["publish_transcript"]` is set, and `service.end(room_name)` + closes its Redis on shutdown.

This task is integration glue; its publisher logic is unit-tested in Task 4 and its consume path in Task 7. Verification here is **mypy + the full worker unit suite + ruff** (no new unit test — `entrypoint` requires a live LiveKit job to exercise directly).

- [ ] **Step 1: Add imports**

In `main.py`, add with the other imports:

```python
from agent_worker.transcript_publisher import attach_transcript_publisher
from redis.asyncio import Redis
from vera_core.redis import create_redis
from vera_core.transcript import RedisTranscriptStore, TranscriptService
```

- [ ] **Step 2: Build the service + attach the publisher**

In `entrypoint`, after `session = build_session(...)` and BEFORE `session.start(...)`, insert:

```python
    # Live transcript publishing (Voice Lab opt-in via dispatch metadata; /calls unset).
    transcript_redis: Redis | None = None
    transcript_service: TranscriptService | None = None
    if meta.get("publish_transcript"):
        transcript_redis = create_redis(settings.redis_url)
        transcript_service = TranscriptService(
            RedisTranscriptStore(
                transcript_redis,
                ttl_seconds=settings.transcript_stream_ttl_seconds,
                end_grace_seconds=settings.transcript_end_grace_seconds,
            )
        )
        attach_transcript_publisher(session, transcript_service, room_name)
```

`meta` is the already-parsed `json.loads(ctx.job.metadata or "{}")` from the `wait_for_speaker` block earlier in `entrypoint`.

- [ ] **Step 3: Replace the shutdown callback**

Replace the existing `_on_shutdown` (which only closed the boundary) with:

```python
    async def _on_shutdown() -> None:
        if transcript_service is not None:
            try:
                await transcript_service.end(room_name)
            except Exception:  # noqa: BLE001 - best-effort; never block shutdown
                logger.exception("failed to mark transcript ended for %s", room_name)
        if transcript_redis is not None:
            await transcript_redis.aclose()
        await boundary.close_session(session_id)
```

Leave `ctx.add_shutdown_callback(_on_shutdown)` and the existing `session.start(..., room_input_options=...)` call unchanged.

- [ ] **Step 4: Verify**

Run: `uv run ruff format . && uv run ruff check apps/agent_worker && uv run mypy && uv run pytest tests/unit/worker/ -q`
Expected: ruff clean, mypy clean, worker suite green.

- [ ] **Step 5: Commit**

```bash
git add apps/agent_worker/src/agent_worker/main.py
git commit -m "feat(worker): publish transcript when publish_transcript metadata set"
```

---

### Task 6: Control-plane wiring — service injection + dependency

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/main.py` (`create_app` + lifespan)
- Modify: `vera-backend/apps/control_plane/src/control_plane/deps.py`
- Modify: `vera-backend/tests/integration/control_plane/conftest.py`

**Interfaces:**
- Produces: `create_app(..., transcript_service: TranscriptService | None = None)`; `app.state.transcript_service`; `get_transcript_service(request) -> TranscriptService`. The conftest `authz_app` injects `TranscriptService(InMemoryTranscriptStore())` exposed via a `transcript_service` fixture.

- [ ] **Step 1: Add the `create_app` param + lifespan wiring**

In `main.py`, add the import:

```python
from vera_core.transcript import RedisTranscriptStore, TranscriptService
```

Add the parameter to `create_app` (alongside `livekit`):

```python
    transcript_service: TranscriptService | None = None,
```

In the lifespan, after `app.state.livekit = ...`:

```python
        app.state.transcript_service = transcript_service or TranscriptService(
            RedisTranscriptStore(
                _redis(),
                ttl_seconds=settings.transcript_stream_ttl_seconds,
                end_grace_seconds=settings.transcript_end_grace_seconds,
            )
        )
```

- [ ] **Step 2: Add the dependency**

In `deps.py`, add the import (with the other `vera_core` imports) and the accessor (next to `get_livekit`):

```python
from vera_core.transcript import TranscriptService


def get_transcript_service(request: Request) -> TranscriptService:
    service: TranscriptService = request.app.state.transcript_service
    return service
```

- [ ] **Step 3: Inject the in-memory service in tests**

In `tests/integration/control_plane/conftest.py`, add the import and fixture, and pass it to `create_app`:

```python
from vera_core.transcript import InMemoryTranscriptStore, TranscriptService


@pytest.fixture(scope="session")
def transcript_service() -> TranscriptService:
    return TranscriptService(InMemoryTranscriptStore())
```

Add `transcript_service: TranscriptService,` to the `authz_app` fixture parameters and `transcript_service=transcript_service,` to its `create_app(...)` call.

- [ ] **Step 4: Verify nothing regressed**

Run: `just up && uv run pytest tests/integration/control_plane/test_voice_lab.py -q`
Expected: PASS (existing Voice Lab tests still green).

- [ ] **Step 5: Lint + types + commit**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy
git add apps/control_plane/src/control_plane/main.py apps/control_plane/src/control_plane/deps.py tests/integration/control_plane/conftest.py
git commit -m "feat(control-plane): inject TranscriptService + get_transcript_service dep"
```

---

### Task 7: SSE transcript endpoint + publish_transcript metadata

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/voice_lab.py`
- Create: `vera-backend/tests/integration/control_plane/test_transcription.py`

**Interfaces:**
- Consumes: `current_identity`, `get_sessionmaker`, `get_audit`, `get_transcript_service` (deps); `get_resolver`, `PermissionResolver` (rbac); `tenant_session` (`vera_core.db.rls`); `parse_room_name`; `AuditRecord`, `ActorType`, `AuditEvent`; `current_request_id`; `TranscriptService`, `TranscriptEvent`.
- Produces: `GET /api/v1/voice-lab/sessions/{room_name}/transcript` (SSE); `start_voice_session` passes `"publish_transcript": True` in `create_call_room` metadata.

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/control_plane/test_transcription.py`:

```python
import httpx
import pytest
from sqlalchemy import text

from tests.integration.control_plane.conftest import RBACWorld
from vera_core.db import uuid7
from vera_core.observability.correlation import room_name_for_call
from vera_core.transcript import ROLE_AGENT, ROLE_USER, TranscriptService


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_stream_replays_then_ends(
    client: httpx.AsyncClient, rbac_world: RBACWorld, transcript_service: TranscriptService
) -> None:
    room = room_name_for_call(rbac_world.tenant_id, uuid7())
    await transcript_service.publish_turn(room, ROLE_USER, "hi", ts=1)
    await transcript_service.publish_turn(room, ROLE_AGENT, "hello", ts=2)
    await transcript_service.end(room)

    resp = await client.get(
        f"/api/v1/voice-lab/sessions/{room}/transcript", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert '"text":"hi"' in body and '"text":"hello"' in body
    assert body.index("hi") < body.index("hello")


@pytest.mark.asyncio
async def test_stream_requires_auth(client: httpx.AsyncClient, rbac_world: RBACWorld) -> None:
    room = room_name_for_call(rbac_world.tenant_id, uuid7())
    resp = await client.get(f"/api/v1/voice-lab/sessions/{room}/transcript")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_stream_requires_permission(client: httpx.AsyncClient, rbac_world: RBACWorld) -> None:
    room = room_name_for_call(rbac_world.tenant_id, uuid7())
    resp = await client.get(
        f"/api/v1/voice-lab/sessions/{room}/transcript", headers=_auth(rbac_world.norole_token)
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_stream_foreign_tenant_room_404(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    foreign = room_name_for_call(rbac_world.other_tenant_id, uuid7())
    resp = await client.get(
        f"/api/v1/voice-lab/sessions/{foreign}/transcript", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_stream_access_is_audited(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    transcript_service: TranscriptService,
    admin_sessionmaker,
) -> None:
    room = room_name_for_call(rbac_world.tenant_id, uuid7())
    await transcript_service.end(room)
    await client.get(
        f"/api/v1/voice-lab/sessions/{room}/transcript", headers=_auth(rbac_world.admin_token)
    )
    async with admin_sessionmaker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT decision FROM audit_log WHERE event_type='phi.access' "
                    "AND resource_type='transcript' AND resource_id=:r"
                ).bindparams(r=room)
            )
        ).first()
    assert row is not None and row[0] == "allow"
```

(Confirm `admin_sessionmaker` against `conftest.py` — the calls tests use it.)

- [ ] **Step 2: Run them, expect failure**

Run: `just up && uv run pytest tests/integration/control_plane/test_transcription.py -q`
Expected: FAIL — `404` for the stream route (endpoint not defined).

- [ ] **Step 3: Implement the endpoint + metadata flag**

In `voice_lab.py`, extend imports:

```python
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.auth.rbac import PermissionResolver, get_resolver
from control_plane.deps import current_identity, get_audit, get_sessionmaker, get_transcript_service
from control_plane.request_context import current_request_id
from vera_core.audit import AuditRecord, AuditSink
from vera_core.db.rls import tenant_session
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.transcript import TranscriptEvent, TranscriptService
```

`parse_room_name` is already imported (Voice Lab `end_voice_session`); keep one import. `VerifiedIdentity`, `NotFoundError`, `CustomAPIException`, `DefaultExceptionCode` are already imported.

Change the `create_call_room` call in `start_voice_session`:

```python
    await livekit.create_call_room(
        room_name, metadata={"wait_for_speaker": True, "publish_transcript": True}
    )
```

Add at the end of the file:

```python
def _sse_frame(entry_id: str, event: TranscriptEvent) -> str:
    return f"id: {entry_id}\ndata: {event.model_dump_json()}\n\n"


@router.get("/voice-lab/sessions/{room_name}/transcript")
async def stream_transcript(
    room_name: str,
    request: Request,
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    resolver: Annotated[PermissionResolver, Depends(get_resolver)],
    audit: Annotated[AuditSink, Depends(get_audit)],
    service: Annotated[TranscriptService, Depends(get_transcript_service)],
) -> StreamingResponse:
    # Tenant scope without a DB hit: only tenant users; the room name embeds the tenant
    # uuid and must match the caller's (cross-tenant guard, like end_voice_session).
    ref = parse_room_name(room_name)
    if identity.account_type != "tenant" or identity.tenant_id is None or ref is None:
        raise NotFoundError(message="voice session not found")
    tenant_id = identity.tenant_id
    if ref.tenant_id != tenant_id:
        raise NotFoundError(message="voice session not found")

    # Authorize in a SHORT-LIVED tenant session, then release it before streaming — an
    # SSE response is long-lived; we must not hold a DB connection for its duration.
    async with tenant_session(sessionmaker, tenant_id) as session:
        user_id, permissions = await resolver.effective_permissions(
            session, tenant_id, identity.user_id
        )
    allowed = "calls:read" in permissions
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=user_id,
            actor_label=identity.email or identity.subject,
            event_type=AuditEvent.PHI_ACCESS.value,
            resource_type="transcript",
            resource_id=room_name,
            permission_key="calls:read",
            decision="allow" if allowed else "deny",
            request_id=current_request_id(request),
        )
    )
    if not allowed:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="missing permission calls:read"
        )

    async def _events() -> AsyncIterator[str]:
        async for entry_id, event in service.consume(room_name):
            yield _sse_frame(entry_id, event)

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
```

- [ ] **Step 4: Run the new tests + existing Voice Lab tests**

Run: `uv run pytest tests/integration/control_plane/test_transcription.py tests/integration/control_plane/test_voice_lab.py -q`
Expected: PASS (5 transcription + existing Voice Lab). Update `test_browser_session_returns_caller_token_with_wait_metadata` if it asserts the exact dispatch metadata dict — the expected is now `{"wait_for_speaker": True, "publish_transcript": True}`.

- [ ] **Step 5: Lint + types + commit**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy
git add apps/control_plane/src/control_plane/api/v1/voice_lab.py tests/integration/control_plane/test_transcription.py tests/integration/control_plane/test_voice_lab.py
git commit -m "feat(control-plane): authenticated SSE transcript stream endpoint"
```

---

### Task 8: Frontend transcription client

**Files:**
- Modify: `vera-frontend/src/lib/api/client.ts` (export `BASE_URL`)
- Create: `vera-frontend/src/lib/api/transcription.ts`
- Create: `vera-frontend/src/lib/api/transcription.test.ts`

**Interfaces:**
- Consumes: `BASE_URL`, `ApiError` from `@/lib/api/client`; `getToken` from `@/lib/auth/storage`.
- Produces: `type TranscriptEvent = { role: "user" | "agent"; text: string; ts: number }`; `streamTranscription(roomName: string, opts: { signal: AbortSignal; onEvent: (e: TranscriptEvent) => void }): Promise<void>`.

- [ ] **Step 1: Export `BASE_URL`**

In `src/lib/api/client.ts`, change `const BASE_URL = ...` to `export const BASE_URL = ...`.

- [ ] **Step 2: Write the failing test**

Create `src/lib/api/transcription.test.ts`:

```typescript
import { afterEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/auth/storage", () => ({ getToken: () => "tok" }))

import { streamTranscription, type TranscriptEvent } from "./transcription"

function sseStream(frames: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder()
  return new ReadableStream({
    start(controller) {
      for (const f of frames) controller.enqueue(enc.encode(f))
      controller.close()
    },
  })
}

afterEach(() => vi.unstubAllGlobals())

describe("streamTranscription", () => {
  it("parses SSE data frames into events in order", async () => {
    const frames = [
      'id: 1-0\ndata: {"role":"user","text":"hi","ts":1}\n\n',
      'id: 2-0\ndata: {"role":"agent","text":"hello","ts":2}\n\n',
    ]
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(sseStream(frames), {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        }),
      ),
    )
    const seen: TranscriptEvent[] = []
    await streamTranscription("call--t--c", {
      signal: new AbortController().signal,
      onEvent: (e) => seen.push(e),
    })
    expect(seen).toEqual([
      { role: "user", text: "hi", ts: 1 },
      { role: "agent", text: "hello", ts: 2 },
    ])
  })

  it("sends the bearer token and hits the right url", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(sseStream([]), { status: 200 }))
    vi.stubGlobal("fetch", fetchMock)
    await streamTranscription("call--t--c", {
      signal: new AbortController().signal,
      onEvent: () => {},
    })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toContain("/voice-lab/sessions/call--t--c/transcript")
    expect(init.headers.Authorization).toBe("Bearer tok")
  })
})
```

- [ ] **Step 3: Run it, expect failure**

Run (from `vera-frontend/`): `npx vitest run src/lib/api/transcription.test.ts`
Expected: FAIL — cannot resolve `./transcription`.

- [ ] **Step 4: Implement the client**

Create `src/lib/api/transcription.ts`:

```typescript
// Live transcript SSE client. Uses fetch + ReadableStream (not EventSource) so it can
// send the Authorization header; reconnect = re-call (the endpoint replays from the
// start). Parses text/event-stream frames and emits one event per finalized turn.

import { ApiError, BASE_URL } from "@/lib/api/client"
import { getToken } from "@/lib/auth/storage"

export type TranscriptEvent = { role: "user" | "agent"; text: string; ts: number }

export async function streamTranscription(
  roomName: string,
  opts: { signal: AbortSignal; onEvent: (e: TranscriptEvent) => void },
): Promise<void> {
  const res = await fetch(
    `${BASE_URL}/voice-lab/sessions/${encodeURIComponent(roomName)}/transcript`,
    {
      method: "GET",
      headers: { Authorization: `Bearer ${getToken()}`, Accept: "text/event-stream" },
      signal: opts.signal,
    },
  )
  if (!res.ok || !res.body) {
    throw new ApiError(res.status, null, `transcript stream failed (${res.status})`)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const frames = buffer.split("\n\n")
    buffer = frames.pop() ?? ""
    for (const frame of frames) {
      const dataLine = frame.split("\n").find((l) => l.startsWith("data:"))
      if (!dataLine) continue
      const json = dataLine.slice(5).trim()
      if (json) opts.onEvent(JSON.parse(json) as TranscriptEvent)
    }
  }
}
```

- [ ] **Step 5: Run tests, expect pass**

Run: `npx vitest run src/lib/api/transcription.test.ts`
Expected: PASS (2 tests).

- [ ] **Step 6: Typecheck, lint, commit**

```bash
cd vera-frontend && npx tsc -b && npx eslint src/lib/api/transcription.ts src/lib/api/transcription.test.ts src/lib/api/client.ts
git add src/lib/api/transcription.ts src/lib/api/transcription.test.ts src/lib/api/client.ts
git commit -m "feat(frontend): live transcript SSE client"
```

---

### Task 9: Voice Lab transcript panel

**Files:**
- Modify: `vera-frontend/src/pages/VoiceLab.tsx`

**Interfaces:**
- Consumes: `streamTranscription`, `TranscriptEvent` (Task 8); the existing `session.room_name`.
- Produces: a `<TranscriptPanel roomName={...} />` rendered inside `<LiveKitRoom>` next to `<SessionPanel>`.

- [ ] **Step 1: Add the panel component**

In `src/pages/VoiceLab.tsx`, add the import:

```typescript
import { streamTranscription, type TranscriptEvent } from "@/lib/api/transcription"
```

Change `import { useState } from "react"` to `import { useEffect, useState } from "react"`.

Add the component above `export function VoiceLab`:

```tsx
function TranscriptPanel({ roomName }: { roomName: string }) {
  const [turns, setTurns] = useState<TranscriptEvent[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    setTurns([])
    setError(null)
    streamTranscription(roomName, {
      signal: controller.signal,
      onEvent: (e) => setTurns((prev) => [...prev, e]),
    }).catch((err) => {
      if (!controller.signal.aborted) {
        setError(err instanceof Error ? err.message : "Transcript stream failed.")
      }
    })
    return () => controller.abort()
  }, [roomName])

  return (
    <Card>
      <CardHeader>
        <CardTitle>Live transcript</CardTitle>
      </CardHeader>
      <CardContent className="max-h-80 space-y-2 overflow-y-auto text-sm">
        {turns.length === 0 && !error && (
          <p className="text-muted-foreground">Waiting for the conversation…</p>
        )}
        {turns.map((t, i) => (
          <div key={i}>
            <span
              className={
                t.role === "agent"
                  ? "font-medium text-emerald-700"
                  : "font-medium text-foreground"
              }
            >
              {t.role === "agent" ? "Agent" : "Caller"}:
            </span>{" "}
            <span className="text-muted-foreground">{t.text}</span>
          </div>
        ))}
        {error && <p className="text-destructive">{error}</p>}
      </CardContent>
    </Card>
  )
}
```

Render it inside `<LiveKitRoom>`, right after `<SessionPanel ... />`:

```tsx
          <SessionPanel mode={session.mode} onEnd={endSession} />
          <TranscriptPanel roomName={session.room_name} />
          <RoomAudioRenderer />
```

`Card`, `CardHeader`, `CardTitle`, `CardContent` are already imported.

- [ ] **Step 2: Typecheck + lint + build**

Run: `npx tsc -b && npx eslint src/pages/VoiceLab.tsx && npm run build`
Expected: clean (exit 0), build succeeds.

- [ ] **Step 3: Commit**

```bash
git add src/pages/VoiceLab.tsx
git commit -m "feat(frontend): live transcript panel in Voice Lab"
```

---

### Task 10: Full verification

- [ ] **Step 1: Backend gate** — from `vera-backend/` with `just up`: `just check`
  Expected: ruff format + ruff check + mypy clean, pytest green. (Pre-existing `test_calls.py`/`test_seed_form_schemas.py` collisions from leftover DB data are unrelated — reset the test DB if they appear.)

- [ ] **Step 2: Frontend gate** — from `vera-frontend/`: `npx tsc -b && npx eslint . && npx vitest run && npm run build`
  Expected: all clean/green.

- [ ] **Step 3: Run `/simplify`** on the diff (per repo CLAUDE.md), then re-run both gates.

- [ ] **Step 4: Manual smoke (optional, full stack)** — `just api` + `just worker` + LiveKit + frontend: start an in-browser Voice Lab session, speak, confirm the transcript panel fills live; End session and confirm the stream closes.

---

## Notes for the implementer

- **The service is the seam:** every transcript producer/consumer uses `TranscriptService` (`publish_turn` / `consume` / `collect` / `end` / `clear`), never a bare store or raw Redis. The future persistence finalizer (separate spec) will `service.collect(room)` to drain into the `Transcript` table — that's the reuse this layer enables.
- **Why the SSE endpoint doesn't use `require()`:** `require("calls:read")` depends on `tenant_scoped_session`, a `yield`-dependency that stays open for the whole response — fatal for a long-lived SSE. Task 7 replicates auth/authz/audit inline and releases the DB session before streaming. Keep it that way.
- **PHI:** never `console.log`/`logger` transcript text; the stream is tokenized but treat it as sensitive. The endpoint sets `Cache-Control: no-store`.
- **Ordering:** the worker publishes via fire-and-forget tasks; turns are sequential so XADD arrival order matches turn order. If a future change makes turns overlap, switch the publisher to a single-consumer queue.
- **Forward-compat:** don't shorten the grace TTL below what a future finalizer needs to drain.
