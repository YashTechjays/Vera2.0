# Filled Form Eval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the post-call pipeline that re-reads a finished call's transcript, extracts every collectable field with per-field confidence + evidence, scores each with a judge pass, persists the results, and advances the form to a terminal state.

**Architecture:** The call-end callback snapshots the form, flips it to `AI_PROCESSING`, and `XADD`s a job to a Redis stream, returning immediately. A new control-plane background consumer (mirroring `worker_events.py`) drains the job, runs a pure orchestration service (`vera_core.services.post_call_eval`) that calls an injected `LLMClient` (concrete Vertex/Gemini impl in the control plane), writes `field_answer(source=ai_call)` + `field_evaluation` + the `call_form_snapshot.after_state`, decides `COMPLETED` vs `EXCEPTION_REVIEW`, and fires the dispatcher.

**Tech Stack:** Python 3.12, asyncio, FastAPI, SQLAlchemy async, `redis.asyncio` Streams, Vertex AI Gemini Flash via `google-genai`, pytest / pytest-asyncio, ruff, mypy --strict.

## Global Constraints

- **PHI never reaches the LLM raw.** The pipeline consumes the transcript **as stored** (already de-identified during the call). It adds no tokenizer/vault/re-identification of its own. Do NOT send raw PHI to Gemini. (`vera-backend/CLAUDE.md` prime directive; ⛔ PreToolUse hook.)
- **Audit records carry field names/counts only, never values.** Every PHI read/write emits an `AuditRecord`.
- **Timestamps come from the DB clock** (`func.now()` / model mixins), never `datetime.now()`.
- **All DB work runs inside a tenant-scoped session** (`tenant_session(sessionmaker, tenant_id)`; RLS `SET LOCAL app.tenant_id`).
- **Migrations must be idempotent** (`ADD COLUMN IF NOT EXISTS`; constraint `DO $$ … duplicate_object … $$`) — but **no schema changes are expected** (all tables exist). Only touch `migrations/` if a column is truly required.
- **PEP 695 type params** (`class Foo[T]`, `def f[T]`); ruff rejects `Generic[T]`/`TypeVar`. **asyncio only** — never `import anyio`; use `asyncio.TaskGroup`/`asyncio.timeout`.
- **`redis.asyncio` BLOCK reads RAISE `TimeoutError`** on an idle window — catch `TimeoutError as RedisTimeoutError` and treat as a normal idle tick (copy `worker_events.py::_read_once`).
- **Endpoints** return `ResponseModel[T]` via `ok(...)`; raise `CustomAPIException`, never `HTTPException`.
- **Verification gate:** `just check` (ruff + mypy --strict + pytest) green → run `/simplify` on the change → re-run `just check` → then done/commit. A change that adds a long-lived background loop MUST also be **boot-verified**: `just up`, run the consumer, watch it idle two windows.
- **Model default:** Gemini **Flash** for both passes.
- **Commit style:** conventional commits; end the body with the repo's `Co-Authored-By:` / `Claude-Session:` trailers.

---

## File structure

| File | Responsibility |
|---|---|
| `packages/vera_core/src/vera_core/services/form_state_machine.py` (modify) | Allow `AI_PROCESSING → EXCEPTION_REVIEW`. |
| `packages/vera_core/src/vera_core/events/post_call.py` (create) | `PostCallJob` model, stream/group constants, `PostCallJobBus` (emit + ensure_group). |
| `packages/vera_core/src/vera_core/events/__init__.py` (modify) | Re-export the new symbols. |
| `packages/vera_core/src/vera_core/transcript.py` (modify) | Add `TranscriptService.snapshot()` + `*Store.snapshot()` (non-blocking XRANGE read). |
| `packages/vera_core/src/vera_core/integrations/llm.py` (create) | Pure `LLMClient` Protocol + request/response dataclasses + `FakeLLMClient`. No google dep. |
| `packages/vera_core/src/vera_core/services/post_call_eval.py` (create) | Pure orchestration `evaluate_call(...)`: extract → persist → judge → snapshot → status. Depends only on the `LLMClient` Protocol. |
| `apps/control_plane/src/control_plane/llm.py` (create) | `VertexLLMClient(LLMClient)` using `google-genai`. |
| `apps/control_plane/src/control_plane/post_call.py` (create) | `PostCallConsumer` — the Redis-stream consumer loop. |
| `apps/control_plane/src/control_plane/api/v1/calls.py` (modify) | Callback: on `COMPLETED`, snapshot `before_state`, transition `IN_CALL → AI_PROCESSING`, `XADD` job. |
| `apps/control_plane/src/control_plane/main.py` (modify) | Boot/stop the consumer in lifespan. |
| `packages/vera_core/src/vera_core/config/settings.py` (modify) | Add Gemini/Vertex + post-call settings. |
| `apps/control_plane/pyproject.toml` (modify) | Add `google-genai`. |
| `apps/agent_worker/.../main.py` (modify, **deferred Task 11**) | Seed intake PHI + stash `field_path→token` map (blocked — see Task 11). |

**Tables (all exist, no DDL):** `field_answer`, `field_evaluation`, `call_form_snapshot` (`packages/vera_core/src/vera_core/models/field_answer.py`).

---

## Task 1: Allow `AI_PROCESSING → EXCEPTION_REVIEW` in the state machine

**Files:**
- Modify: `packages/vera_core/src/vera_core/services/form_state_machine.py:26`
- Test: `packages/vera_core/tests/unit/test_form_state_machine.py` (add cases; create if absent)

**Interfaces:**
- Consumes: `FormStateMachine.transition(form, target, *, tenant_max_retries)` (existing).
- Produces: legal edge `AI_PROCESSING → EXCEPTION_REVIEW` (and existing `→ COMPLETED`, `→ CALL_FAILED`).

- [ ] **Step 1: Write the failing test**

```python
# packages/vera_core/tests/unit/test_form_state_machine.py
import types
import pytest
from vera_core.models.enums import FormStatus
from vera_core.services.form_state_machine import FormStateMachine, InvalidTransitionError


def _form(status: FormStatus):
    return types.SimpleNamespace(status=status.value, retry_count=0, enqueued_at=None)


def test_ai_processing_can_go_to_exception_review():
    form = _form(FormStatus.AI_PROCESSING)
    FormStateMachine().transition(form, FormStatus.EXCEPTION_REVIEW, tenant_max_retries=5)
    assert form.status == FormStatus.EXCEPTION_REVIEW.value


def test_ai_processing_can_still_complete():
    form = _form(FormStatus.AI_PROCESSING)
    FormStateMachine().transition(form, FormStatus.COMPLETED, tenant_max_retries=5)
    assert form.status == FormStatus.COMPLETED.value


def test_ready_cannot_jump_to_exception_review_via_ai_processing():
    form = _form(FormStatus.IN_QUEUE)
    with pytest.raises(InvalidTransitionError):
        FormStateMachine().transition(form, FormStatus.EXCEPTION_REVIEW, tenant_max_retries=5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest packages/vera_core/tests/unit/test_form_state_machine.py::test_ai_processing_can_go_to_exception_review -v`
Expected: FAIL with `InvalidTransitionError` (edge not allowed yet).

- [ ] **Step 3: Add the edge**

In `form_state_machine.py`, change the `AI_PROCESSING` entry:

```python
    FormStatus.AI_PROCESSING: frozenset(
        {FormStatus.COMPLETED, FormStatus.CALL_FAILED, FormStatus.EXCEPTION_REVIEW}
    ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest packages/vera_core/tests/unit/test_form_state_machine.py -v`
Expected: PASS (all three).

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/services/form_state_machine.py packages/vera_core/tests/unit/test_form_state_machine.py
git commit -m "feat(forms): allow AI_PROCESSING -> EXCEPTION_REVIEW transition"
```

---

## Task 2: `PostCallJob` event + `PostCallJobBus`

**Files:**
- Create: `packages/vera_core/src/vera_core/events/post_call.py`
- Modify: `packages/vera_core/src/vera_core/events/__init__.py`
- Test: `packages/vera_core/tests/unit/test_post_call_event.py`

**Interfaces:**
- Produces:
  - `POST_CALL_STREAM = "vera:post-call"`, `POST_CALL_GROUP = "post-call"`.
  - `class PostCallJob(BaseModel)`: `tenant_id: UUID`, `form_id: UUID`, `call_id: UUID`.
  - `parse_post_call_job(raw: str) -> PostCallJob`.
  - `class PostCallJobBus`: `__init__(self, redis, *, maxlen=10_000)`, `async emit(job) -> None`, `async ensure_group() -> None`.

- [ ] **Step 1: Write the failing test**

```python
# packages/vera_core/tests/unit/test_post_call_event.py
from uuid import uuid4
from vera_core.events.post_call import PostCallJob, parse_post_call_job


def test_post_call_job_roundtrips_json():
    job = PostCallJob(tenant_id=uuid4(), form_id=uuid4(), call_id=uuid4())
    restored = parse_post_call_job(job.model_dump_json())
    assert restored == job
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest packages/vera_core/tests/unit/test_post_call_event.py -v`
Expected: FAIL with `ModuleNotFoundError: vera_core.events.post_call`.

- [ ] **Step 3: Create the module** (mirrors `events/worker.py`)

```python
# packages/vera_core/src/vera_core/events/post_call.py
"""Control-plane-internal post-call eval job over Redis Streams + a consumer group.

The call-end callback enqueues one job per completed call; the post-call consumer
drains it and runs the LLM re-read. PHI-free by construction: only tenant/form/call
UUIDs — never transcript text or identifiers.
"""

from uuid import UUID

from pydantic import BaseModel, TypeAdapter
from redis.asyncio import Redis
from redis.exceptions import ResponseError

POST_CALL_STREAM = "vera:post-call"
POST_CALL_GROUP = "post-call"
_JOB_FIELD = "job"


class PostCallJob(BaseModel):
    """A completed call awaiting the post-call re-read."""

    tenant_id: UUID
    form_id: UUID
    call_id: UUID


_ADAPTER: TypeAdapter[PostCallJob] = TypeAdapter(PostCallJob)


def parse_post_call_job(raw: str) -> PostCallJob:
    """Deserialize a stream payload; raises on invalid."""
    return _ADAPTER.validate_json(raw)


class PostCallJobBus:
    """XADD publish side (callback) + consumer-group bootstrap. One stream, one group."""

    def __init__(self, redis: Redis, *, maxlen: int = 10_000) -> None:
        self._redis = redis
        self._maxlen = maxlen

    async def emit(self, job: PostCallJob) -> None:
        await self._redis.xadd(
            POST_CALL_STREAM,
            {_JOB_FIELD: job.model_dump_json()},
            maxlen=self._maxlen,
            approximate=True,
        )

    async def ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(
                POST_CALL_STREAM, POST_CALL_GROUP, id="0", mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
```

- [ ] **Step 4: Re-export in the package `__init__`**

Add to `packages/vera_core/src/vera_core/events/__init__.py` (follow the existing `from .worker import (...)` block and `__all__`):

```python
from .post_call import (
    POST_CALL_GROUP,
    POST_CALL_STREAM,
    PostCallJob,
    PostCallJobBus,
    parse_post_call_job,
)
```
and add those five names to `__all__`.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd vera-backend && uv run pytest packages/vera_core/tests/unit/test_post_call_event.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/events/post_call.py packages/vera_core/src/vera_core/events/__init__.py packages/vera_core/tests/unit/test_post_call_event.py
git commit -m "feat(events): PostCallJob event + PostCallJobBus over Redis Streams"
```

---

## Task 3: `TranscriptService.snapshot()` — non-blocking durable read

The pipeline needs a **one-shot, non-blocking** read of the finished call's transcript (the existing `read()`/`collect()` tail/block until the ended sentinel — wrong for a post-call drain that must return even if the sentinel is missing). `snapshot()` XRANGEs all current entries once and returns them in order.

**Files:**
- Modify: `packages/vera_core/src/vera_core/transcript.py`
- Test: `packages/vera_core/tests/unit/test_transcript_snapshot.py`

**Interfaces:**
- Produces:
  - `TranscriptStore.snapshot(self, room_name: str) -> list[tuple[str, TranscriptEvent]]` (Protocol method).
  - `InMemoryTranscriptStore.snapshot`, `RedisTranscriptStore.snapshot`.
  - `TranscriptService.snapshot(self, room_name: str) -> list[TranscriptEvent]`.

- [ ] **Step 1: Write the failing test** (InMemory store)

```python
# packages/vera_core/tests/unit/test_transcript_snapshot.py
import pytest
from vera_core.transcript import InMemoryTranscriptStore, TranscriptService


@pytest.mark.asyncio
async def test_snapshot_returns_published_turns_in_order():
    store = InMemoryTranscriptStore()
    svc = TranscriptService(store)
    await svc.publish_turn("room1", "user", "hello", ts=1)
    await svc.publish_turn("room1", "agent", "hi there", ts=2)
    await svc.end("room1")  # sentinel present; snapshot must ignore it

    turns = await svc.snapshot("room1")

    assert [(t.role, t.text) for t in turns] == [("user", "hello"), ("agent", "hi there")]


@pytest.mark.asyncio
async def test_snapshot_of_missing_stream_is_empty():
    svc = TranscriptService(InMemoryTranscriptStore())
    assert await svc.snapshot("nope") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest packages/vera_core/tests/unit/test_transcript_snapshot.py -v`
Expected: FAIL with `AttributeError: 'TranscriptService' object has no attribute 'snapshot'`.

- [ ] **Step 3: Add `snapshot` to the Protocol, both stores, and the service**

In `transcript.py`, add to the `TranscriptStore` Protocol (after `read`):

```python
    async def snapshot(self, room_name: str) -> list[tuple[str, TranscriptEvent]]: ...
```

Add to `InMemoryTranscriptStore`:

```python
    async def snapshot(self, room_name: str) -> list[tuple[str, TranscriptEvent]]:
        key = transcript_stream_key(room_name)
        out: list[tuple[str, TranscriptEvent]] = []
        async with self._cond:
            for entry_id, fields in self._entries.get(key, []):
                if fields.get(_ENDED_FIELD) == _ENDED_VALUE:
                    continue
                out.append(
                    (
                        entry_id,
                        TranscriptEvent(
                            role=fields["role"], text=fields["text"], ts=int(fields["ts"])
                        ),
                    )
                )
        return out
```

Add to `RedisTranscriptStore` (XRANGE the whole stream once — no BLOCK, so no `TimeoutError`):

```python
    async def snapshot(self, room_name: str) -> list[tuple[str, TranscriptEvent]]:
        key = transcript_stream_key(room_name)
        entries = cast(
            list[tuple[str, dict[str, str]]],
            await self._redis.xrange(key),
        )
        out: list[tuple[str, TranscriptEvent]] = []
        for entry_id, fields in entries:
            if fields.get(_ENDED_FIELD) == _ENDED_VALUE:
                continue
            out.append(
                (
                    entry_id,
                    TranscriptEvent(
                        role=fields["role"], text=fields["text"], ts=int(fields["ts"])
                    ),
                )
            )
        return out
```

Add to `TranscriptService`:

```python
    async def snapshot(self, room_name: str) -> list[TranscriptEvent]:
        """One-shot, non-blocking drain of the current stream (post-call re-read).
        Unlike consume()/collect(), returns even if the ended sentinel is absent."""
        return [event for _id, event in await self._store.snapshot(room_name)]
```

(`cast` is already imported in `transcript.py`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest packages/vera_core/tests/unit/test_transcript_snapshot.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/transcript.py packages/vera_core/tests/unit/test_transcript_snapshot.py
git commit -m "feat(transcript): non-blocking snapshot() for the post-call drain"
```

---

## Task 4: `LLMClient` Protocol + request/response types + `FakeLLMClient`

Pure types in `vera_core` (no google dep) so `post_call_eval` can type against them and tests inject a fake.

**Files:**
- Create: `packages/vera_core/src/vera_core/integrations/llm.py`
- Test: `packages/vera_core/tests/unit/test_fake_llm.py`

**Interfaces:**
- Produces (exact names the later tasks depend on):
  - `@dataclass(frozen=True) TranscriptTurn: seq: int; role: str; text: str`
  - `@dataclass(frozen=True) ExtractedField: field_path: str; value: str; confidence: int; evidence_seq: int`
  - `@dataclass(frozen=True) JudgeVerdict: field_path: str; supported: bool; confidence: int; evidence: str`
  - `class LLMClient(Protocol)`:
    - `async def extract(self, *, field_paths: list[str], turns: list[TranscriptTurn]) -> list[ExtractedField]: ...`
    - `async def judge(self, *, extracted: list[ExtractedField], turns: list[TranscriptTurn]) -> list[JudgeVerdict]: ...`
  - `class FakeLLMClient(LLMClient)`: constructed with canned `extract`/`judge` results for tests.

- [ ] **Step 1: Write the failing test**

```python
# packages/vera_core/tests/unit/test_fake_llm.py
import pytest
from vera_core.integrations.llm import (
    ExtractedField,
    FakeLLMClient,
    JudgeVerdict,
    TranscriptTurn,
)


@pytest.mark.asyncio
async def test_fake_llm_returns_canned_results():
    extracted = [ExtractedField("sections.cov.network_status", "in-network", 90, 2)]
    verdicts = [JudgeVerdict("sections.cov.network_status", True, 88, "in network")]
    client = FakeLLMClient(extracted=extracted, verdicts=verdicts)

    turns = [TranscriptTurn(2, "user", "you are in network")]
    assert await client.extract(field_paths=["sections.cov.network_status"], turns=turns) == extracted
    assert await client.judge(extracted=extracted, turns=turns) == verdicts
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest packages/vera_core/tests/unit/test_fake_llm.py -v`
Expected: FAIL with `ModuleNotFoundError: vera_core.integrations.llm`.

- [ ] **Step 3: Create the module**

```python
# packages/vera_core/src/vera_core/integrations/llm.py
"""LLM seam for the post-call re-read — a pure Protocol + DTOs, no provider SDK.

The concrete Vertex/Gemini client lives in the control plane (control_plane.llm);
vera_core only knows this interface so the eval service stays provider-agnostic and
unit-testable with FakeLLMClient.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TranscriptTurn:
    """One de-identified transcript turn handed to the LLM. `seq` is the 0-based
    index within the call's snapshot — the stable pointer stored as evidence_seq."""

    seq: int
    role: str
    text: str


@dataclass(frozen=True)
class ExtractedField:
    field_path: str
    value: str
    confidence: int  # 0-100
    evidence_seq: int  # index into the turns list


@dataclass(frozen=True)
class JudgeVerdict:
    field_path: str
    supported: bool
    confidence: int  # 0-100
    evidence: str


class LLMClient(Protocol):
    async def extract(
        self, *, field_paths: list[str], turns: list[TranscriptTurn]
    ) -> list[ExtractedField]: ...

    async def judge(
        self, *, extracted: list[ExtractedField], turns: list[TranscriptTurn]
    ) -> list[JudgeVerdict]: ...


class FakeLLMClient:
    """Deterministic test double."""

    def __init__(
        self, *, extracted: list[ExtractedField], verdicts: list[JudgeVerdict]
    ) -> None:
        self._extracted = extracted
        self._verdicts = verdicts

    async def extract(
        self, *, field_paths: list[str], turns: list[TranscriptTurn]
    ) -> list[ExtractedField]:
        return list(self._extracted)

    async def judge(
        self, *, extracted: list[ExtractedField], turns: list[TranscriptTurn]
    ) -> list[JudgeVerdict]:
        return list(self._verdicts)
```

Create `packages/vera_core/src/vera_core/integrations/__init__.py` if it does not exist (empty file).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd vera-backend && uv run pytest packages/vera_core/tests/unit/test_fake_llm.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/integrations/
git add packages/vera_core/tests/unit/test_fake_llm.py
git commit -m "feat(llm): LLMClient Protocol + DTOs + FakeLLMClient for post-call eval"
```

---

## Task 5: `post_call_eval` — pure helpers (token detection, evidence, status decision)

Before the DB orchestration, build the **pure** decision helpers so they're unit-tested in isolation.

**Files:**
- Create: `packages/vera_core/src/vera_core/services/post_call_eval.py` (helpers only this task)
- Test: `packages/vera_core/tests/unit/test_post_call_eval_helpers.py`

**Interfaces:**
- Consumes: `ExtractedField`, `JudgeVerdict`, `TranscriptTurn` (Task 4); `PHI token regex` from `phi_codec.tokens.token.TOKEN_RE`.
- Produces:
  - `PHI_TOKEN_RE` (re-exported `TOKEN_RE`) and `has_phi_token(value: str) -> bool`.
  - `REVIEW_CONFIDENCE_FLOOR` used by `needs_review`.
  - `def needs_review(extracted: ExtractedField, verdict: JudgeVerdict | None, *, floor: int) -> bool`
    → True if the value still contains a token, OR verdict missing, OR `not verdict.supported`, OR `verdict.confidence < floor`.
  - `def evidence_text(turns: list[TranscriptTurn], evidence_seq: int) -> str | None` (safe index lookup).

- [ ] **Step 1: Write the failing test**

```python
# packages/vera_core/tests/unit/test_post_call_eval_helpers.py
from vera_core.integrations.llm import ExtractedField, JudgeVerdict, TranscriptTurn
from vera_core.services.post_call_eval import (
    evidence_text,
    has_phi_token,
    needs_review,
)

FLOOR = 60


def test_has_phi_token_detects_bracket_token():
    assert has_phi_token("[[MEMBER_ID_1]]") is True
    assert has_phi_token("in-network") is False


def test_needs_review_when_value_still_tokenized():
    ef = ExtractedField("p", "[[MEMBER_ID_1]]", 95, 0)
    v = JudgeVerdict("p", True, 95, "e")
    assert needs_review(ef, v, floor=FLOOR) is True


def test_needs_review_when_unsupported_or_low_confidence():
    ef = ExtractedField("p", "in-network", 95, 0)
    assert needs_review(ef, JudgeVerdict("p", False, 95, "e"), floor=FLOOR) is True
    assert needs_review(ef, JudgeVerdict("p", True, 40, "e"), floor=FLOOR) is True
    assert needs_review(ef, None, floor=FLOOR) is True


def test_no_review_when_supported_and_confident_and_clean():
    ef = ExtractedField("p", "in-network", 95, 0)
    assert needs_review(ef, JudgeVerdict("p", True, 80, "e"), floor=FLOOR) is False


def test_evidence_text_safe_index():
    turns = [TranscriptTurn(0, "user", "hello"), TranscriptTurn(1, "agent", "in network")]
    assert evidence_text(turns, 1) == "in network"
    assert evidence_text(turns, 9) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest packages/vera_core/tests/unit/test_post_call_eval_helpers.py -v`
Expected: FAIL with `ModuleNotFoundError: vera_core.services.post_call_eval`.

- [ ] **Step 3: Create the helpers**

```python
# packages/vera_core/src/vera_core/services/post_call_eval.py
"""Post-call re-read: extract collected fields from the (de-identified) transcript,
persist them, judge each, and decide the form's terminal status. Pure helpers here;
the DB orchestration (evaluate_call) is added in a later task.
"""

from phi_codec.tokens.token import TOKEN_RE

from vera_core.integrations.llm import ExtractedField, JudgeVerdict, TranscriptTurn

# A judge verdict below this confidence (or unsupported) routes the field to review.
REVIEW_CONFIDENCE_FLOOR = 60

PHI_TOKEN_RE = TOKEN_RE


def has_phi_token(value: str) -> bool:
    """True if the extracted value still contains a `[[TYPE_N]]` PHI token — meaning the
    LLM surfaced an identifier we cannot safely materialize (no live vault). Such fields
    are routed to review rather than stored as a token."""
    return PHI_TOKEN_RE.search(value) is not None


def needs_review(
    extracted: ExtractedField, verdict: JudgeVerdict | None, *, floor: int
) -> bool:
    if has_phi_token(extracted.value):
        return True
    if verdict is None or not verdict.supported:
        return True
    return verdict.confidence < floor


def evidence_text(turns: list[TranscriptTurn], evidence_seq: int) -> str | None:
    if 0 <= evidence_seq < len(turns):
        return turns[evidence_seq].text
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest packages/vera_core/tests/unit/test_post_call_eval_helpers.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/services/post_call_eval.py packages/vera_core/tests/unit/test_post_call_eval_helpers.py
git commit -m "feat(post-call): pure eval helpers (token detection, review gate, evidence)"
```

---

## Task 6: `evaluate_call` — DB orchestration (extract → persist → judge → status → dispatch)

The heart of the pipeline. Runs inside a caller-provided tenant-scoped session. Idempotent on redelivery.

**Files:**
- Modify: `packages/vera_core/src/vera_core/services/post_call_eval.py`
- Test: `packages/vera_core/tests/integration/test_post_call_eval.py` (real docker Postgres via the repo's integration fixtures)

**Interfaces:**
- Consumes: `LLMClient` (Task 4), helpers (Task 5), `FormStateMachine` (Task 1), models `PatientForm`, `Call`, `FieldAnswer`, `FieldEvaluation`, `CallFormSnapshot`, `SchemaVersion`, `Tenant`; `AuditSink`; dispatcher `try_dispatch`; `dsl.load_document`, `completion_pct_v2`/`completion_pct`, `is_v2` (from `vera_core.forms.review`).
- Produces:
  - `@dataclass EvalDeps: llm: LLMClient; audit: AuditSink; livekit: Any; floor: int = REVIEW_CONFIDENCE_FLOOR`
  - `@dataclass EvalOutcome: status: FormStatus; answers_written: int; reviewed_fields: list[str]`
  - `async def evaluate_call(session: AsyncSession, deps: EvalDeps, *, tenant_id: UUID, form_id: UUID, call_id: UUID, turns: list[TranscriptTurn]) -> EvalOutcome`

**Behavior (implement exactly):**
1. **Idempotency guard:** if any `FieldAnswer` with `source='ai_call'` and `call_id == call_id` exists, return early `EvalOutcome(status=FormStatus(form.status), answers_written=0, reviewed_fields=[])` (no re-write, safe on redelivery).
2. Load `form`, `tenant`, `version` (schema). Parse the schema with `dsl.load_document(json.dumps(version.schema_json))` → `doc`; `paths = doc.collection_paths()`.
3. If `turns` is empty → route to `EXCEPTION_REVIEW` (no transcript), audit, transition, `try_dispatch`, return.
4. `extracted = await deps.llm.extract(field_paths=paths, turns=turns)`.
5. For each `ExtractedField`, if `has_phi_token(value)` → add path to `reviewed`, **skip writing** a value. Else write a current `FieldAnswer` via the **merge invariant**: demote any existing current for `(form_id, field_path)` (`is_current=False`, `flush()`), then `session.add(FieldAnswer(source='ai_call', call_id=call_id, value={"value": value}, confidence=..., evidence_seq=..., evidence=evidence_text(turns, evidence_seq), is_current=True, ...))`.
6. `verdicts = await deps.llm.judge(extracted=written_extracted, turns=turns)`; index by `field_path`. For each written answer, `session.add(FieldEvaluation(answer_id=..., supported=..., confidence=..., evidence=...))`. Apply `needs_review(...)` → collect `reviewed`.
7. Recompute `completion_pct` (mirror `patient_forms.py:651`): load current values, `completion_pct_v2(values, version.schema_json)` if `is_v2(...)` else `completion_pct(set(values), version.schema_json)`.
8. Write `CallFormSnapshot(call_id=call_id, before_state=..., after_state=<current values dict>)` **only if** none exists for the call (the callback wrote `before_state`; here we set `after_state` — update the existing row).
9. Decide status: `EXCEPTION_REVIEW` if `reviewed` non-empty else `COMPLETED`. `FormStateMachine().transition(form, status, tenant_max_retries=tenant.max_retries)`.
10. Emit an `AuditRecord` (`event_type=AuditEvent.FORM_STATUS_CHANGE.value`, `actor_type=SERVICE`, `actor_label="post-call-eval"`, `detail={"from": prev, "to": form.status, "call_id": str(call_id), "reviewed": len(reviewed), "answers": written, "trigger": "post_call_eval"}`) — names/counts only.
11. `await try_dispatch(session, tenant_id, deps.livekit, audit=deps.audit)`.
12. Return `EvalOutcome`.

- [ ] **Step 1: Write the failing integration test** (uses the repo's `db_session`/tenant fixtures; see `tests/integration/control_plane/test_call_queue.py` for the seeding pattern — a tenant, a `PatientForm` in `AI_PROCESSING`, a `Call`, a `SchemaVersion`)

```python
# packages/vera_core/tests/integration/test_post_call_eval.py
import pytest
from sqlalchemy import select
from vera_core.integrations.llm import ExtractedField, FakeLLMClient, JudgeVerdict, TranscriptTurn
from vera_core.models.enums import AnswerSource, FormStatus
from vera_core.models.field_answer import FieldAnswer, FieldEvaluation
from vera_core.services.post_call_eval import EvalDeps, evaluate_call

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_evaluate_call_writes_answers_and_completes(seeded_ai_processing_form, fake_audit, fake_livekit):
    ctx = seeded_ai_processing_form  # fixture: returns tenant_id, form_id, call_id, one collection path
    path = ctx.collection_path
    turns = [TranscriptTurn(0, "agent", "are they in network"), TranscriptTurn(1, "user", "yes in network")]
    llm = FakeLLMClient(
        extracted=[ExtractedField(path, "in-network", 92, 1)],
        verdicts=[JudgeVerdict(path, True, 88, "yes in network")],
    )
    deps = EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit)

    outcome = await evaluate_call(
        ctx.session, deps, tenant_id=ctx.tenant_id, form_id=ctx.form_id, call_id=ctx.call_id, turns=turns
    )

    assert outcome.status == FormStatus.COMPLETED
    rows = (await ctx.session.execute(
        select(FieldAnswer).where(FieldAnswer.form_id == ctx.form_id, FieldAnswer.source == AnswerSource.AI_CALL.value)
    )).scalars().all()
    assert len(rows) == 1 and rows[0].evidence == "yes in network" and rows[0].is_current
    evals = (await ctx.session.execute(select(FieldEvaluation))).scalars().all()
    assert len(evals) == 1 and evals[0].supported is True


@pytest.mark.asyncio
async def test_token_valued_field_routes_to_review(seeded_ai_processing_form, fake_audit, fake_livekit):
    ctx = seeded_ai_processing_form
    path = ctx.collection_path
    turns = [TranscriptTurn(0, "user", "member id is [[MEMBER_ID_1]]")]
    llm = FakeLLMClient(
        extracted=[ExtractedField(path, "[[MEMBER_ID_1]]", 99, 0)],
        verdicts=[JudgeVerdict(path, True, 99, "member id")],
    )
    outcome = await evaluate_call(
        ctx.session, EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit),
        tenant_id=ctx.tenant_id, form_id=ctx.form_id, call_id=ctx.call_id, turns=turns,
    )
    assert outcome.status == FormStatus.EXCEPTION_REVIEW
    rows = (await ctx.session.execute(
        select(FieldAnswer).where(FieldAnswer.source == AnswerSource.AI_CALL.value)
    )).scalars().all()
    assert rows == []  # token value never stored


@pytest.mark.asyncio
async def test_redelivery_is_a_noop(seeded_ai_processing_form, fake_audit, fake_livekit):
    ctx = seeded_ai_processing_form
    path = ctx.collection_path
    turns = [TranscriptTurn(0, "user", "in network")]
    llm = FakeLLMClient(
        extracted=[ExtractedField(path, "in-network", 92, 0)],
        verdicts=[JudgeVerdict(path, True, 88, "in network")],
    )
    deps = EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit)
    kwargs = dict(tenant_id=ctx.tenant_id, form_id=ctx.form_id, call_id=ctx.call_id, turns=turns)
    await evaluate_call(ctx.session, deps, **kwargs)
    second = await evaluate_call(ctx.session, deps, **kwargs)
    assert second.answers_written == 0
    rows = (await ctx.session.execute(
        select(FieldAnswer).where(FieldAnswer.source == AnswerSource.AI_CALL.value)
    )).scalars().all()
    assert len(rows) == 1  # not doubled
```

Add the `seeded_ai_processing_form`, `fake_audit`, `fake_livekit` fixtures to `packages/vera_core/tests/integration/conftest.py` (reuse the tenant/session fixtures already there; `fake_audit` is a stub `AuditSink` collecting records; `fake_livekit` is an object with an async `set_room_metadata`/`delete_room` no-op, matching what `try_dispatch` needs).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && just up && uv run pytest packages/vera_core/tests/integration/test_post_call_eval.py -v`
Expected: FAIL with `ImportError: cannot import name 'evaluate_call'`.

- [ ] **Step 3: Implement `evaluate_call`** in `post_call_eval.py`

Append the DTOs + function implementing the 12-step behavior above. Key excerpts (write the full function):

```python
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.audit import AuditRecord, AuditSink
from vera_core.forms import dsl
from vera_core.forms.review import completion_pct, completion_pct_v2, is_v2
from vera_core.integrations.llm import ExtractedField, JudgeVerdict, LLMClient, TranscriptTurn
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.call import Call  # noqa: F401  (kept explicit for readers)
from vera_core.models.enums import AnswerSource, FormStatus
from vera_core.models.field_answer import CallFormSnapshot, FieldAnswer, FieldEvaluation
from vera_core.models.patient_form import PatientForm
from vera_core.models.schema_version import SchemaVersion
from vera_core.models.tenant import Tenant
from vera_core.services.form_state_machine import FormStateMachine
from vera_core.services.queue_dispatcher import try_dispatch


@dataclass
class EvalDeps:
    llm: LLMClient
    audit: AuditSink
    livekit: Any
    floor: int = REVIEW_CONFIDENCE_FLOOR


@dataclass
class EvalOutcome:
    status: FormStatus
    answers_written: int
    reviewed_fields: list[str] = field(default_factory=list)


async def _demote_current(session: AsyncSession, form_id: UUID, field_path: str) -> None:
    await session.execute(
        update(FieldAnswer)
        .where(
            FieldAnswer.form_id == form_id,
            FieldAnswer.field_path == field_path,
            FieldAnswer.is_current.is_(True),
        )
        .values(is_current=False)
    )
    await session.flush()  # clear the old current before inserting the new one


async def evaluate_call(
    session: AsyncSession,
    deps: EvalDeps,
    *,
    tenant_id: UUID,
    form_id: UUID,
    call_id: UUID,
    turns: list[TranscriptTurn],
) -> EvalOutcome:
    form = (
        await session.execute(select(PatientForm).where(PatientForm.id == form_id).with_for_update())
    ).scalar_one()

    # (1) Idempotency guard.
    already = (
        await session.execute(
            select(FieldAnswer.id).where(
                FieldAnswer.call_id == call_id,
                FieldAnswer.source == AnswerSource.AI_CALL.value,
            ).limit(1)
        )
    ).first()
    if already is not None:
        return EvalOutcome(status=FormStatus(form.status), answers_written=0)

    tenant = (await session.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
    version = (
        await session.execute(select(SchemaVersion).where(SchemaVersion.id == form.schema_version_id))
    ).scalar_one()
    prev_status = form.status
    sm = FormStateMachine()

    async def _finish(target: FormStatus, *, written: int, reviewed: list[str]) -> EvalOutcome:
        sm.transition(form, target, tenant_max_retries=tenant.max_retries)
        await session.flush()
        await deps.audit.emit(AuditRecord(
            tenant_id=tenant_id, actor_type=ActorType.SERVICE, actor_user_id=None,
            actor_label="post-call-eval", event_type=AuditEvent.FORM_STATUS_CHANGE.value,
            resource_type="patient_form", resource_id=str(form_id),
            detail={"from": prev_status, "to": form.status, "call_id": str(call_id),
                    "reviewed": len(reviewed), "answers": written, "trigger": "post_call_eval"},
        ))
        await try_dispatch(session, tenant_id, deps.livekit, audit=deps.audit)
        return EvalOutcome(status=target, answers_written=written, reviewed_fields=reviewed)

    # (3) No transcript → review.
    if not turns:
        return await _finish(FormStatus.EXCEPTION_REVIEW, written=0, reviewed=[])

    doc = dsl.load_document(json.dumps(version.schema_json))
    paths = doc.collection_paths()

    # (4-5) Extract + persist (skip token-valued).
    extracted = await deps.llm.extract(field_paths=paths, turns=turns)
    reviewed: list[str] = []
    kept: list[ExtractedField] = []
    for ef in extracted:
        if has_phi_token(ef.value):
            reviewed.append(ef.field_path)
            continue
        await _demote_current(session, form_id, ef.field_path)
        session.add(FieldAnswer(
            tenant_id=tenant_id, form_id=form_id, call_id=call_id, field_path=ef.field_path,
            value={"value": ef.value}, source=AnswerSource.AI_CALL.value,
            confidence=ef.confidence, evidence_seq=ef.evidence_seq,
            evidence=evidence_text(turns, ef.evidence_seq), is_current=True,
        ))
        kept.append(ef)
    await session.flush()

    # (6) Judge + field_evaluation.
    verdicts = {v.field_path: v for v in await deps.llm.judge(extracted=kept, turns=turns)}
    answer_ids = {
        r.field_path: r.id
        for r in (await session.execute(
            select(FieldAnswer).where(
                FieldAnswer.call_id == call_id,
                FieldAnswer.source == AnswerSource.AI_CALL.value,
                FieldAnswer.is_current.is_(True),
            )
        )).scalars().all()
    }
    for ef in kept:
        v = verdicts.get(ef.field_path)
        if v is not None:
            session.add(FieldEvaluation(
                tenant_id=tenant_id, answer_id=answer_ids[ef.field_path],
                confidence=v.confidence, evidence=v.evidence, supported=v.supported,
            ))
        if needs_review(ef, v, floor=deps.floor):
            reviewed.append(ef.field_path)
    await session.flush()

    # (7) completion_pct.
    current = {
        r.field_path: r.value["value"]
        for r in (await session.execute(
            select(FieldAnswer).where(FieldAnswer.form_id == form_id, FieldAnswer.is_current.is_(True))
        )).scalars().all()
    }
    form.completion_pct = (
        completion_pct_v2(current, version.schema_json)
        if is_v2(version.schema_json)
        else completion_pct(set(current), version.schema_json)
    )

    # (8) snapshot.after_state (update the row the callback created).
    await session.execute(
        update(CallFormSnapshot).where(CallFormSnapshot.call_id == call_id).values(after_state=current)
    )

    # (9-11) status + audit + dispatch.
    target = FormStatus.EXCEPTION_REVIEW if reviewed else FormStatus.COMPLETED
    return await _finish(target, written=len(kept), reviewed=reviewed)
```

> **Note for the implementer:** confirm the exact import paths of `SchemaVersion` (`vera_core/models/schema_version.py`) and `is_v2` (grep `def is_v2` in `vera_core/forms/review.py`) before running — adjust the `from ... import` lines to match. If `is_v2`/`completion_pct_v2` are named differently, use the names found at `patient_forms.py:651-655`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest packages/vera_core/tests/integration/test_post_call_eval.py -v`
Expected: PASS (all three).

- [ ] **Step 5: Typecheck + lint the new module**

Run: `cd vera-backend && uv run ruff check packages/vera_core/src/vera_core/services/post_call_eval.py && uv run mypy packages/vera_core/src/vera_core/services/post_call_eval.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/services/post_call_eval.py packages/vera_core/tests/integration/
git commit -m "feat(post-call): evaluate_call orchestration (extract, judge, persist, status)"
```

---

## Task 7: Settings + `VertexLLMClient` (Gemini Flash) + `google-genai` dep

**Files:**
- Modify: `packages/vera_core/src/vera_core/config/settings.py`
- Modify: `apps/control_plane/pyproject.toml`
- Create: `apps/control_plane/src/control_plane/llm.py`
- Test: `apps/control_plane/tests/unit/test_vertex_llm_prompt.py` (test prompt/parse shaping only — no network)

**Interfaces:**
- Produces:
  - Settings: `gemini_flash_model: str = "gemini-2.5-flash"`, `vertex_location: str = "us-central1"`, `post_call_review_floor: int = 60`, `post_call_block_ms: int = 5_000`, `post_call_reclaim_idle_ms: int = 60_000`. (Reuse existing `gcp_project`.)
  - `class VertexLLMClient(LLMClient)`: `__init__(self, *, project: str, location: str, model: str)`; implements `extract`/`judge` via `google-genai` structured output.
  - Pure helpers `build_extract_prompt(field_paths, turns) -> str` and `parse_extract_response(data: list[dict]) -> list[ExtractedField]` (and judge equivalents) so shaping is unit-tested without the SDK.

- [ ] **Step 1: Write the failing test** (shaping only)

```python
# apps/control_plane/tests/unit/test_vertex_llm_prompt.py
from vera_core.integrations.llm import TranscriptTurn
from control_plane.llm import build_extract_prompt, parse_extract_response


def test_extract_prompt_numbers_turns_and_lists_paths():
    turns = [TranscriptTurn(0, "user", "hello"), TranscriptTurn(1, "agent", "in network")]
    prompt = build_extract_prompt(["sections.cov.network_status"], turns)
    assert "sections.cov.network_status" in prompt
    assert "[0]" in prompt and "[1]" in prompt  # evidence_seq anchors


def test_parse_extract_response_maps_fields():
    data = [{"field_path": "p", "value": "in-network", "confidence": 90, "evidence_seq": 1}]
    out = parse_extract_response(data)
    assert out[0].field_path == "p" and out[0].evidence_seq == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest apps/control_plane/tests/unit/test_vertex_llm_prompt.py -v`
Expected: FAIL with `ModuleNotFoundError: control_plane.llm`.

- [ ] **Step 3: Add settings**

In `settings.py`, after the worker-events block:

```python
    # Post-call re-read (LLM eval). Gemini Flash on Vertex (BAA-covered); the review
    # floor routes low-confidence/unsupported fields to EXCEPTION_REVIEW.
    gemini_flash_model: str = "gemini-2.5-flash"  # VERA_GEMINI_FLASH_MODEL
    vertex_location: str = "us-central1"  # VERA_VERTEX_LOCATION
    post_call_review_floor: int = 60  # VERA_POST_CALL_REVIEW_FLOOR
    post_call_block_ms: int = 5_000  # VERA_POST_CALL_BLOCK_MS
    post_call_reclaim_idle_ms: int = 60_000  # VERA_POST_CALL_RECLAIM_IDLE_MS
```

- [ ] **Step 4: Add the dependency**

In `apps/control_plane/pyproject.toml` `dependencies`, add:

```toml
    "google-genai>=1.0",
```

Run: `cd vera-backend && uv sync`
Expected: lockfile updates, install succeeds.

- [ ] **Step 5: Implement `control_plane/llm.py`**

```python
# apps/control_plane/src/control_plane/llm.py
"""Vertex AI Gemini implementation of the post-call LLMClient. Structured output;
Flash by default. Consumes only the de-identified transcript — no raw PHI."""

import json
from typing import Any

from google import genai
from google.genai import types

from vera_core.integrations.llm import ExtractedField, JudgeVerdict, LLMClient, TranscriptTurn


def _turns_block(turns: list[TranscriptTurn]) -> str:
    return "\n".join(f"[{t.seq}] {t.role}: {t.text}" for t in turns)


def build_extract_prompt(field_paths: list[str], turns: list[TranscriptTurn]) -> str:
    return (
        "You are extracting insurance-benefit answers from a de-identified call "
        "transcript. Turns are numbered [n]. For each requested field_path, return the "
        "value stated by the payer, a 0-100 confidence, and evidence_seq = the [n] of the "
        "turn that supports it. Omit fields not present. Do NOT invent values.\n\n"
        f"field_paths:\n{json.dumps(field_paths)}\n\ntranscript:\n{_turns_block(turns)}"
    )


def parse_extract_response(data: list[dict[str, Any]]) -> list[ExtractedField]:
    return [
        ExtractedField(
            field_path=str(d["field_path"]), value=str(d["value"]),
            confidence=int(d["confidence"]), evidence_seq=int(d["evidence_seq"]),
        )
        for d in data
    ]


def build_judge_prompt(extracted: list[ExtractedField], turns: list[TranscriptTurn]) -> str:
    items = [{"field_path": e.field_path, "value": e.value, "evidence_seq": e.evidence_seq} for e in extracted]
    return (
        "For each extracted field, decide whether the transcript SUPPORTS the value. "
        "Return supported (bool), 0-100 confidence, and a short evidence quote.\n\n"
        f"extracted:\n{json.dumps(items)}\n\ntranscript:\n{_turns_block(turns)}"
    )


def parse_judge_response(data: list[dict[str, Any]]) -> list[JudgeVerdict]:
    return [
        JudgeVerdict(
            field_path=str(d["field_path"]), supported=bool(d["supported"]),
            confidence=int(d["confidence"]), evidence=str(d["evidence"]),
        )
        for d in data
    ]


_EXTRACT_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "field_path": {"type": "string"}, "value": {"type": "string"},
            "confidence": {"type": "integer"}, "evidence_seq": {"type": "integer"},
        },
        "required": ["field_path", "value", "confidence", "evidence_seq"],
    },
}
_JUDGE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "field_path": {"type": "string"}, "supported": {"type": "boolean"},
            "confidence": {"type": "integer"}, "evidence": {"type": "string"},
        },
        "required": ["field_path", "supported", "confidence", "evidence"],
    },
}


class VertexLLMClient(LLMClient):
    def __init__(self, *, project: str, location: str, model: str) -> None:
        self._client = genai.Client(vertexai=True, project=project, location=location)
        self._model = model

    async def _generate(self, prompt: str, schema: dict[str, Any]) -> list[dict[str, Any]]:
        resp = await self._client.aio.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=schema
            ),
        )
        return list(json.loads(resp.text))

    async def extract(
        self, *, field_paths: list[str], turns: list[TranscriptTurn]
    ) -> list[ExtractedField]:
        data = await self._generate(build_extract_prompt(field_paths, turns), _EXTRACT_SCHEMA)
        return parse_extract_response(data)

    async def judge(
        self, *, extracted: list[ExtractedField], turns: list[TranscriptTurn]
    ) -> list[JudgeVerdict]:
        if not extracted:
            return []
        data = await self._generate(build_judge_prompt(extracted, turns), _JUDGE_SCHEMA)
        return parse_judge_response(data)
```

> **Note for the implementer:** the exact `google-genai` call surface (`client.aio.models.generate_content`, `types.GenerateContentConfig`) should be confirmed against the installed version; the pure `build_*`/`parse_*` helpers are what the tests cover, and the thin `_generate` wrapper is exercised in the boot-verification step (Task 10), not unit tests.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest apps/control_plane/tests/unit/test_vertex_llm_prompt.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/src/vera_core/config/settings.py apps/control_plane/pyproject.toml uv.lock apps/control_plane/src/control_plane/llm.py apps/control_plane/tests/unit/test_vertex_llm_prompt.py
git commit -m "feat(llm): VertexLLMClient (Gemini Flash) + post-call settings"
```

---

## Task 8: `PostCallConsumer` — the Redis-stream consumer

**Files:**
- Create: `apps/control_plane/src/control_plane/post_call.py`
- Test: `apps/control_plane/tests/unit/test_post_call_consumer.py`

**Interfaces:**
- Consumes: `PostCallJobBus`, `parse_post_call_job`, `POST_CALL_STREAM/GROUP` (Task 2); `evaluate_call`, `EvalDeps` (Task 6); `TranscriptService.snapshot` (Task 3); `tenant_session` (`vera_core.db.rls`); `LLMClient`.
- Produces: `class PostCallConsumer` with `__init__(self, redis, sessionmaker, transcript, llm, audit, livekit, *, block_ms, reclaim_idle_ms, review_floor, consumer_name=None)` and `async def run(self) -> None`. Handler: `_process_job(job: PostCallJob)` builds `turns` from `transcript.snapshot(room_name_for_call(job.tenant_id, job.call_id))` (enumerate → `TranscriptTurn(seq=i, ...)`), opens `tenant_session`, calls `evaluate_call`.

- [ ] **Step 1: Write the failing test** (drive the handler with a fake redis is heavy; instead test `_build_turns` + `_process_job` against a fake transcript + in-memory sqlite-less path is hard — so test the pure turn-building and that `_process_job` invokes `evaluate_call` via monkeypatch)

```python
# apps/control_plane/tests/unit/test_post_call_consumer.py
import pytest
from uuid import uuid4
from vera_core.integrations.llm import TranscriptTurn
from vera_core.transcript import InMemoryTranscriptStore, TranscriptService
from vera_core.observability.correlation import room_name_for_call
from control_plane.post_call import build_turns


@pytest.mark.asyncio
async def test_build_turns_enumerates_snapshot():
    store = InMemoryTranscriptStore()
    svc = TranscriptService(store)
    tenant_id, call_id = uuid4(), uuid4()
    room = room_name_for_call(tenant_id, call_id)
    await svc.publish_turn(room, "user", "hello", ts=1)
    await svc.publish_turn(room, "agent", "in network", ts=2)

    turns = await build_turns(svc, tenant_id, call_id)

    assert turns == [TranscriptTurn(0, "user", "hello"), TranscriptTurn(1, "agent", "in network")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest apps/control_plane/tests/unit/test_post_call_consumer.py -v`
Expected: FAIL with `ModuleNotFoundError: control_plane.post_call`.

- [ ] **Step 3: Implement the consumer** (mirror `worker_events.py` exactly for the loop/ack/reclaim; new handler)

```python
# apps/control_plane/src/control_plane/post_call.py
"""Post-call eval consumer: drains vera:post-call, re-reads each finished call's
transcript, and runs evaluate_call. Mirrors worker_events.WorkerEventConsumer for the
group/ack/reclaim + idle-TimeoutError discipline."""

import asyncio
import logging
import os
import socket
from typing import Any, cast
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.audit import AuditSink
from vera_core.db.rls import tenant_session
from vera_core.events import (
    POST_CALL_GROUP,
    POST_CALL_STREAM,
    PostCallJob,
    PostCallJobBus,
    parse_post_call_job,
)
from vera_core.integrations.llm import LLMClient, TranscriptTurn
from vera_core.observability.correlation import room_name_for_call
from vera_core.services.post_call_eval import EvalDeps, evaluate_call
from vera_core.transcript import TranscriptService

logger = logging.getLogger("control_plane.post_call")

type _StreamEntries = list[tuple[str, dict[str, str]]]


async def build_turns(
    transcript: TranscriptService, tenant_id: UUID, call_id: UUID
) -> list[TranscriptTurn]:
    room = room_name_for_call(tenant_id, call_id)
    events = await transcript.snapshot(room)
    return [TranscriptTurn(seq=i, role=e.role, text=e.text) for i, e in enumerate(events)]


class PostCallConsumer:
    def __init__(
        self,
        redis: Redis,
        sessionmaker: async_sessionmaker[AsyncSession],
        transcript: TranscriptService,
        llm: LLMClient,
        audit: AuditSink,
        livekit: Any,
        *,
        block_ms: int = 5_000,
        reclaim_idle_ms: int = 60_000,
        review_floor: int = 60,
        consumer_name: str | None = None,
    ) -> None:
        self._redis = redis
        self._sessionmaker = sessionmaker
        self._transcript = transcript
        self._llm = llm
        self._audit = audit
        self._livekit = livekit
        self._block_ms = block_ms
        self._reclaim_idle_ms = reclaim_idle_ms
        self._floor = review_floor
        self._consumer = consumer_name or f"{socket.gethostname()}:{os.getpid()}"
        self._bus = PostCallJobBus(redis)

    async def run(self) -> None:
        group_ready = False
        while True:
            try:
                if not group_ready:
                    await self._bus.ensure_group()
                    group_ready = True
                await self._reclaim_stale()
                await self._read_once()
            except asyncio.CancelledError:
                raise
            except RedisError:
                logger.exception("post-call consumer Redis error; backing off")
                await asyncio.sleep(1.0)

    async def _read_once(self) -> None:
        try:
            resp = await self._redis.xreadgroup(
                POST_CALL_GROUP, self._consumer, {POST_CALL_STREAM: ">"},
                count=16, block=self._block_ms,
            )
        except RedisTimeoutError:
            return  # idle tick — see CLAUDE.md
        if not resp:
            return
        streams = cast("list[tuple[str, _StreamEntries]]", resp)
        _stream, entries = streams[0]
        await asyncio.gather(*(self._process(eid, f) for eid, f in entries))

    async def _reclaim_stale(self) -> None:
        result = await self._redis.xautoclaim(
            POST_CALL_STREAM, POST_CALL_GROUP, self._consumer,
            min_idle_time=self._reclaim_idle_ms, start_id="0-0", count=16,
        )
        _cursor, entries, _deleted = cast("tuple[str, _StreamEntries, list[str]]", result)
        await asyncio.gather(*(self._process(eid, f) for eid, f in entries))

    async def _process(self, entry_id: str, fields: dict[str, str]) -> None:
        raw = fields.get("job")
        if raw is None:
            await self._ack(entry_id)
            return
        try:
            job = parse_post_call_job(raw)
        except Exception:
            logger.exception("dropping unparseable post-call job %s", entry_id)
            await self._ack(entry_id)
            return
        try:
            await self._process_job(job)
        except Exception:
            logger.exception("post-call job %s failed; leaving unacked for reclaim", entry_id)
            return  # do NOT ack → XAUTOCLAIM retries (at-least-once)
        await self._ack(entry_id)

    async def _process_job(self, job: PostCallJob) -> None:
        turns = await build_turns(self._transcript, job.tenant_id, job.call_id)
        deps = EvalDeps(llm=self._llm, audit=self._audit, livekit=self._livekit, floor=self._floor)
        async with tenant_session(self._sessionmaker, job.tenant_id) as session:
            outcome = await evaluate_call(
                session, deps, tenant_id=job.tenant_id, form_id=job.form_id,
                call_id=job.call_id, turns=turns,
            )
        logger.info("post-call eval form=%s -> %s (%d answers, %d reviewed)",
                    job.form_id, outcome.status.value, outcome.answers_written,
                    len(outcome.reviewed_fields))

    async def _ack(self, entry_id: str) -> None:
        await self._redis.xack(POST_CALL_STREAM, POST_CALL_GROUP, entry_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest apps/control_plane/tests/unit/test_post_call_consumer.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/control_plane/src/control_plane/post_call.py apps/control_plane/tests/unit/test_post_call_consumer.py
git commit -m "feat(post-call): PostCallConsumer draining vera:post-call"
```

---

## Task 9: Wire the callback — enqueue on `COMPLETED`, snapshot `before_state`

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/calls.py` (`update_call_status`, ~491)
- Test: `apps/control_plane/tests/integration/test_call_status_callback.py` (extend existing callback tests)

**Interfaces:**
- Consumes: `PostCallJobBus`, `PostCallJob` (Task 2); `CallFormSnapshot`; `room` app state redis.
- Produces: on `COMPLETED`, form goes to `AI_PROCESSING` (not `COMPLETED`), a `CallFormSnapshot.before_state` row is written, and a `PostCallJob` is XADDed. Failure statuses unchanged.

- [ ] **Step 1: Write the failing test**

```python
# apps/control_plane/tests/integration/test_call_status_callback.py (add)
@pytest.mark.asyncio
async def test_completed_callback_moves_form_to_ai_processing_and_enqueues(client, seeded_in_call, fake_post_call_bus):
    ctx = seeded_in_call
    r = await client.post(f"/api/v1/calls/{ctx.call_id}/status", json={"status": "completed"}, headers=ctx.worker_headers)
    assert r.status_code == 200
    form = await ctx.reload_form()
    assert form.status == "ai_processing"
    assert fake_post_call_bus.emitted  # one PostCallJob for this call
    snap = await ctx.get_snapshot(ctx.call_id)
    assert snap is not None  # before_state written
```

Wire `fake_post_call_bus` via a settable `app.state.post_call_bus` (add that state key in Task 10) so the test can assert without Redis.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest apps/control_plane/tests/integration/test_call_status_callback.py::test_completed_callback_moves_form_to_ai_processing_and_enqueues -v`
Expected: FAIL (form becomes `completed`, no job emitted).

- [ ] **Step 3: Modify the callback**

In `update_call_status`, replace the `COMPLETED` branch (`calls.py:491-492`):

```python
        if body.status == CallStatus.COMPLETED:
            sm.transition(form, FormStatus.AI_PROCESSING, tenant_max_retries=tenant.max_retries)
            session.add(CallFormSnapshot(
                tenant_id=tenant_id, call_id=call.id,
                before_state=await _current_values(session, form.id), after_state={},
            ))
```

After `await session.flush()` and the existing audit emit, before `try_dispatch`, enqueue:

```python
        if form.status == FormStatus.AI_PROCESSING.value:
            bus: PostCallJobBus = request.app.state.post_call_bus
            await bus.emit(PostCallJob(tenant_id=tenant_id, form_id=form.id, call_id=call.id))
```

Add a small `_current_values(session, form_id) -> dict[str, Any]` helper in `calls.py` (query current `FieldAnswer` rows → `{field_path: value["value"]}`), or import an existing equivalent if one exists in `patient_forms.py` (grep `current` value-builder there first and reuse).

> **Important:** on `COMPLETED` we no longer transition straight to `COMPLETED`; the slot stays held (dispatcher counts `AI_PROCESSING`). `try_dispatch` still runs (harmless — no slot freed yet); the eval pipeline frees it when it finishes. Keep the failure-path (`CALL_FAILED` + auto-retry) exactly as-is.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest apps/control_plane/tests/integration/test_call_status_callback.py -v`
Expected: PASS (new + existing failure-path tests).

- [ ] **Step 5: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/calls.py apps/control_plane/tests/integration/test_call_status_callback.py
git commit -m "feat(calls): completed callback -> AI_PROCESSING + snapshot + enqueue post-call job"
```

---

## Task 10: Boot the consumer + bus in the app lifespan

**Files:**
- Modify: `apps/control_plane/src/control_plane/main.py`
- Test: covered by boot-verification (below) + a smoke test that `create_app()` starts without error when `livekit_url` set.

**Interfaces:**
- Consumes: `PostCallConsumer` (Task 8), `PostCallJobBus`, `VertexLLMClient` (Task 7).
- Produces: `app.state.post_call_bus` set always (so the callback + tests can emit); the consumer task started when `livekit_url` + `gcp_project` are configured (skipped in tests, like `worker_events`).

- [ ] **Step 1: Add the wiring** in `lifespan` (after the worker-events consumer block)

```python
        # Post-call bus is always available (the callback enqueues through it); tests
        # inject a fake via app.state before issuing the callback.
        post_call_redis: Redis | None = None
        post_call_task: asyncio.Task[None] | None = None
        app.state.post_call_bus = PostCallJobBus(_redis())
        if (
            settings.livekit_url is not None
            and app.state.livekit is not None
            and settings.gcp_project is not None
        ):
            post_call_redis = create_redis(settings.redis_url)
            llm = VertexLLMClient(
                project=settings.gcp_project,
                location=settings.vertex_location,
                model=settings.gemini_flash_model,
            )
            consumer = PostCallConsumer(
                post_call_redis, sessionmaker, _transcript_service, llm,
                app.state.audit, app.state.livekit,
                block_ms=settings.post_call_block_ms,
                reclaim_idle_ms=settings.post_call_reclaim_idle_ms,
                review_floor=settings.post_call_review_floor,
            )
            post_call_task = asyncio.create_task(consumer.run())
            post_call_task.add_done_callback(_log_consumer_exit)
```

Add symmetric teardown in the shutdown half (mirror the worker-events cancel + `aclose()`), and import `PostCallConsumer`, `PostCallJobBus`, `VertexLLMClient` at the top.

For tests: allow injection via a new `create_app(..., post_call_bus: PostCallJobBus | None = None)` kwarg, and set `app.state.post_call_bus = post_call_bus or PostCallJobBus(_redis())`.

- [ ] **Step 2: Smoke-test app creation**

Run: `cd vera-backend && uv run pytest apps/control_plane/tests -k "create_app or lifespan or callback" -v`
Expected: PASS.

- [ ] **Step 3: Full gate**

Run: `cd vera-backend && just check`
Expected: ruff + mypy --strict + pytest all green.

- [ ] **Step 4: Boot-verify the background loop (repo rule — REQUIRED)**

```bash
cd vera-backend && just up
# terminal A:
LOCAL_KMS_MASTER_KEY=... VERA_LIVEKIT_URL=ws://localhost:7880 VERA_GCP_PROJECT=<proj> just api
```
Watch startup: the `post-call` consumer logs its group bootstrap and idles across two `post_call_block_ms` windows **without** a `TimeoutError` traceback or back-off spam. (Confirms the idle-`TimeoutError` handling; a fake-returning test can't.)

- [ ] **Step 5: Commit**

```bash
git add apps/control_plane/src/control_plane/main.py
git commit -m "feat(post-call): boot PostCallConsumer + bus in the app lifespan"
```

---

## Task 11 (DEFERRED — do not implement without sign-off): worker token-map seam

**Status:** Blocked / compliance-gated. Documented so it isn't lost.

**Why deferred:** The "reconcile identifier tokens against intake" path (brainstorming decision #4) requires the worker to (a) **seed intake PHI** into the vault at `open_session(known=...)` and (b) stash the resulting `field_path→token` map. But the agent worker is **DB-less** and does **not currently receive intake PHI** — `open_session(session_id)` is called with no `known=` (`agent_worker/main.py:261`). Passing patient PHI to the worker (via dispatch metadata or a new fetch) is a **new PHI-boundary decision** requiring compliance sign-off (backend `CLAUDE.md`), and the user's guidance was "ignore the phi tokenizer for now."

**Consequence for Phase 1 (already handled):** token-valued fields route to `EXCEPTION_REVIEW` (`needs_review`/`has_phi_token`), never stored as tokens. Correct and safe without this seam.

**When unblocked, the seam is feasible** because `phi_codec` exposes `vault.dump(session_id) -> list[VaultEntry]` (`{token, raw_value, entity_type, …}`), so after `seed_session` the worker can stash `{field_path: token}` in Redis under the call key for the pipeline to reconcile against the intake baseline. Track as a follow-up spec with a compliance review + an `adr/devops-todo.md` row.

---

## Self-review

**Spec coverage:**
- §3 pipeline (callback → AI_PROCESSING → stream → consumer → extract/judge/persist → status → dispatch): Tasks 1, 2, 6, 8, 9, 10. ✅
- §4 components (post_call_eval, llm client, consumer, callback change, state machine, worker seam, dep, config): Tasks 1,2,4,5,6,7,8,9,10,11. ✅
- §5 tables (field_answer / field_evaluation / call_form_snapshot usage): Task 6 (+9 before_state). ✅
- §6 LLM contract (extract structured, judge supported/confidence/evidence): Tasks 4, 7. ✅
- §7 status decision (COMPLETED vs EXCEPTION_REVIEW): Task 6. ✅
- §8 error/idempotency (redelivery guard, LLM-failure→review, TimeoutError idle, boot-verify): Tasks 6, 8, 10. ✅
- §2 PHI posture (consume de-identified transcript, no raw PHI to LLM, token→review, audit names-only): Tasks 5, 6, 11. ✅
- D2/D3/D4 open decisions: D2 resolved (worker seam deferred, Task 11); D3 floor = 60 constant (Task 5/7); D4 judge is one batched call per form (Task 4/7). ✅

**Placeholder scan:** No `TBD`/`implement later`; every code step shows code. Two explicit "confirm the exact import path / SDK surface" notes (Task 6, Task 7) point at the file:line to check — not placeholders, but pre-flight confirmations. ✅

**Type consistency:** `ExtractedField`/`JudgeVerdict`/`TranscriptTurn` defined in Task 4 and used unchanged in Tasks 5–8. `evaluate_call(session, deps, *, tenant_id, form_id, call_id, turns)` signature consistent between Tasks 6 and 8. `EvalDeps`/`EvalOutcome` consistent. `build_turns` produced in Task 8 and tested there. `PostCallJob(tenant_id, form_id, call_id)` consistent Tasks 2/8/9. ✅

**Newly surfaced dependency added:** transcript is Redis-only + self-clearing → Task 3 (`snapshot`) added; `evidence_seq` redefined as the snapshot index (no DB transcript needed for Phase 1). Noted in Task 8.
