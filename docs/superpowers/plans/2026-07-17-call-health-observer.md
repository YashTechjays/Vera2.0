# Call Health Observer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A per-call observer in the agent worker that periodically LLM-scores live-call health (0–100 + intervention flag), persists results for reporting, flips `ACTIVE↔CRITICAL`, and pushes realtime intervention alerts to a new login-session notification SSE consumed by the frontend.

**Architecture:** The observer is an extra `TurnPublisher` sink on the worker's existing fan-out (user-turn-triggered, min-interval cooldown, never blocks the cascade). Results ride two existing rails: a new `health` envelope on the per-call `CallStreamService` stream (per-call SSE), and a new `call.health` worker event whose control-plane handler updates denormalized `Call` columns, writes `CallEvent(HEALTH)` rows only on flag transitions, flips status, and publishes user-scoped notifications on a per-tenant Redis stream tailed by a new `GET /api/v1/notifications/stream` SSE.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async / Redis Streams / livekit-agents (`vera_core.llm.ResilientLLM`, primary `google:gemini-3.1-flash-lite`, fallback `openai:gpt-5.4-mini`); React + Vite + TypeScript + vitest + sonner (new dep).

**Spec:** `docs/superpowers/specs/2026-07-17-call-health-observer-design.md` — read it before starting; it holds the rationale for every rule below.

## Global Constraints

- Backend gate: run `just check` **verbatim** (lint + typecheck + test) from `vera-backend/` — never a subset. Re-run after any later commit.
- Frontend gate: `npx tsc -b` + `npx eslint .` + `npm test` + `npm run build` from `vera-frontend/` — all four, every time.
- Migrations MUST be idempotent: `ADD COLUMN IF NOT EXISTS`; constraints via `DO $$ ... EXCEPTION WHEN duplicate_object THEN NULL; END $$`. Never hand-number revision ids — `alembic revision` generates them.
- PHI logging: never log transcript text, LLM replies, notification payloads, or exception reprs near PHI I/O — log `type(exc).__name__` only.
- Every out-of-pipeline LLM call goes through `vera_core.llm.ResilientLLM` — never a provider SDK/plugin client at a call site.
- `asyncio` only (no anyio imports); PEP 695 type params; Python pinned 3.12.
- The prompt prefix-stability rule: the health system prompt is byte-identical across all analyses of all calls; anything dynamic goes AFTER the transcript in the user message.
- Commits: no `Co-Authored-By` lines (user rule).
- A change adding a long-lived background loop MUST be verified by booting the real service (Task 11), not pytest alone.
- After the implementation is complete, run the **code-simplifier** agent, then re-run both gates (Task 17) — repo-mandated, not optional.

---

### Task 1: `CallHealthFlag` enum, `Call` health columns, migration

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/models/enums.py` (after `InterventionCategory`, ~line 105)
- Modify: `vera-backend/packages/vera_core/src/vera_core/models/call.py` (Call class, `__table_args__` and columns)
- Create: `vera-backend/migrations/versions/<generated>_call_health_columns.py`
- Test: `vera-backend/tests/unit/db/test_call_health_columns.py`

**Interfaces:**
- Produces: `CallHealthFlag` StrEnum (`NONE="none"`, `SUPERVISOR_REQUESTED="supervisor_requested"`, plus the 7 `InterventionCategory` values); `Call.health_score: int | None`, `Call.health_flag: str | None`, `Call.health_analyzed_at: datetime | None`.

- [ ] **Step 1: Write the failing test**

```python
# vera-backend/tests/unit/db/test_call_health_columns.py
"""Call health denormalized columns + the observer flag vocabulary."""

from uuid import uuid4

from vera_core.models import Call
from vera_core.models.enums import CallHealthFlag, InterventionCategory, values_of


def test_health_flag_vocabulary_superset_of_intervention_categories() -> None:
    flags = set(values_of(CallHealthFlag))
    assert set(values_of(InterventionCategory)) <= flags
    assert "none" in flags
    assert "supervisor_requested" in flags


def test_call_health_columns_default_null() -> None:
    call = Call(id=uuid4(), tenant_id=uuid4(), form_id=uuid4(), current_status="active")
    assert call.health_score is None
    assert call.health_flag is None
    assert call.health_analyzed_at is None


def test_call_has_health_flag_check() -> None:
    names = {c.name for c in Call.__table__.constraints}
    assert "ck_call_health_flag_valid" in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/unit/db/test_call_health_columns.py -v`
Expected: FAIL — `ImportError: cannot import name 'CallHealthFlag'`

- [ ] **Step 3: Add the enum** — in `enums.py`, directly after `InterventionCategory`:

```python
class CallHealthFlag(enum.StrEnum):
    """Call-health observer verdict vocabulary: the InterventionCategory values
    (kept in sync so intervention reports and observer flags speak one language)
    plus `none` (healthy) and `supervisor_requested` (the rep/IVR asked for a
    human). Stored on call.health_flag and in call_event HEALTH rows."""

    NONE = "none"
    SUPERVISOR_REQUESTED = "supervisor_requested"
    REPEATED_QUESTIONS = "repeated_questions"
    HALLUCINATION = "hallucination"
    CONVERSATION_LOOP = "conversation_loop"
    LONG_SILENCE = "long_silence"
    OFF_SCRIPT = "off_script"
    LOW_CONFIDENCE = "low_confidence"
    OTHER = "other"
```

- [ ] **Step 4: Add the columns** — in `call.py`:
  - extend the `sqlalchemy` import with `Integer`;
  - extend the enums import: `from vera_core.models.enums import CallEventType, CallHealthFlag, CallMode, CallStatus, check_in`;
  - inside `__table_args__`, after `check_in("current_status", ...)`: add

```python
        check_in("health_flag", CallHealthFlag, name="health_flag_valid"),
```

  - after the `intervener_claimed_at` column:

```python
    # Latest call-health-observer assessment (denormalized at-a-glance state; the
    # transition history lives in call_event HEALTH rows). Deliberately KEPT after
    # the call ends — last-known health feeds reporting. NULL score = never
    # assessed (renders neutrally, never as 0).
    health_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    health_flag: Mapped[str | None] = mapped_column(String(32), nullable=True)
    health_analyzed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd vera-backend && uv run pytest tests/unit/db/test_call_health_columns.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Create the migration.** Generate the file (alembic assigns the random hex id and current head automatically):

Run: `cd vera-backend && uv run alembic revision -m "call health columns"`

Then replace the generated `upgrade`/`downgrade` bodies (keep the generated `revision`/`down_revision` values) with:

```python
def upgrade() -> None:
    # Idempotent: a fresh DB already has these via 0001's create_all off the live
    # models; only an already-provisioned DB needs the ADDs (repo migration rule).
    op.execute("ALTER TABLE call ADD COLUMN IF NOT EXISTS health_score INTEGER")
    op.execute("ALTER TABLE call ADD COLUMN IF NOT EXISTS health_flag VARCHAR(32)")
    op.execute(
        "ALTER TABLE call ADD COLUMN IF NOT EXISTS health_analyzed_at TIMESTAMP WITH TIME ZONE"
    )
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE call ADD CONSTRAINT ck_call_health_flag_valid CHECK (
                health_flag IN ('none', 'supervisor_requested', 'repeated_questions',
                                'hallucination', 'conversation_loop', 'long_silence',
                                'off_script', 'low_confidence', 'other')
            );
        EXCEPTION WHEN duplicate_object THEN NULL; END $$
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE call DROP CONSTRAINT IF EXISTS ck_call_health_flag_valid")
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS health_analyzed_at")
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS health_flag")
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS health_score")
```

- [ ] **Step 7: Apply locally** (needs `just up` done once):

Run: `cd vera-backend && just migrate`
Expected: `Running upgrade ... -> <hex>, call health columns` (or no-op columns already present on a fresh DB — both fine)

- [ ] **Step 8: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/models/enums.py \
        vera-backend/packages/vera_core/src/vera_core/models/call.py \
        vera-backend/migrations/versions/*call_health_columns.py \
        vera-backend/tests/unit/db/test_call_health_columns.py
git commit -m "feat: call health flag vocabulary + denormalized health columns on call"
```

---

### Task 2: Analyzer contract (`vera_core/call_health.py`) + health settings

**Files:**
- Create: `vera-backend/packages/vera_core/src/vera_core/call_health.py`
- Modify: `vera-backend/packages/vera_core/src/vera_core/config/settings.py` (after the summary block, ~line 163)
- Test: `vera-backend/tests/unit/call_health/test_call_health.py` (+ empty `__init__.py` if sibling dirs have one — they don't; skip)

**Interfaces:**
- Consumes: `CallHealthFlag` (Task 1); `ROLE_DTMF`, `TurnRole`, `TurnSource` from `vera_core.transcript`.
- Produces:
  - `HEALTH_SYSTEM_PROMPT: str` (byte-stable), `HEALTH_USER_SUFFIX: str`
  - `HealthResult(score: int, flag: str, reason: str)` frozen dataclass
  - `parse_assessment(text: str) -> HealthResult | None`
  - `HealthTranscript(max_turns=60)` with `.add(role, source, text)`, `.render_user_message() -> str`, `.turn_count: int`
  - Settings: `health_primary_model`, `health_fallback_models`, `health_attempt_timeout_seconds`, `health_min_interval_seconds`, `health_min_user_turns`, `health_max_turns`

- [ ] **Step 1: Write the failing test**

```python
# vera-backend/tests/unit/call_health/test_call_health.py
"""Analyzer contract: JSON parsing/coercion, unassessable no-op, and the
prefix-stable chunked re-anchoring transcript window (prompt-cache rules)."""

from vera_core.call_health import (
    HEALTH_SYSTEM_PROMPT,
    HEALTH_USER_SUFFIX,
    HealthTranscript,
    parse_assessment,
)


def test_parse_assessable_result() -> None:
    result = parse_assessment(
        '{"assessable": true, "call_health_score": 78, '
        '"intervention_flag": "none", "reason": "going fine"}'
    )
    assert result is not None
    assert (result.score, result.flag, result.reason) == (78, "none", "going fine")


def test_parse_unassessable_is_none() -> None:
    assert parse_assessment('{"assessable": false}') is None


def test_parse_strips_markdown_fences() -> None:
    result = parse_assessment(
        '```json\n{"assessable": true, "call_health_score": 40, '
        '"intervention_flag": "conversation_loop", "reason": "loop"}\n```'
    )
    assert result is not None
    assert result.flag == "conversation_loop"


def test_parse_clamps_score_and_coerces_unknown_flag() -> None:
    result = parse_assessment(
        '{"assessable": true, "call_health_score": 250, '
        '"intervention_flag": "stuck_in_loop", "reason": "?"}'
    )
    assert result is not None
    assert result.score == 100
    assert result.flag == "other"  # unknown vocabulary coerces, never propagates


def test_parse_missing_flag_reads_as_none() -> None:
    result = parse_assessment('{"assessable": true, "call_health_score": 90}')
    assert result is not None
    assert result.flag == "none"


def test_parse_assessable_without_score_is_none() -> None:
    assert parse_assessment('{"assessable": true, "intervention_flag": "none"}') is None


def test_parse_garbage_is_none() -> None:
    assert parse_assessment("I think the call is fine!") is None


def test_render_is_prefix_stable_while_under_the_cap() -> None:
    a, b = HealthTranscript(max_turns=60), HealthTranscript(max_turns=60)
    for i in range(10):
        a.add("agent", "bot", f"question {i}")
        a.add("user", "rep", f"answer {i}")
        b.add("agent", "bot", f"question {i}")
        b.add("user", "rep", f"answer {i}")
    shorter = a.render_user_message().removesuffix(HEALTH_USER_SUFFIX)
    b.add("user", "rep", "one more")
    longer = b.render_user_message().removesuffix(HEALTH_USER_SUFFIX)
    assert longer.startswith(shorter)  # cacheable prefix grows append-only


def test_window_reanchors_in_chunks_not_per_turn() -> None:
    t = HealthTranscript(max_turns=60)
    for i in range(61):  # one past the cap -> single truncation to newest 40
        t.add("user", "rep", f"turn {i}")
    assert t.turn_count == 40
    t.add("user", "rep", "turn 61")
    assert t.turn_count == 41  # grows again; no per-request sliding


def test_dtmf_turn_labelled_as_keypad() -> None:
    t = HealthTranscript(max_turns=60)
    t.add("dtmf", "bot", "3")
    assert "Vera (agent) [keypad]: 3" in t.render_user_message()


def test_health_settings_defaults() -> None:
    from vera_core.config.settings import Settings

    s = Settings(_env_file=None)
    assert s.health_primary_model == "google:gemini-3.1-flash-lite"
    assert s.health_fallback_models == ["openai:gpt-5.4-mini"]
    assert s.health_min_interval_seconds == 15.0
    assert s.health_min_user_turns == 2
    assert s.health_max_turns == 60
    assert s.health_attempt_timeout_seconds == 8.0
```

- [ ] **Step 2: Run to verify failure**

Run: `cd vera-backend && uv run pytest tests/unit/call_health/ -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vera_core.call_health'`

- [ ] **Step 3: Implement `vera_core/call_health.py`**

```python
"""Call-health analyzer contract — shared vocabulary between the agent worker's
observer (which runs the LLM analysis) and the control plane's consumer (which
persists the result).

Prompt-cache rules (spec §4.2): the system prompt is BYTE-IDENTICAL for every
analysis of every call, the transcript renders append-only (a turn, once
rendered, never changes), and anything dynamic goes AFTER the transcript — so
successive analyses of one call share a growing identical prefix that Vertex
Gemini / OpenAI implicit prompt caching discounts. The window is bounded by
chunked re-anchoring, not per-request sliding, for the same reason.

PHI: transcript text and the LLM's `reason` are PHI — nothing here logs content
(parse failures log exception type names only).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from pydantic import BaseModel

from vera_core.models.enums import CallHealthFlag, values_of
from vera_core.transcript import ROLE_DTMF, TurnRole, TurnSource

logger = logging.getLogger(__name__)

HEALTH_SYSTEM_PROMPT = """\
Given the ongoing conversation transcription so far, analyse whether the call
can be completed fully by the bot agent. Give a call health score from 0 to 100
and categorize whether a supervisor intervention is needed or the bot can
continue and finish the call itself.

Rules:
- Early automated IVR/phone-menu navigation is normal and must never be flagged
  as a conversation loop.
- If the conversation so far is insufficient to judge, return
  {"assessable": false} - never guess a low score to express uncertainty.
- A low score must always mean the call is going badly, never "unsure".
- Do not converse. Respond with ONLY a JSON object, no markdown fences, in
  exactly one of these shapes:
  {"assessable": false}
  {"assessable": true, "call_health_score": 78, "intervention_flag": "<flag>",
   "reason": "<one short sentence>"}
- intervention_flag must be one of: none, supervisor_requested,
  repeated_questions, hallucination, conversation_loop, long_silence,
  off_script, low_confidence, other."""

# Appended AFTER the transcript in the user message — the dynamic tail must never
# sit ahead of the cacheable transcript prefix.
HEALTH_USER_SUFFIX = "\n\nAssess the call health now. Respond with ONLY the JSON object."

_SPEAKER_LABELS = {"rep": "Payer rep", "bot": "Vera (agent)", "supervisor": "Supervisor"}
# On overflow, truncate once to this many newest turns, then grow back to the cap:
# the prefix stays byte-identical BETWEEN re-anchors (~1 cache miss per 20 turns).
_REANCHOR_KEEP = 40
_MAX_REASON_LEN = 500

_JSON_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
_VALID_FLAGS = frozenset(values_of(CallHealthFlag))


@dataclass(frozen=True)
class HealthResult:
    """One normalized assessable analysis."""

    score: int  # clamped to 0-100
    flag: str  # a CallHealthFlag value
    reason: str  # PHI — never log


class _RawAssessment(BaseModel):
    """The LLM's JSON contract, before normalization."""

    assessable: bool = True
    call_health_score: float | None = None
    intervention_flag: str | None = None
    reason: str | None = None


def parse_assessment(text: str) -> HealthResult | None:
    """LLM reply -> normalized result. None means "no result this cycle" — the
    model said `assessable: false`, omitted the score, or ignored the contract;
    all three are a complete no-op for the observer (a low score must always
    mean "going badly", never "could not parse"). Unknown flags coerce to
    `other`; a missing flag reads as `none`."""
    raw = text.strip()
    fenced = _JSON_FENCE.match(raw)
    if fenced:
        raw = fenced.group(1)
    try:
        parsed = _RawAssessment.model_validate_json(raw)
    except Exception as exc:  # the reply is PHI — type name only
        logger.warning("health assessment parse failed: %s", type(exc).__name__)
        return None
    if not parsed.assessable or parsed.call_health_score is None:
        return None
    flag = (parsed.intervention_flag or CallHealthFlag.NONE.value).strip().lower()
    if flag not in _VALID_FLAGS:
        flag = CallHealthFlag.OTHER.value
    score = max(0, min(100, round(parsed.call_health_score)))
    return HealthResult(score=score, flag=flag, reason=(parsed.reason or "")[:_MAX_REASON_LEN])


class HealthTranscript:
    """Bounded, prefix-stable transcript window (see module docstring)."""

    def __init__(self, *, max_turns: int = 60) -> None:
        if max_turns <= _REANCHOR_KEEP:
            raise ValueError(f"max_turns must exceed {_REANCHOR_KEEP}")
        self._max_turns = max_turns
        self._lines: list[str] = []

    @property
    def turn_count(self) -> int:
        return len(self._lines)

    def add(self, role: TurnRole, source: TurnSource, text: str) -> None:
        label = _SPEAKER_LABELS.get(source, source)
        if role == ROLE_DTMF:
            label = f"{label} [keypad]"
        self._lines.append(f"{label}: {text}")
        if len(self._lines) > self._max_turns:
            del self._lines[: len(self._lines) - _REANCHOR_KEEP]

    def render_user_message(self) -> str:
        return "\n".join(self._lines) + HEALTH_USER_SUFFIX
```

- [ ] **Step 4: Add settings** — in `settings.py`, after `summary_total_timeout_seconds` / its validator:

```python
    # --- call-health observer (agent worker) --------------------------------
    # Fault-tolerant analyzer chain, same "provider:model" selector format as the
    # summary chain; runs INSIDE the agent worker as a per-call background task.
    health_primary_model: str = "google:gemini-3.1-flash-lite"  # VERA_HEALTH_PRIMARY_MODEL
    health_fallback_models: list[str] = ["openai:gpt-5.4-mini"]  # VERA_HEALTH_FALLBACK_MODELS
    health_attempt_timeout_seconds: float = 8.0  # VERA_HEALTH_ATTEMPT_TIMEOUT_SECONDS
    # A completed user turn triggers an analysis, at most one in flight and at
    # least this many seconds apart (silence triggers nothing).
    health_min_interval_seconds: float = 15.0  # VERA_HEALTH_MIN_INTERVAL_SECONDS
    # Cold-start gate: no analysis until this many user turns exist.
    health_min_user_turns: int = 2  # VERA_HEALTH_MIN_USER_TURNS
    # Transcript window cap (chunked re-anchoring — see vera_core.call_health).
    health_max_turns: int = 60  # VERA_HEALTH_MAX_TURNS

    @field_validator("health_fallback_models", mode="before")
    @classmethod
    def _split_health_fallback_models(cls, value: object) -> object:
        return _split_csv(value)
```

- [ ] **Step 5: Run tests**

Run: `cd vera-backend && uv run pytest tests/unit/call_health/ -v`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/call_health.py \
        vera-backend/packages/vera_core/src/vera_core/config/settings.py \
        vera-backend/tests/unit/call_health/
git commit -m "feat: call-health analyzer contract (prompt, parser, cache-friendly window) + settings"
```

---

### Task 3: `CallHealthEvent` worker event

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/events/worker.py`
- Modify: `vera-backend/packages/vera_core/src/vera_core/events/__init__.py`
- Test: `vera-backend/tests/unit/events/test_call_health_event.py`

**Interfaces:**
- Produces: `CallHealthEvent(type="call.health", room_name, score: int, flag: str, reason: str, turn_count: int, ts: int)`; parsed by the existing `parse_worker_event`.

- [ ] **Step 1: Write the failing test**

```python
# vera-backend/tests/unit/events/test_call_health_event.py
"""call.health worker event: round-trips through the discriminated adapter."""

import pytest

from vera_core.events import CallHealthEvent, parse_worker_event


def test_call_health_event_roundtrip() -> None:
    ev = CallHealthEvent(
        room_name="call--t--c", score=42, flag="conversation_loop",
        reason="asked the same question three times", turn_count=12, ts=1_720_000_000_000,
    )
    parsed = parse_worker_event(ev.model_dump_json())
    assert isinstance(parsed, CallHealthEvent)
    assert parsed == ev


def test_unknown_type_still_rejected() -> None:
    with pytest.raises(Exception):
        parse_worker_event('{"type": "call.nonsense", "room_name": "x", "ts": 1}')
```

- [ ] **Step 2: Run to verify failure**

Run: `cd vera-backend && uv run pytest tests/unit/events/test_call_health_event.py -v`
Expected: FAIL — `ImportError: cannot import name 'CallHealthEvent'`

- [ ] **Step 3: Implement.** In `events/worker.py`:

Amend the module docstring's last sentence (`Events are PHI-free by construction: ...`) to:

```
events (call failures, and the answered/ended call-status transitions that
drive the consumer's closeout) to the control plane. Lifecycle events are
PHI-free by construction (a room_name, an enum, a timestamp). The one
exception is CallHealthEvent, whose `reason` sentence is derived from the
conversation (PHI): the stream is in-boundary Redis (CMEK at rest) so it may
carry it, but no handler or log line may ever echo it — type names and room
names only.
```

After `CallEndedEvent`, add:

```python
class CallHealthEvent(BaseModel):
    """Emitted by the worker's call-health observer after each assessable
    analysis. `reason` is PHI (see module docstring) — never log it."""

    type: Literal["call.health"] = "call.health"
    room_name: str
    score: int  # 0-100 (clamped at the producer)
    flag: str  # a CallHealthFlag value ("none" = healthy)
    reason: str  # PHI — never log
    turn_count: int
    ts: int  # analyzed_at, epoch milliseconds — the consumer's idempotency key
```

Update the union + adapter:

```python
type WorkerEvent = CallFailedEvent | CallAnsweredEvent | CallEndedEvent | CallHealthEvent
_ADAPTER: TypeAdapter[WorkerEvent] = TypeAdapter(
    Annotated[
        CallFailedEvent | CallAnsweredEvent | CallEndedEvent | CallHealthEvent,
        Field(discriminator="type"),
    ]
)
```

In `events/__init__.py`, add `CallHealthEvent` to the import and `__all__` (alphabetical position after `CallFailureReason`... keep the existing ordering style: insert `CallHealthEvent` after `CallFailedEvent`).

- [ ] **Step 4: Run tests**

Run: `cd vera-backend && uv run pytest tests/unit/events/ -v`
Expected: PASS (existing + new)

- [ ] **Step 5: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/events/
git add vera-backend/tests/unit/events/test_call_health_event.py
git commit -m "feat: call.health worker event"
```

---

### Task 4: `TYPE_HEALTH` envelope on the per-call stream

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/call_stream.py`
- Test: `vera-backend/tests/unit/transcript/test_call_stream_health.py`

**Interfaces:**
- Produces: `TYPE_HEALTH = "health"`; `CallStreamService.publish_health(room_name, *, score: int, flag: str, reason: str, ts: int)`.

- [ ] **Step 1: Write the failing test**

```python
# vera-backend/tests/unit/transcript/test_call_stream_health.py
"""The health envelope rides the same per-call stream as transcript/status."""

import pytest

from vera_core.call_stream import TYPE_HEALTH, CallStreamEvent, CallStreamService


class _SpyStore:
    def __init__(self) -> None:
        self.published: list[tuple[str, CallStreamEvent]] = []

    async def publish(self, room_name: str, event: CallStreamEvent) -> None:
        self.published.append((room_name, event))


@pytest.mark.asyncio
async def test_publish_health_envelope() -> None:
    store = _SpyStore()
    service = CallStreamService(store)  # type: ignore[arg-type]
    await service.publish_health("room-1", score=35, flag="long_silence", reason="hold", ts=99)
    [(room, event)] = store.published
    assert room == "room-1"
    assert event.type == TYPE_HEALTH == "health"
    assert event.data == {"score": 35, "flag": "long_silence", "reason": "hold"}
    assert event.ts == 99
```

- [ ] **Step 2: Run to verify failure**

Run: `cd vera-backend && uv run pytest tests/unit/transcript/test_call_stream_health.py -v`
Expected: FAIL — `ImportError: cannot import name 'TYPE_HEALTH'`

- [ ] **Step 3: Implement.** In `call_stream.py`:
  - after `TYPE_CALL_STATUS = "call_status"` add `TYPE_HEALTH = "health"`;
  - in `CallStreamService`, after `publish_status`, add:

```python
    async def publish_health(
        self, room_name: str, *, score: int, flag: str, reason: str, ts: int
    ) -> None:
        """Publish one call-health-observer assessment frame (spec: rides the
        same /calls/{id}/events SSE — no new pipe per event type)."""
        await self._store.publish(
            room_name,
            CallStreamEvent(
                type=TYPE_HEALTH, data={"score": score, "flag": flag, "reason": reason}, ts=ts
            ),
        )
```

  - Also update the `CallStreamEvent.type` comment: `# "transcript" | "call_status" | "health" | future types`.

- [ ] **Step 4: Run tests**

Run: `cd vera-backend && uv run pytest tests/unit/transcript/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/call_stream.py \
        vera-backend/tests/unit/transcript/test_call_stream_health.py
git commit -m "feat: health envelope type on the per-call event stream"
```

---

### Task 5: `NotificationService` (per-tenant Redis stream)

**Files:**
- Create: `vera-backend/packages/vera_core/src/vera_core/notifications.py`
- Test: `vera-backend/tests/unit/notifications/test_notifications.py`

**Interfaces:**
- Produces: `Notification(type, audience: NotificationAudience, data: dict, ts: int)`; `NotificationAudience(kind: "user"|"tenant", user_id: str | None)`; `TYPE_INTERVENTION_NEEDED = "intervention_needed"`; `RedisNotificationStore(redis, *, maxlen=1000, ttl_seconds=86_400, block_ms=5000)`; `NotificationService(store)` with `await publish(tenant_id, notification)` and `tail(tenant_id) -> AsyncIterator[tuple[str, Notification] | None]` (None = idle keepalive tick, same contract as `RedisCallStreamStore.read`).

- [ ] **Step 1: Write the failing test**

```python
# vera-backend/tests/unit/notifications/test_notifications.py
"""Per-tenant notification stream: publish shape, tail-from-now anchor, idle
keepalive ticks (redis.asyncio BLOCK reads RAISE TimeoutError — repo footgun)."""

import json
from typing import Any
from uuid import uuid4

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from vera_core.notifications import (
    TYPE_INTERVENTION_NEEDED,
    Notification,
    NotificationAudience,
    NotificationService,
    RedisNotificationStore,
    notify_stream_key,
)


class _FakePipe:
    def __init__(self, redis: "_FakeRedis") -> None:
        self._redis = redis
        self._ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def xadd(self, *args: Any, **kwargs: Any) -> None:
        self._ops.append(("xadd", args, kwargs))

    def expire(self, *args: Any, **kwargs: Any) -> None:
        self._ops.append(("expire", args, kwargs))

    async def execute(self) -> None:
        self._redis.ops.extend(self._ops)


class _FakeRedis:
    def __init__(self) -> None:
        self.ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.xrevrange_result: list[tuple[str, dict[str, str]]] = []
        # Each item: an exception to raise, or an xread response to return.
        self.xread_script: list[Any] = []

    def pipeline(self, transaction: bool = False) -> _FakePipe:
        return _FakePipe(self)

    async def xrevrange(self, key: str, max: str, min: str, count: int) -> Any:
        return self.xrevrange_result

    async def xread(self, streams: dict[str, str], block: int) -> Any:
        self.ops.append(("xread", (dict(streams),), {"block": block}))
        item = self.xread_script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _notification() -> Notification:
    return Notification(
        type=TYPE_INTERVENTION_NEEDED,
        audience=NotificationAudience(kind="tenant"),
        data={"call_id": "c1", "score": 30, "flag": "conversation_loop", "reason": "r"},
        ts=123,
    )


@pytest.mark.asyncio
async def test_publish_xadds_with_maxlen_and_ttl() -> None:
    redis = _FakeRedis()
    tenant_id = uuid4()
    service = NotificationService(RedisNotificationStore(redis))  # type: ignore[arg-type]
    await service.publish(tenant_id, _notification())
    kinds = [op[0] for op in redis.ops]
    assert kinds == ["xadd", "expire"]
    _, xadd_args, xadd_kwargs = redis.ops[0]
    assert xadd_args[0] == notify_stream_key(tenant_id)
    assert json.loads(xadd_args[1]["n"])["type"] == TYPE_INTERVENTION_NEEDED
    assert xadd_kwargs == {"maxlen": 1000, "approximate": True}


@pytest.mark.asyncio
async def test_tail_anchors_past_existing_entries_and_ticks_on_idle() -> None:
    redis = _FakeRedis()
    tenant_id = uuid4()
    key = notify_stream_key(tenant_id)
    redis.xrevrange_result = [("5-1", {"n": _notification().model_dump_json()})]
    redis.xread_script = [
        RedisTimeoutError(),  # idle BLOCK window -> keepalive tick
        [(key, [("6-1", {"n": _notification().model_dump_json()})])],
    ]
    service = NotificationService(RedisNotificationStore(redis))  # type: ignore[arg-type]
    it = service.tail(tenant_id)
    assert await anext(it) is None  # the idle tick
    item = await anext(it)
    assert item is not None
    entry_id, n = item
    assert entry_id == "6-1"
    assert n.audience.kind == "tenant"
    # Anchor: the first xread must start AFTER the pre-existing entry, not at 0/"$".
    first_xread = next(op for op in redis.ops if op[0] == "xread")
    assert first_xread[1][0] == {key: "5-1"}


@pytest.mark.asyncio
async def test_tail_skips_malformed_entries() -> None:
    redis = _FakeRedis()
    tenant_id = uuid4()
    key = notify_stream_key(tenant_id)
    redis.xread_script = [
        [(key, [("1-1", {"n": "not json"}), ("1-2", {"n": _notification().model_dump_json()})])],
    ]
    service = NotificationService(RedisNotificationStore(redis))  # type: ignore[arg-type]
    item = await anext(service.tail(tenant_id))
    assert item is not None and item[0] == "1-2"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd vera-backend && uv run pytest tests/unit/notifications/ -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vera_core.notifications'`

- [ ] **Step 3: Implement `vera_core/notifications.py`**

```python
"""User-scoped realtime notifications over a per-tenant Redis stream.

The control plane publishes (the worker-event consumer's intervention alerts
today; anything user-facing later) and GET /notifications/stream tails.
Delivery is fan-out-with-filtering: every notification carries an `audience`
(one user, or the whole tenant) and each authenticated SSE connection forwards
only what is addressed to its user. Ephemeral by design: no DB persistence,
MAXLEN-trimmed, rolling TTL — a reconnecting client starts from "now" and
recovers current state from the REST API (the stream is an accelerant, never
the source of truth).

PHI: `data` may carry the health observer's `reason` sentence, delivered only
to users already authorized to read the call (owner, or tenant users for a
published call — the same owner-or-published rule as call visibility). Content
is never logged here (type names only).
"""

import logging
from collections.abc import AsyncIterator
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError

logger = logging.getLogger(__name__)

_KEY_PREFIX = "vera:notify:"
_FIELD = "n"

TYPE_INTERVENTION_NEEDED = "intervention_needed"


def notify_stream_key(tenant_id: UUID) -> str:
    return f"{_KEY_PREFIX}{tenant_id}"


class NotificationAudience(BaseModel):
    """Who a notification is addressed to. kind="user" requires user_id."""

    kind: Literal["user", "tenant"]
    user_id: str | None = None


class Notification(BaseModel):
    type: str  # "intervention_needed" | future types
    audience: NotificationAudience
    data: dict[str, Any]
    ts: int  # epoch milliseconds


class RedisNotificationStore:
    """Redis Streams transport. MAXLEN bounds per-tenant memory; the rolling TTL
    self-clears idle tenants' streams."""

    def __init__(
        self, redis: Redis, *, maxlen: int = 1000, ttl_seconds: int = 86_400, block_ms: int = 5000
    ) -> None:
        self._redis = redis
        self._maxlen = maxlen
        self._ttl_seconds = ttl_seconds
        self._block_ms = block_ms

    async def publish(self, tenant_id: UUID, notification: Notification) -> None:
        key = notify_stream_key(tenant_id)
        pipe = self._redis.pipeline(transaction=False)
        pipe.xadd(
            key, {_FIELD: notification.model_dump_json()}, maxlen=self._maxlen, approximate=True
        )
        pipe.expire(key, self._ttl_seconds)
        await pipe.execute()

    async def tail(self, tenant_id: UUID) -> AsyncIterator[tuple[str, Notification] | None]:
        """Tail from "now". Yields None on every idle BLOCK window so the SSE
        endpoint can emit a keepalive (same contract as RedisCallStreamStore.read).

        The anchor id is resolved ONCE via XREVRANGE and advanced per entry —
        re-issuing XREAD with "$" on every tick would silently drop entries
        published between two ticks."""
        key = notify_stream_key(tenant_id)
        newest = await self._redis.xrevrange(key, "+", "-", count=1)
        anchored = cast("list[tuple[str, dict[str, str]]]", newest or [])
        last_id = anchored[0][0] if anchored else "0-0"
        while True:
            try:
                result = await self._redis.xread({key: last_id}, block=self._block_ms)
            except RedisTimeoutError:
                # BLOCK with no entries RAISES (per-command read deadline) — idle tick.
                result = None
            if not result:
                yield None
                continue
            entries = cast("list[tuple[str, list[tuple[str, dict[str, str]]]]]", result)[0][1]
            for entry_id, fields in entries:
                last_id = entry_id
                raw = fields.get(_FIELD)
                if raw is None:
                    continue
                try:
                    yield entry_id, Notification.model_validate_json(raw)
                except Exception as exc:  # content may be PHI — type name only
                    logger.warning("skipping malformed notification (%s)", type(exc).__name__)


class NotificationService:
    """Produce/consume surface — no caller touches raw Redis (mirrors
    CallStreamService)."""

    def __init__(self, store: RedisNotificationStore) -> None:
        self._store = store

    async def publish(self, tenant_id: UUID, notification: Notification) -> None:
        await self._store.publish(tenant_id, notification)

    def tail(self, tenant_id: UUID) -> AsyncIterator[tuple[str, Notification] | None]:
        return self._store.tail(tenant_id)
```

- [ ] **Step 4: Run tests**

Run: `cd vera-backend && uv run pytest tests/unit/notifications/ -v`
Expected: PASS (3)

- [ ] **Step 5: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/notifications.py \
        vera-backend/tests/unit/notifications/
git commit -m "feat: per-tenant user-scoped notification stream service"
```

---

### Task 6: `CallHealthObserver` (agent worker)

**Files:**
- Create: `vera-backend/apps/agent_worker/src/agent_worker/health_observer.py`
- Test: `vera-backend/tests/unit/worker/test_health_observer.py`

**Interfaces:**
- Consumes: `HealthTranscript`, `parse_assessment`, `HEALTH_SYSTEM_PROMPT` (Task 2); `CallHealthEvent` + `WorkerEventBus` (Task 3); `CallStreamService.publish_health` (Task 4); `takeover_engaged(session)` from `agent_worker.intervention`; `ResilientLLM`/`LLMSpec`/`FallbackOptions` + `EnvSecretProvider`.
- Produces:
  - `CallHealthObserver` — implements the `TurnPublisher` protocol (`async publish_turn(room_name, role, text, *, ts, source=None)`), plus `await aclose()`.
  - `build_health_observer(session, *, room_name, settings, call_stream, bus) -> CallHealthObserver` — the factory `main.py` wires (Task 7).

- [ ] **Step 1: Write the failing test**

```python
# vera-backend/tests/unit/worker/test_health_observer.py
"""CallHealthObserver: user-turn trigger, cold-start gate, cooldown coalescing,
single-in-flight, takeover stop (pre-start AND pre-emit), unassessable no-op,
LLM-failure skip, shutdown cancellation. The cascade must never notice it."""

import asyncio
from typing import Any

import pytest

from agent_worker.health_observer import CallHealthObserver
from vera_core.call_health import HealthTranscript
from vera_core.events import CallHealthEvent


class _FakeLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None  # when set, complete() blocks on it
        self.closed = False

    async def complete(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.reply

    async def aclose(self) -> None:
        self.closed = True


class _FakeCallStream:
    def __init__(self) -> None:
        self.health: list[dict[str, Any]] = []

    async def publish_health(
        self, room_name: str, *, score: int, flag: str, reason: str, ts: int
    ) -> None:
        self.health.append({"room": room_name, "score": score, "flag": flag, "reason": reason})


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[CallHealthEvent] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


_OK_REPLY = (
    '{"assessable": true, "call_health_score": 80, "intervention_flag": "none", "reason": "ok"}'
)


def _observer(
    llm: _FakeLLM,
    stream: _FakeCallStream,
    bus: _FakeBus,
    *,
    engaged: bool = False,
    min_interval_s: float = 0.0,
    min_user_turns: int = 2,
) -> tuple[CallHealthObserver, dict[str, bool]]:
    state = {"engaged": engaged}
    obs = CallHealthObserver(
        room_name="room-x",
        llm=llm,
        call_stream=stream,  # type: ignore[arg-type]
        bus=bus,  # type: ignore[arg-type]
        engaged=lambda: state["engaged"],
        transcript=HealthTranscript(max_turns=60),
        min_user_turns=min_user_turns,
        min_interval_s=min_interval_s,
    )
    return obs, state


async def _feed(obs: CallHealthObserver, *turns: tuple[str, str]) -> None:
    for role, text in turns:
        await obs.publish_turn("room-x", role, text, ts=1)  # type: ignore[arg-type]


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_analyzes_after_min_user_turns_and_emits_both_rails() -> None:
    llm, stream, bus = _FakeLLM(_OK_REPLY), _FakeCallStream(), _FakeBus()
    obs, _ = _observer(llm, stream, bus)
    await _feed(obs, ("agent", "hello"), ("user", "hi"))
    await _settle()
    assert llm.calls == []  # 1 user turn < min_user_turns=2 — cold-start gate
    await _feed(obs, ("agent", "name?"), ("user", "jane"))
    await _settle()
    assert len(llm.calls) == 1
    assert stream.health and stream.health[0]["score"] == 80
    assert bus.events and bus.events[0].flag == "none" and bus.events[0].turn_count == 4
    await obs.aclose()
    assert llm.closed


@pytest.mark.asyncio
async def test_cooldown_coalesces_turn_burst_into_one_deferred_run() -> None:
    llm, stream, bus = _FakeLLM(_OK_REPLY), _FakeCallStream(), _FakeBus()
    obs, _ = _observer(llm, stream, bus, min_interval_s=0.15)
    await _feed(obs, ("agent", "a"), ("user", "b"), ("agent", "c"), ("user", "d"))
    await _settle()  # ~0.2s
    first_count = len(llm.calls)
    assert first_count == 1  # first run immediate
    await _feed(obs, ("user", "e"), ("user", "f"), ("user", "g"))  # burst inside cooldown
    await asyncio.sleep(0.3)
    assert len(llm.calls) == 2  # exactly ONE deferred run for the whole burst
    await obs.aclose()


@pytest.mark.asyncio
async def test_takeover_stops_before_start_and_before_emit() -> None:
    # Pre-start: engaged before any analysis -> zero LLM calls, task exits.
    llm, stream, bus = _FakeLLM(_OK_REPLY), _FakeCallStream(), _FakeBus()
    obs, _ = _observer(llm, stream, bus, engaged=True)
    await _feed(obs, ("agent", "a"), ("user", "b"), ("agent", "c"), ("user", "d"))
    await _settle()
    assert llm.calls == [] and stream.health == [] and bus.events == []
    await obs.aclose()

    # Pre-emit: takeover lands while the LLM call is in flight -> result discarded.
    llm2, stream2, bus2 = _FakeLLM(_OK_REPLY), _FakeCallStream(), _FakeBus()
    llm2.gate = asyncio.Event()
    obs2, state2 = _observer(llm2, stream2, bus2)
    await _feed(obs2, ("agent", "a"), ("user", "b"), ("agent", "c"), ("user", "d"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if llm2.calls:
            break
    state2["engaged"] = True  # supervisor takes over mid-analysis
    llm2.gate.set()
    await _settle()
    assert stream2.health == [] and bus2.events == []
    await obs2.aclose()


@pytest.mark.asyncio
async def test_unassessable_and_llm_failure_are_silent_no_ops() -> None:
    llm, stream, bus = _FakeLLM('{"assessable": false}'), _FakeCallStream(), _FakeBus()
    obs, _ = _observer(llm, stream, bus)
    await _feed(obs, ("agent", "a"), ("user", "b"), ("agent", "c"), ("user", "d"))
    await _settle()
    assert len(llm.calls) == 1 and stream.health == [] and bus.events == []
    await obs.aclose()

    llm2, stream2, bus2 = _FakeLLM(_OK_REPLY), _FakeCallStream(), _FakeBus()
    llm2.error = RuntimeError("providers down")
    obs2, _ = _observer(llm2, stream2, bus2)
    await _feed(obs2, ("agent", "a"), ("user", "b"), ("agent", "c"), ("user", "d"))
    await _settle()  # must not raise anywhere
    assert stream2.health == [] and bus2.events == []
    await obs2.aclose()


@pytest.mark.asyncio
async def test_aclose_cancels_inflight_analysis() -> None:
    llm, stream, bus = _FakeLLM(_OK_REPLY), _FakeCallStream(), _FakeBus()
    llm.gate = asyncio.Event()  # never set — the LLM call hangs
    obs, _ = _observer(llm, stream, bus)
    await _feed(obs, ("agent", "a"), ("user", "b"), ("agent", "c"), ("user", "d"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if llm.calls:
            break
    await obs.aclose()  # must return promptly, not hang on the in-flight call
    assert stream.health == [] and bus.events == []
```

- [ ] **Step 2: Run to verify failure**

Run: `cd vera-backend && uv run pytest tests/unit/worker/test_health_observer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_worker.health_observer'`

- [ ] **Step 3: Implement `health_observer.py`**

```python
"""Concurrent per-call health observer — never blocks the cascade.

An extra TurnPublisher sink on the job's fan-out: it accumulates the ordered
turn stream in a bounded, cache-friendly window and, on each completed USER
turn (bot spoke, rep replied), runs at most one in-flight LLM analysis with a
minimum interval between runs. Assessable results go out on two rails: a
`health` envelope on the per-call event stream (the live SSE) and a
`call.health` worker event (the control plane persists it). Everything here is
best-effort — an LLM outage or Redis failure logs a type name and skips the
cycle; the call never notices.

Stops permanently the moment a supervisor takeover engages (checked before
starting a run AND again before emitting a completed one — an in-flight result
must not land after a human stepped in), and is cancelled at job shutdown.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from agent_worker.intervention import takeover_engaged
from vera_core.call_health import HEALTH_SYSTEM_PROMPT, HealthTranscript, parse_assessment
from vera_core.call_stream import CallStreamService
from vera_core.config import EnvSecretProvider, Settings
from vera_core.events import CallHealthEvent, WorkerEventBus
from vera_core.llm import FallbackOptions, LLMSpec, ResilientLLM
from vera_core.transcript import ROLE_AGENT, ROLE_USER, TurnRole, TurnSource, source_for_role

logger = logging.getLogger("agent_worker")


class _HealthLLM(Protocol):
    """Structural view of ResilientLLM (keeps tests decoupled)."""

    async def complete(self, *, system: str, user: str) -> str: ...
    async def aclose(self) -> None: ...


class CallHealthObserver:
    """See module docstring. Owns its LLM chain (aclose closes it)."""

    def __init__(
        self,
        *,
        room_name: str,
        llm: _HealthLLM,
        call_stream: CallStreamService,
        bus: WorkerEventBus,
        engaged: Callable[[], bool],
        transcript: HealthTranscript,
        min_user_turns: int,
        min_interval_s: float,
    ) -> None:
        self._room = room_name
        self._llm = llm
        self._call_stream = call_stream
        self._bus = bus
        self._engaged = engaged
        self._transcript = transcript
        self._min_user_turns = min_user_turns
        self._min_interval_s = min_interval_s
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._closed = False
        self._last_run = float("-inf")
        self._user_turns = 0
        self._agent_turns = 0
        self._task: asyncio.Task[None] = self._loop.create_task(self._run())

    # --- TurnPublisher sink (called by the fan-out; must stay cheap) ---

    async def publish_turn(
        self,
        room_name: str,
        role: TurnRole,
        text: str,
        *,
        ts: int,
        source: TurnSource | None = None,
    ) -> None:
        if self._closed:
            return
        self._transcript.add(role, source or source_for_role(role), text)
        if role == ROLE_AGENT:
            self._agent_turns += 1
        if role == ROLE_USER:
            self._user_turns += 1
            # Trigger only on a completed exchange (bot spoke AND the rep has
            # replied enough times) — the cold-start gate (spec edge #11).
            if self._user_turns >= self._min_user_turns and self._agent_turns >= 1:
                self._wake.set()

    # --- analysis loop ---

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            # Cooldown: a turn burst inside the window coalesces into exactly one
            # deferred run (the event stays set until cleared below).
            wait_s = self._last_run + self._min_interval_s - self._loop.time()
            if wait_s > 0:
                await asyncio.sleep(wait_s)
            self._wake.clear()
            if self._closed:
                return
            if self._engaged():
                logger.info("health observer for %s stopping: takeover engaged", self._room)
                return  # permanent — its purpose is handing off to a human
            self._last_run = self._loop.time()
            await self._analyze_once()

    async def _analyze_once(self) -> None:
        user_message = self._transcript.render_user_message()
        turn_count = self._transcript.turn_count
        try:
            reply = await self._llm.complete(system=HEALTH_SYSTEM_PROMPT, user=user_message)
        except Exception as exc:  # prompt/reply are PHI — type name only
            logger.warning("health analysis for %s skipped (%s)", self._room, type(exc).__name__)
            return
        result = parse_assessment(reply)
        if result is None:
            return  # unassessable / contract-ignoring reply — complete no-op
        if self._closed or self._engaged():
            return  # a late result must never land after shutdown/takeover
        ts = int(time.time() * 1000)
        try:
            await self._call_stream.publish_health(
                self._room, score=result.score, flag=result.flag, reason=result.reason, ts=ts
            )
        except Exception as exc:
            logger.warning("health frame publish failed for %s (%s)", self._room, type(exc).__name__)
        try:
            await self._bus.emit(
                CallHealthEvent(
                    room_name=self._room,
                    score=result.score,
                    flag=result.flag,
                    reason=result.reason,
                    turn_count=turn_count,
                    ts=ts,
                )
            )
        except Exception as exc:
            logger.warning("health event emit failed for %s (%s)", self._room, type(exc).__name__)

    async def aclose(self) -> None:
        """Idempotent: stop the loop (cancelling any in-flight analysis) and
        close the owned LLM chain."""
        if self._closed:
            return
        self._closed = True
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        with contextlib.suppress(Exception):
            await self._llm.aclose()


def build_health_observer(
    session: Any,
    *,
    room_name: str,
    settings: Settings,
    call_stream: CallStreamService,
    bus: WorkerEventBus,
) -> CallHealthObserver:
    """Wire an observer for a real /calls job. `session` is the AgentSession —
    its TakeoverState userdata is the stop signal. The LLM chain is per-job and
    lazy (no provider client until the first analysis)."""
    llm = ResilientLLM(
        LLMSpec.parse(settings.health_primary_model),
        [LLMSpec.parse(selector) for selector in settings.health_fallback_models],
        options=FallbackOptions(attempt_timeout=settings.health_attempt_timeout_seconds),
        secrets=EnvSecretProvider(),
    )
    return CallHealthObserver(
        room_name=room_name,
        llm=llm,
        call_stream=call_stream,
        bus=bus,
        engaged=lambda: takeover_engaged(session),
        transcript=HealthTranscript(max_turns=settings.health_max_turns),
        min_user_turns=settings.health_min_user_turns,
        min_interval_s=settings.health_min_interval_seconds,
    )
```

Note: `EnvSecretProvider` and `Settings` import from `vera_core.config` — confirm the re-export exists (control-plane `main.py` imports `EnvSecretProvider, SecretProvider, Settings` from `vera_core.config`); if mypy complains, import from the concrete submodules instead.

- [ ] **Step 4: Run tests**

Run: `cd vera-backend && uv run pytest tests/unit/worker/test_health_observer.py -v`
Expected: PASS (5)

- [ ] **Step 5: Commit**

```bash
git add vera-backend/apps/agent_worker/src/agent_worker/health_observer.py \
        vera-backend/tests/unit/worker/test_health_observer.py
git commit -m "feat: per-call health observer in the agent worker"
```

---

### Task 7: Wire the observer into the worker entrypoint

**Files:**
- Modify: `vera-backend/apps/agent_worker/src/agent_worker/main.py`

**Interfaces:**
- Consumes: `build_health_observer`, `CallHealthObserver` (Task 6).
- Produces: observer attached as a fan-out sink for real `/calls` jobs (`publish_events` metadata AND a worker-event bus present); closed in `_on_shutdown` and on setup failure.

No new unit test (the entrypoint has no direct tests; behavior is covered by Task 6 units + Task 11 boot verification).

- [ ] **Step 1: Add the import** (with the other `agent_worker.*` imports):

```python
from agent_worker.health_observer import CallHealthObserver, build_health_observer
```

- [ ] **Step 2: Declare for the failure path.** Next to `call_stream_redis: Redis | None = None` / `plan_redis` (before the `try:`, ~line 319):

```python
    health_observer: CallHealthObserver | None = None
```

- [ ] **Step 3: Create + register as a sink.** Immediately after the `call_stream` block (after line ~434, before the `sinks` assembly), replace the sinks assembly with:

```python
        # Call-health observer (real /calls flow only: needs both the per-call
        # event stream and the worker-event bus). An extra fan-out sink — it sees
        # the same ordered turns as every other stream and never blocks them.
        if call_stream is not None and bus is not None:
            health_observer = build_health_observer(
                session, room_name=room_name, settings=settings, call_stream=call_stream, bus=bus
            )

        # One reordering emitter fanned out to every enabled sink — the barge-in reorder
        # state machine lives once per job, not once per stream (see transcript_publisher).
        sinks: list[TurnPublisher] = [
            svc for svc in (transcript_service, call_stream, health_observer) if svc is not None
        ]
```

(The existing comment block stays; only the tuple gains `health_observer`.)

- [ ] **Step 4: Shutdown teardown.** In `_on_shutdown`, after the `takeover_transcriber` close and before `await _end_transcript_stream()`:

```python
            if health_observer is not None:
                try:
                    await health_observer.aclose()  # before the call stream ends
                except Exception:
                    logger.exception("failed to close health observer for %s", room_name)
```

- [ ] **Step 5: Setup-failure cleanup.** In the `except BaseException:` block (alongside the redis closes):

```python
        if health_observer is not None:
            with contextlib.suppress(Exception):
                await health_observer.aclose()
```

- [ ] **Step 6: Run the worker test suite + typecheck**

Run: `cd vera-backend && uv run pytest tests/unit/worker/ tests/unit/agent_worker/ apps/agent_worker/tests -v && uv run mypy`
Expected: PASS / no new mypy errors

- [ ] **Step 7: Commit**

```bash
git add vera-backend/apps/agent_worker/src/agent_worker/main.py
git commit -m "feat: attach the call-health observer to real-call jobs"
```

---

### Task 8: Control-plane `call.health` handler (episode state machine)

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/worker_events.py`
- Test: `vera-backend/tests/unit/control_plane/test_call_health_handler.py`
- Modify (guard): `_handle_call_answered` in the same file
- Test (guards): extend `vera-backend/tests/unit/control_plane/test_worker_events.py`

**Interfaces:**
- Consumes: `CallHealthEvent` (Task 3), `CallHealthFlag` (Task 1), `Notification`/`NotificationAudience`/`NotificationService`/`TYPE_INTERVENTION_NEEDED` (Task 5).
- Produces: `WorkerEventConsumer(..., notifications: NotificationService | None = None)`; handler registered for `"call.health"`. Semantics (spec §4.3): guards (no row → drop; terminal → drop; intervener set → drop; `ts` ≤ `health_analyzed_at` → drop), columns updated every surviving analysis, `CallEvent(HEALTH)` + status flip + notification only on episode transitions, recovery needs 2 consecutive healthy results, notifications only on escalation/category change.

- [ ] **Step 1: Write the failing tests**

```python
# vera-backend/tests/unit/control_plane/test_call_health_handler.py
"""_handle_call_health: guards, column updates, the episode state machine with
asymmetric hysteresis, and transition-only notifications (spec §4.3, §5)."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Insert

import control_plane.worker_events as worker_events
from control_plane.livekit_gateway import LiveKitGateway
from control_plane.worker_events import WorkerEventConsumer
from vera_core.events import CallHealthEvent
from vera_core.models import Call, CallEvent
from vera_core.models.enums import CallEventType, CallStatus
from vera_core.notifications import Notification
from vera_core.observability.correlation import room_name_for_call

_TENANT = uuid4()
_CALL = uuid4()
_ROOM = room_name_for_call(_TENANT, _CALL)


class _Result:
    def __init__(self, scalar: Any) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self) -> Any:
        return self._scalar


class _FakeSession:
    """Routes execute() by target entity: the Call row lock, and the episode-flag
    lookup (select(CallEvent.event_value) ... limit(1))."""

    def __init__(self, *, call: Any = None, episode_flag: str | None = None) -> None:
        self.call = call
        self.episode_flag = episode_flag
        self.added: list[Any] = []

    async def execute(self, stmt: Any) -> _Result:
        if isinstance(stmt, Insert):  # pragma: no cover — handler uses session.add
            raise AssertionError("unexpected insert")
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Call:
            return _Result(self.call)
        if entity is CallEvent:
            return _Result(self.episode_flag)
        raise AssertionError(f"unexpected query entity {entity}")

    def add(self, obj: Any) -> None:
        self.added.append(obj)


class _FakeSessionCtx:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _SpyNotifications:
    def __init__(self) -> None:
        self.published: list[tuple[UUID, Notification]] = []

    async def publish(self, tenant_id: UUID, notification: Notification) -> None:
        self.published.append((tenant_id, notification))


@dataclass
class _Wired:
    consumer: WorkerEventConsumer
    session: _FakeSession
    notifications: _SpyNotifications
    added_health: list[CallEvent] = field(default_factory=list)


def _wire(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> _Wired:
    notifications = _SpyNotifications()
    monkeypatch.setattr(
        worker_events, "tenant_session", lambda sm, tid: _FakeSessionCtx(session)
    )
    consumer = WorkerEventConsumer(
        cast(Redis, object()),
        cast(LiveKitGateway, object()),
        cast("async_sessionmaker[AsyncSession]", object()),
        object(),
        cast(Any, object()),
        cast(Any, object()),
        notifications=cast(Any, notifications),
    )
    return _Wired(consumer=consumer, session=session, notifications=notifications)


def _call_row(**overrides: Any) -> Call:
    defaults: dict[str, Any] = {
        "id": _CALL,
        "tenant_id": _TENANT,
        "form_id": uuid4(),
        "current_status": CallStatus.ACTIVE.value,
        "published": False,
        "initiated_by_id": uuid4(),
        "intervener_user_id": None,
        "health_score": None,
        "health_flag": None,
        "health_analyzed_at": None,
    }
    defaults.update(overrides)
    return Call(**defaults)


def _event(*, score: int = 40, flag: str = "conversation_loop", ts: int = 2_000) -> CallHealthEvent:
    return CallHealthEvent(
        room_name=_ROOM, score=score, flag=flag, reason="looping", turn_count=8, ts=ts
    )


def _health_rows(session: _FakeSession) -> list[CallEvent]:
    return [
        e for e in session.added
        if isinstance(e, CallEvent) and e.event_type == CallEventType.HEALTH.value
    ]


def _status_rows(session: _FakeSession) -> list[CallEvent]:
    return [
        e for e in session.added
        if isinstance(e, CallEvent) and e.event_type == CallEventType.STATUS.value
    ]


@pytest.mark.asyncio
async def test_escalation_opens_episode_flips_critical_and_notifies_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = uuid4()
    call = _call_row(initiated_by_id=owner)
    wired = _wire(monkeypatch, _FakeSession(call=call))
    await wired.consumer._handle_call_health(_event())
    assert call.health_score == 40 and call.health_flag == "conversation_loop"
    assert call.current_status == CallStatus.CRITICAL.value
    assert [r.event_value for r in _health_rows(wired.session)] == ["conversation_loop"]
    assert [r.event_value for r in _status_rows(wired.session)] == [CallStatus.CRITICAL.value]
    [(tenant_id, n)] = wired.notifications.published
    assert tenant_id == _TENANT
    assert n.audience.kind == "user" and n.audience.user_id == str(owner)  # unpublished -> owner
    assert n.data["call_id"] == str(_CALL) and n.data["flag"] == "conversation_loop"


@pytest.mark.asyncio
async def test_published_call_notifies_tenant_wide(monkeypatch: pytest.MonkeyPatch) -> None:
    call = _call_row(published=True)
    wired = _wire(monkeypatch, _FakeSession(call=call))
    await wired.consumer._handle_call_health(_event())
    [(_tid, n)] = wired.notifications.published
    assert n.audience.kind == "tenant"


@pytest.mark.asyncio
async def test_reconfirmation_updates_columns_only(monkeypatch: pytest.MonkeyPatch) -> None:
    call = _call_row(
        current_status=CallStatus.CRITICAL.value,
        health_flag="conversation_loop",
        health_analyzed_at=datetime.fromtimestamp(1.0, tz=UTC),
    )
    wired = _wire(monkeypatch, _FakeSession(call=call, episode_flag="conversation_loop"))
    await wired.consumer._handle_call_health(_event(score=35, ts=3_000))
    assert call.health_score == 35
    assert wired.session.added == [] and wired.notifications.published == []


@pytest.mark.asyncio
async def test_category_change_notifies_but_stays_critical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = _call_row(
        current_status=CallStatus.CRITICAL.value,
        health_flag="conversation_loop",
        health_analyzed_at=datetime.fromtimestamp(1.0, tz=UTC),
    )
    wired = _wire(monkeypatch, _FakeSession(call=call, episode_flag="conversation_loop"))
    await wired.consumer._handle_call_health(_event(flag="long_silence", ts=3_000))
    assert call.current_status == CallStatus.CRITICAL.value
    assert [r.event_value for r in _health_rows(wired.session)] == ["long_silence"]
    assert _status_rows(wired.session) == []
    assert len(wired.notifications.published) == 1


@pytest.mark.asyncio
async def test_recovery_needs_two_consecutive_healthy_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = _call_row(
        current_status=CallStatus.CRITICAL.value,
        health_flag="conversation_loop",
        health_analyzed_at=datetime.fromtimestamp(1.0, tz=UTC),
    )
    wired = _wire(monkeypatch, _FakeSession(call=call, episode_flag="conversation_loop"))
    # First healthy result: columns update, still CRITICAL, no rows, no notify.
    await wired.consumer._handle_call_health(_event(score=85, flag="none", ts=3_000))
    assert call.current_status == CallStatus.CRITICAL.value
    assert call.health_flag == "none" and wired.session.added == []
    # Second consecutive healthy: close the episode, back to ACTIVE, no notify.
    await wired.consumer._handle_call_health(_event(score=90, flag="none", ts=4_000))
    assert call.current_status == CallStatus.ACTIVE.value
    assert [r.event_value for r in _health_rows(wired.session)] == ["none"]
    assert [r.event_value for r in _status_rows(wired.session)] == [CallStatus.ACTIVE.value]
    assert wired.notifications.published == []


@pytest.mark.asyncio
async def test_healthy_blip_then_same_flag_does_not_renotify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = _call_row(
        current_status=CallStatus.CRITICAL.value,
        health_flag="none",  # a single healthy blip inside the episode
        health_analyzed_at=datetime.fromtimestamp(1.0, tz=UTC),
    )
    wired = _wire(monkeypatch, _FakeSession(call=call, episode_flag="conversation_loop"))
    await wired.consumer._handle_call_health(_event(flag="conversation_loop", ts=3_000))
    # Same episode category re-asserting itself after a blip: no new episode.
    assert wired.session.added == [] and wired.notifications.published == []
    assert call.health_flag == "conversation_loop"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call_kwargs",
    [
        {"current_status": CallStatus.COMPLETED.value},  # terminal — late result
        {"intervener_user_id": uuid4()},  # takeover raced the observer
        {"health_analyzed_at": datetime.fromtimestamp(10.0, tz=UTC)},  # stale/redelivered
    ],
)
async def test_guards_drop_the_event(
    monkeypatch: pytest.MonkeyPatch, call_kwargs: dict[str, Any]
) -> None:
    call = _call_row(**call_kwargs)
    before = (call.health_score, call.health_flag, call.current_status)
    wired = _wire(monkeypatch, _FakeSession(call=call))
    await wired.consumer._handle_call_health(_event(ts=2_000))
    assert (call.health_score, call.health_flag, call.current_status) == before
    assert wired.session.added == [] and wired.notifications.published == []


@pytest.mark.asyncio
async def test_no_call_row_drops_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    wired = _wire(monkeypatch, _FakeSession(call=None))
    await wired.consumer._handle_call_health(_event())  # must not raise _RetryEventLater
    assert wired.notifications.published == []
```

- [ ] **Step 2: Run to verify failure**

Run: `cd vera-backend && uv run pytest tests/unit/control_plane/test_call_health_handler.py -v`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'notifications'` (or missing handler)

- [ ] **Step 3: Implement.** In `worker_events.py`:

Imports to add:

```python
from datetime import UTC, datetime
from vera_core.events import CallHealthEvent  # extend the existing block
from vera_core.models.enums import CallEventType, CallHealthFlag, CallStatus  # extend
from vera_core.notifications import (
    TYPE_INTERVENTION_NEEDED,
    Notification,
    NotificationAudience,
    NotificationService,
)
```

Constructor: add the keyword param and store it; register the handler:

```python
        notifications: NotificationService | None = None,
```
```python
        self._notifications = notifications
```
```python
            "call.health": self._handle_call_health,
```

Handler (after `_handle_call_ended`):

```python
    async def _handle_call_health(self, event: WorkerEvent) -> None:
        """Persist one observer analysis (spec §4.3). Every surviving analysis
        updates the denormalized Call columns; a CallEvent(HEALTH) row, the
        ACTIVE<->CRITICAL flip, and a notification happen only on episode
        transitions — escalation immediately, recovery after 2 consecutive
        healthy results (asymmetric hysteresis, spec edge #4). Late results are
        DROPPED, never retried: unlike lifecycle events, a health frame is
        transient and superseded by the next analysis."""
        if not isinstance(event, CallHealthEvent):
            return
        ref = parse_room_name(event.room_name)
        if ref is None:
            return
        analyzed_at = datetime.fromtimestamp(event.ts / 1000, tz=UTC)
        notification: Notification | None = None
        async with tenant_session(self._sessionmaker, ref.tenant_id) as session:
            call = (
                await session.execute(select(Call).where(Call.id == ref.call_id).with_for_update())
            ).scalar_one_or_none()
            if call is None:
                return  # voice-lab room / dropped row
            if call.current_status in TERMINAL_VALUES:
                return  # analysis finished after the call ended
            if call.intervener_user_id is not None:
                return  # takeover raced the in-flight analysis
            if call.health_analyzed_at is not None and analyzed_at <= call.health_analyzed_at:
                return  # consumer-group redelivery / out-of-order duplicate
            prior_flag = call.health_flag
            in_episode = call.current_status == CallStatus.CRITICAL.value
            flagged = event.flag != CallHealthFlag.NONE.value
            call.health_score = event.score
            call.health_flag = event.flag
            call.health_analyzed_at = analyzed_at
            detail: dict[str, object] = {
                "score": event.score,
                "reason": event.reason,
                "turn_count": event.turn_count,
            }
            transition_flag: str | None = None
            if flagged and not in_episode:
                transition_flag = event.flag  # open an episode (escalation: immediate)
                if call.current_status == CallStatus.ACTIVE.value:
                    call.current_status = CallStatus.CRITICAL.value
                    session.add(
                        CallEvent(
                            tenant_id=ref.tenant_id,
                            call_id=call.id,
                            event_type=CallEventType.STATUS.value,
                            event_value=CallStatus.CRITICAL.value,
                        )
                    )
            elif flagged and in_episode:
                # Compare against the EPISODE category (the last HEALTH row), not
                # the per-analysis flag — a single healthy blip must not make the
                # same category read as a brand-new episode (spec §4.3).
                episode_flag = (
                    await session.execute(
                        select(CallEvent.event_value)
                        .where(
                            CallEvent.call_id == call.id,
                            CallEvent.event_type == CallEventType.HEALTH.value,
                        )
                        .order_by(CallEvent.created_at.desc(), CallEvent.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if event.flag != episode_flag:
                    transition_flag = event.flag  # category change while flagged
            elif in_episode and prior_flag == CallHealthFlag.NONE.value:
                # Second consecutive healthy result — close the episode. The
                # (prior_flag == none AND status CRITICAL) pair IS the 2-streak;
                # no counter column needed. No notification on recovery.
                session.add(
                    CallEvent(
                        tenant_id=ref.tenant_id,
                        call_id=call.id,
                        event_type=CallEventType.HEALTH.value,
                        event_value=CallHealthFlag.NONE.value,
                        detail=detail,
                    )
                )
                call.current_status = CallStatus.ACTIVE.value
                session.add(
                    CallEvent(
                        tenant_id=ref.tenant_id,
                        call_id=call.id,
                        event_type=CallEventType.STATUS.value,
                        event_value=CallStatus.ACTIVE.value,
                    )
                )
            if transition_flag is not None:
                session.add(
                    CallEvent(
                        tenant_id=ref.tenant_id,
                        call_id=call.id,
                        event_type=CallEventType.HEALTH.value,
                        event_value=transition_flag,
                        detail=detail,
                    )
                )
                # Routing rule (spec §4.4): unpublished -> owner only; published
                # or ownerless (tenant-visible in the list) -> tenant-wide.
                audience = (
                    NotificationAudience(kind="tenant")
                    if call.published or call.initiated_by_id is None
                    else NotificationAudience(kind="user", user_id=str(call.initiated_by_id))
                )
                notification = Notification(
                    type=TYPE_INTERVENTION_NEEDED,
                    audience=audience,
                    data={
                        "call_id": str(call.id),
                        "score": event.score,
                        "flag": event.flag,
                        "reason": event.reason,
                    },
                    ts=event.ts,
                )
        # After the transaction committed — receivers who refetch see the new state.
        if notification is not None and self._notifications is not None:
            try:
                await self._notifications.publish(ref.tenant_id, notification)
            except Exception as exc:  # payload is PHI — type name only
                logger.warning(
                    "intervention notification publish failed for %s (%s)",
                    event.room_name,
                    type(exc).__name__,
                )
```

- [ ] **Step 4: Guard `_handle_call_answered` against clobbering CRITICAL.** Replace its idempotency line:

```python
            if call.current_status == CallStatus.ACTIVE.value:
                return  # idempotent redelivery
```

with:

```python
            if call.current_status in (CallStatus.ACTIVE.value, CallStatus.CRITICAL.value):
                return  # idempotent redelivery (CRITICAL = already live AND health-flagged)
```

Add to `tests/unit/control_plane/test_worker_events.py` (uses that file's existing helpers):

```python
@pytest.mark.asyncio
async def test_answered_redelivery_does_not_clobber_critical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    call = _call_row(tenant_id, call_id, form_id, current_status=CallStatus.CRITICAL.value)
    wired = _consumer(monkeypatch, _FakeRedis(), _FakeLiveKit(), session=_FakeSession(call=call))
    ev = CallAnsweredEvent(room_name=room_name_for_call(tenant_id, call_id), ts=int(time.time() * 1000))
    await wired.consumer._handle_call_answered(ev)
    assert call.current_status == CallStatus.CRITICAL.value  # health flip survives
    assert wired.session.added == []
```

Also add a CRITICAL-closeout test there (spec edge #6 — first real producer of this status):

```python
@pytest.mark.asyncio
async def test_call_ended_closes_a_critical_call(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    call = _call_row(tenant_id, call_id, form_id, current_status=CallStatus.CRITICAL.value)
    form = _form_row(tenant_id, form_id)
    session = _FakeSession(call=call, form=form, tenant=_tenant(id=tenant_id))
    wired = _consumer(monkeypatch, _FakeRedis(), _FakeLiveKit(), session=session)
    ev = CallEndedEvent(room_name=room_name_for_call(tenant_id, call_id), ts=int(time.time() * 1000))
    await wired.consumer._handle_call_ended(ev)
    assert call.current_status in TERMINAL_VALUES  # CRITICAL closes like any active status
```

(Match the file's existing imports; `TERMINAL_VALUES` comes from `control_plane.call_closeout`. If an existing similar test asserts a specific terminal value, mirror its assertions.)

- [ ] **Step 5: Run tests**

Run: `cd vera-backend && uv run pytest tests/unit/control_plane/ -v`
Expected: PASS (existing + 10 new)

- [ ] **Step 6: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/worker_events.py \
        vera-backend/tests/unit/control_plane/test_call_health_handler.py \
        vera-backend/tests/unit/control_plane/test_worker_events.py
git commit -m "feat: persist call-health events — episode state machine, CRITICAL flips, notifications"
```

---

### Task 9: Notification SSE endpoint + app wiring

**Files:**
- Create: `vera-backend/apps/control_plane/src/control_plane/api/v1/notifications.py`
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/__init__.py`
- Modify: `vera-backend/apps/control_plane/src/control_plane/deps.py`
- Modify: `vera-backend/apps/control_plane/src/control_plane/main.py`
- Test: `vera-backend/tests/unit/control_plane/test_notifications_stream.py`

**Interfaces:**
- Consumes: `NotificationService` (Task 5).
- Produces: `GET /api/v1/notifications/stream` (SSE; tenant users with `calls:read`); `get_notification_service` dep; `app.state.notifications`; `WorkerEventConsumer` wired with `notifications=`; `create_app(notification_service=...)` test override; module-level `notification_frames(items, *, user_id)` and `delivers_to(audience, user_id)` (unit-tested pure parts).

- [ ] **Step 1: Write the failing test**

```python
# vera-backend/tests/unit/control_plane/test_notifications_stream.py
"""Audience filtering + keepalive framing for the notification SSE (the pure
generator; auth/permission gating rides the same chain as every endpoint and is
exercised at boot)."""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from control_plane.api.v1.notifications import delivers_to, notification_frames
from control_plane.sse import SSE_KEEPALIVE_FRAME
from vera_core.notifications import Notification, NotificationAudience


def _n(audience: NotificationAudience) -> Notification:
    return Notification(
        type="intervention_needed",
        audience=audience,
        data={"call_id": "c", "score": 30, "flag": "other", "reason": "r"},
        ts=1,
    )


def test_delivers_to() -> None:
    me, other = uuid4(), uuid4()
    assert delivers_to(NotificationAudience(kind="tenant"), me)
    assert delivers_to(NotificationAudience(kind="user", user_id=str(me)), me)
    assert not delivers_to(NotificationAudience(kind="user", user_id=str(other)), me)


@pytest.mark.asyncio
async def test_notification_frames_filters_and_keeps_alive() -> None:
    me, other = uuid4(), uuid4()

    async def items() -> AsyncIterator[tuple[str, Notification] | None]:
        yield None  # idle tick -> keepalive comment
        yield "1-1", _n(NotificationAudience(kind="user", user_id=str(other)))  # filtered
        yield "1-2", _n(NotificationAudience(kind="tenant"))

    frames = [frame async for frame in notification_frames(items(), user_id=me)]
    assert frames[0] == SSE_KEEPALIVE_FRAME
    assert len(frames) == 2  # the other-user notification never leaves the server
    assert frames[1].startswith("id: 1-2\ndata: ")
    assert '"tenant"' in frames[1]
```

- [ ] **Step 2: Run to verify failure**

Run: `cd vera-backend && uv run pytest tests/unit/control_plane/test_notifications_stream.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'control_plane.api.v1.notifications'`

- [ ] **Step 3: Implement the endpoint** (`api/v1/notifications.py`):

```python
"""Login-session notification SSE: user-scoped realtime alerts (intervention
needed today; other event types later) over the per-tenant notification stream.

One connection per logged-in user for the whole session. Filtering is
server-side: the connection forwards only notifications addressed to this user
(owner-only alerts for unpublished calls) or tenant-wide ones (published /
ownerless calls) — the same owner-or-published visibility rule as the call
surfaces. Requires a tenant session + calls:read (v1 notifications are all
call-related). The stream tails from "now": current state is always recovered
from the REST API on (re)connect; this pipe is an accelerant, never the record.
"""

import logging
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.api.v1.common import Audit
from control_plane.auth.identity import VerifiedIdentity
from control_plane.auth.rbac import PermissionResolver, get_resolver
from control_plane.deps import current_identity, get_notification_service, get_sessionmaker
from control_plane.exceptions import CustomAPIException, DefaultExceptionCode
from control_plane.request_context import current_request_id
from control_plane.sse import SSE_KEEPALIVE_FRAME
from vera_core.audit import AuditRecord
from vera_core.db.rls import tenant_session
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.enums import AccountType
from vera_core.notifications import Notification, NotificationAudience, NotificationService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["notifications"])


def delivers_to(audience: NotificationAudience, user_id: UUID) -> bool:
    """Server-side audience filter. Tenant-wide events reach every connection on
    this stream (the connect gate already required calls:read); user events only
    their addressee."""
    if audience.kind == "tenant":
        return True
    return audience.user_id == str(user_id)


async def notification_frames(
    items: AsyncIterator[tuple[str, Notification] | None], *, user_id: UUID
) -> AsyncIterator[str]:
    """Frame addressed notifications as SSE; idle ticks become keepalive comments
    (same proxy-timeout reasoning as frames_with_keepalive — filtered-out events
    produce no frame, so ticks are the only idle bytes)."""
    async for item in items:
        if item is None:
            yield SSE_KEEPALIVE_FRAME
            continue
        entry_id, notification = item
        if delivers_to(notification.audience, user_id):
            yield f"id: {entry_id}\ndata: {notification.model_dump_json()}\n\n"


@router.get("/notifications/stream")
async def stream_notifications(
    request: Request,
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    resolver: Annotated[PermissionResolver, Depends(get_resolver)],
    audit: Audit,
    service: Annotated[NotificationService, Depends(get_notification_service)],
) -> StreamingResponse:
    """Authorization runs in a short-lived tenant session released before
    streaming (an SSE must not pin a DB connection — mirrors stream_call_events).
    The folded authz+access audit record below mirrors _authorize_call_read's
    SSE exception to the emit-helper rule (see control_plane/CLAUDE.md)."""
    if identity.account_type is not AccountType.TENANT or identity.tenant_id is None:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="notifications require a tenant session"
        )
    tenant_id = identity.tenant_id
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
            resource_type="notifications",
            resource_id="stream",
            permission_key="calls:read",
            decision="allow" if allowed else "deny",
            request_id=current_request_id(request),
        )
    )
    if not allowed:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="missing permission calls:read"
        )
    return StreamingResponse(
        notification_frames(service.tail(tenant_id), user_id=user_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
```

- [ ] **Step 4: Register the router.** In `api/v1/__init__.py`:

```python
from control_plane.api.v1.notifications import router as notifications_router
```
and after `router.include_router(calls_router)`:
```python
router.include_router(notifications_router)
```

- [ ] **Step 5: Dependency.** In `deps.py` (near `get_call_stream_service`; import `from vera_core.notifications import NotificationService` at the top):

```python
def get_notification_service(request: Request) -> NotificationService:
    service: NotificationService = request.app.state.notifications
    return service
```

- [ ] **Step 6: App wiring.** In `main.py`:
  - import: `from vera_core.notifications import NotificationService, RedisNotificationStore`
  - `create_app(...)` signature: add `notification_service: NotificationService | None = None,`
  - in the lifespan, next to the other dedicated clients (`call_stream_redis`), declare `notifications_redis: Redis | None = None` and, after the `app.state.call_stream_service` block:

```python
        # User-scoped realtime notifications (intervention alerts). Same
        # dedicated-client reasoning as the SSE streams above: every connected
        # user pins a blocking XREAD, which must not starve the shared pool.
        _notifications = notification_service
        if _notifications is None:
            notifications_redis = create_redis(settings.redis_url)
            _notifications = NotificationService(RedisNotificationStore(notifications_redis))
        app.state.notifications = _notifications
```

  - pass it to the consumer: add `notifications=_notifications,` to the `WorkerEventConsumer(...)` construction;
  - in the shutdown section (next to the other redis closes):

```python
        if notifications_redis is not None:
            await notifications_redis.aclose()
```

- [ ] **Step 7: Run tests + typecheck**

Run: `cd vera-backend && uv run pytest tests/unit/control_plane/ -v && uv run mypy`
Expected: PASS / clean

- [ ] **Step 8: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/notifications.py \
        vera-backend/apps/control_plane/src/control_plane/api/v1/__init__.py \
        vera-backend/apps/control_plane/src/control_plane/deps.py \
        vera-backend/apps/control_plane/src/control_plane/main.py \
        vera-backend/tests/unit/control_plane/test_notifications_stream.py
git commit -m "feat: login-session notification SSE endpoint + app wiring"
```

---

### Task 10: Health fields on `GET /calls`

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/schemas/dto.py` (`CallSummary`)
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/calls.py` (`_summary`, ~line 169)
- Test: `vera-backend/tests/unit/schemas/test_call_dtos.py` (extend)

**Interfaces:**
- Produces: `CallSummary.health_score: int | None`, `.health_flag: str | None`, `.health_analyzed_at: datetime | None` — consumed by the frontend list (Task 13/14).

- [ ] **Step 1: Write the failing test** — append to `tests/unit/schemas/test_call_dtos.py` (match its imports):

```python
def test_call_summary_health_fields_default_null() -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from vera_core.schemas import CallSummary

    s = CallSummary(
        id=uuid4(), tenant_id=uuid4(), status="active", room_name="r",
        created_at=datetime.now(UTC),
    )
    assert s.health_score is None and s.health_flag is None and s.health_analyzed_at is None
```

- [ ] **Step 2: Run to verify failure**

Run: `cd vera-backend && uv run pytest tests/unit/schemas/test_call_dtos.py -v`
Expected: FAIL — unexpected attribute

- [ ] **Step 3: Implement.** In `dto.py`'s `CallSummary`, after `is_owner`:

```python
    # Latest call-health-observer assessment. NULL score = never assessed (the
    # UI renders it neutrally, never as 0). analyzed_at drives staleness display.
    health_score: int | None = None
    health_flag: str | None = None
    health_analyzed_at: datetime | None = None
```

In `calls.py` `_summary(...)`, add to the constructor call:

```python
        health_score=call.health_score,
        health_flag=call.health_flag,
        health_analyzed_at=call.health_analyzed_at,
```

- [ ] **Step 4: Run tests**

Run: `cd vera-backend && uv run pytest tests/unit/schemas/ tests/unit/control_plane/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/schemas/dto.py \
        vera-backend/apps/control_plane/src/control_plane/api/v1/calls.py \
        vera-backend/tests/unit/schemas/test_call_dtos.py
git commit -m "feat: health score/flag on the calls list response"
```

---

### Task 11: Backend gate + boot verification

**Files:** none (verification only)

- [ ] **Step 1: Full gate, verbatim**

Run: `cd vera-backend && just check`
Expected: lint + mypy + pytest all green. Fix anything red before proceeding (prove pre-existing failures against the merge-base if claimed).

- [ ] **Step 2: Boot the control plane** (background-loop rule — pytest alone is insufficient):

```bash
cd vera-backend && just up && just migrate
# LOCAL_KMS_MASTER_KEY must be set (see README / .env); VERA_LIVEKIT_URL must point
# at the local livekit from docker-compose so the worker-event consumer starts.
just api
```

Watch for ≥2 consumer BLOCK windows (~10s) with **no** tracebacks / back-off spam (the Redis BLOCK-timeout footgun). Then `curl -s localhost:8000/healthz` → `{"status":"ok"}`.

- [ ] **Step 3: Boot the worker**

Run: `cd vera-backend && just worker` — starts registered as `vera-agent`, idles cleanly.

- [ ] **Step 4 (if a dev call flow is available): drive one call** through the normal dispatch path and watch the api log for `consumed worker event ... type=call.health` after ~2 user turns + 15s, and `SELECT health_score, health_flag, current_status FROM call ...` showing values. If no telephony is available locally, note it and rely on the dev-environment smoke after merge.

- [ ] **Step 5: Commit** (only if fixes were needed) and re-run `just check` after any fix.

---

### Task 12: Frontend — list types + `health` envelope narrower

**Files:**
- Modify: `vera-frontend/src/lib/api/calls.ts` (`CallSummary` type)
- Modify: `vera-frontend/src/lib/api/callEvents.ts`
- Test: `vera-frontend/src/lib/api/callEvents.test.ts` (extend)

**Interfaces:**
- Produces: `CallSummary.health_score/health_flag/health_analyzed_at`; `CallHealth = { score: number; flag: string; reason: string | null; ts: number }`; `asCallHealth(e: CallStreamEvent): CallHealth | null`.

- [ ] **Step 1: Write the failing test** — append to `callEvents.test.ts`:

```ts
describe("asCallHealth", () => {
  it("narrows a health envelope", () => {
    const e: CallStreamEvent = {
      type: "health",
      data: { score: 35, flag: "conversation_loop", reason: "loop detected" },
      ts: 9,
    }
    expect(asCallHealth(e)).toEqual({
      score: 35,
      flag: "conversation_loop",
      reason: "loop detected",
      ts: 9,
    })
  })

  it("returns null for other or malformed envelopes", () => {
    expect(asCallHealth({ type: "transcript", data: { text: "x" }, ts: 1 })).toBeNull()
    expect(asCallHealth({ type: "health", data: { flag: "other" }, ts: 1 })).toBeNull()
  })

  it("tolerates a missing reason", () => {
    expect(asCallHealth({ type: "health", data: { score: 80, flag: "none" }, ts: 2 })).toEqual({
      score: 80,
      flag: "none",
      reason: null,
      ts: 2,
    })
  })
})
```
(add `asCallHealth` to the existing import from `@/lib/api/callEvents`.)

- [ ] **Step 2: Run to verify failure**

Run: `cd vera-frontend && npm test`
Expected: FAIL — `asCallHealth` is not exported

- [ ] **Step 3: Implement.** In `callEvents.ts`, after `asCallStatus`:

```ts
/** One call-health-observer assessment (the "health" envelope). */
export type CallHealth = {
  /** 0-100; higher is healthier. */
  score: number
  /** "none" (healthy) or an intervention category (conversation_loop, ...). */
  flag: string
  /** LLM's one-line justification (PHI — session-scoped state only). */
  reason: string | null
  ts: number
}

/** Narrow an envelope to a health assessment; null for other/malformed types. */
export function asCallHealth(e: CallStreamEvent): CallHealth | null {
  if (e.type !== "health") return null
  const { score, flag, reason } = e.data as { score?: unknown; flag?: unknown; reason?: unknown }
  if (typeof score !== "number" || typeof flag !== "string") return null
  return { score, flag, reason: typeof reason === "string" ? reason : null, ts: e.ts }
}
```

In `calls.ts`'s `CallSummary` type, after `is_owner`:

```ts
  /** Latest observer health score (0-100); null = never assessed (render neutrally, never 0). */
  health_score: number | null
  /** "none" or an intervention category; null = never assessed. */
  health_flag: string | null
  /** ISO-8601 time of the latest assessment; drives the staleness gray-out. */
  health_analyzed_at: string | null
```

- [ ] **Step 4: Run tests**

Run: `cd vera-frontend && npm test && npx tsc -b`
Expected: PASS. If `tsc` flags test fixtures constructing `CallSummary` without the new fields, add `health_score: null, health_flag: null, health_analyzed_at: null` to those fixtures.

- [ ] **Step 5: Commit**

```bash
git add vera-frontend/src/lib/api/calls.ts vera-frontend/src/lib/api/callEvents.ts \
        vera-frontend/src/lib/api/callEvents.test.ts
git commit -m "feat(frontend): call health types + health envelope narrower"
```

---

### Task 13: Frontend — notifications client + `NotificationsProvider` (sonner)

**Files:**
- Modify: `vera-frontend/package.json` (add `sonner`)
- Create: `vera-frontend/src/lib/api/notifications.ts`
- Create: `vera-frontend/src/components/notifications/NotificationsProvider.tsx`
- Modify: `vera-frontend/src/App.tsx`
- Test: `vera-frontend/src/lib/api/notifications.test.ts`

**Interfaces:**
- Consumes: `BASE_URL`/`ApiError` from `@/lib/api/client`, `getToken` from `@/lib/auth/storage` (same as `callEvents.ts`).
- Produces:
  - `AppNotification = { type: string; data: Record<string, unknown>; ts: number }`
  - `InterventionNeeded = { callId: string; score: number; flag: string }`
  - `asInterventionNeeded(n: AppNotification): InterventionNeeded | null`
  - `streamNotifications(opts: { signal: AbortSignal; onNotification: (n: AppNotification) => void }): Promise<void>` — reconnects forever until abort; 4xx throws.
  - `NotificationsProvider` — opens the stream for the login session, toasts on intervention alerts, dispatches `window` CustomEvent `"vera:notification"` (Task 14 listens); renders sonner's `<Toaster />`.
  - `NOTIFICATION_EVENT = "vera:notification"` exported from the provider module.

- [ ] **Step 1: Install sonner** (Corepack-pinned npm; verify the lockfile the way CI will):

Run: `cd vera-frontend && npm install sonner && npm ci`
Expected: both succeed (`npm ci` proves the lockfile is CI-clean).

- [ ] **Step 2: Write the failing test**

```ts
// vera-frontend/src/lib/api/notifications.test.ts
import { describe, expect, it, vi } from "vitest"

vi.mock("@/lib/auth/storage", () => ({ getToken: () => "tok" }))

import { asInterventionNeeded, type AppNotification } from "@/lib/api/notifications"

describe("asInterventionNeeded", () => {
  it("narrows an intervention_needed notification", () => {
    const n: AppNotification = {
      type: "intervention_needed",
      data: { call_id: "c-1", score: 30, flag: "conversation_loop", reason: "r" },
      ts: 1,
    }
    expect(asInterventionNeeded(n)).toEqual({
      callId: "c-1",
      score: 30,
      flag: "conversation_loop",
    })
  })

  it("returns null for other types or malformed data", () => {
    expect(asInterventionNeeded({ type: "something_else", data: {}, ts: 1 })).toBeNull()
    expect(
      asInterventionNeeded({ type: "intervention_needed", data: { score: 1 }, ts: 1 }),
    ).toBeNull()
  })
})
```

- [ ] **Step 3: Run to verify failure**

Run: `cd vera-frontend && npm test`
Expected: FAIL — module not found

- [ ] **Step 4: Implement `src/lib/api/notifications.ts`**

```ts
// Login-session notification SSE client. One connection for the whole session
// (mounted by NotificationsProvider); the server filters by audience, so every
// event that arrives here is addressed to this user. Reconnects forever with
// capped backoff — the stream tails from "now", so there is no replay to
// discard; consumers refetch current state via the REST API instead (the SSE is
// an accelerant, never the source of truth). Mirrors callEvents.ts transport.

import { ApiError, BASE_URL } from "@/lib/api/client"
import { getToken } from "@/lib/auth/storage"

export type AppNotification = { type: string; data: Record<string, unknown>; ts: number }

/** A "call needs intervention" alert. `reason` is deliberately not surfaced
 *  here — it can carry PHI; the toast shows category + score only. */
export type InterventionNeeded = { callId: string; score: number; flag: string }

/** Narrow a notification to an intervention alert; null for other/malformed types. */
export function asInterventionNeeded(n: AppNotification): InterventionNeeded | null {
  if (n.type !== "intervention_needed") return null
  const { call_id, score, flag } = n.data as {
    call_id?: unknown
    score?: unknown
    flag?: unknown
  }
  if (typeof call_id !== "string" || typeof score !== "number" || typeof flag !== "string")
    return null
  return { callId: call_id, score, flag }
}

/**
 * Stream the caller's notifications until `signal` aborts. Transient failures
 * (network, 5xx, idle-timeout closes) reconnect with capped backoff;
 * non-retryable request failures (4xx: session expired, permission revoked)
 * throw an ApiError so the caller can stop for good.
 */
export async function streamNotifications(opts: {
  signal: AbortSignal
  onNotification: (n: AppNotification) => void
}): Promise<void> {
  let consecutiveFailures = 0
  for (;;) {
    try {
      await streamOnce(opts.signal, opts.onNotification)
      consecutiveFailures = 0
    } catch (err) {
      if (opts.signal.aborted) return
      if (err instanceof ApiError && err.httpStatus < 500) throw err
      consecutiveFailures += 1
    }
    if (opts.signal.aborted) return
    await backoffDelay(Math.min(1000 * 2 ** consecutiveFailures, 15_000), opts.signal)
  }
}

async function streamOnce(
  signal: AbortSignal,
  onNotification: (n: AppNotification) => void,
): Promise<void> {
  const res = await fetch(`${BASE_URL}/notifications/stream`, {
    method: "GET",
    headers: { Authorization: `Bearer ${getToken()}`, Accept: "text/event-stream" },
    signal,
  })
  if (!res.ok || !res.body) {
    throw new ApiError(res.status, null, `notification stream failed (${res.status})`)
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
      if (json) onNotification(JSON.parse(json) as AppNotification)
    }
  }
}

function backoffDelay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const finish = () => {
      clearTimeout(timer)
      signal.removeEventListener("abort", finish)
      resolve()
    }
    const timer = setTimeout(finish, ms)
    signal.addEventListener("abort", finish)
  })
}
```

Check `ApiError`'s constructor signature in `@/lib/api/errors` and match it (callEvents.ts uses `new ApiError(res.status, null, msg)` — mirror exactly).

- [ ] **Step 5: Implement `src/components/notifications/NotificationsProvider.tsx`**

```tsx
import { useEffect } from "react"
import { Toaster, toast } from "sonner"

import { asInterventionNeeded, streamNotifications } from "@/lib/api/notifications"

/** Window event fired on every received notification — pages that show live
 *  call state (Live Monitoring) listen and refetch immediately instead of
 *  waiting for the next poll. */
export const NOTIFICATION_EVENT = "vera:notification"

const FLAG_LABELS: Record<string, string> = {
  supervisor_requested: "Supervisor requested",
  repeated_questions: "Repeated questions",
  hallucination: "Possible hallucination",
  conversation_loop: "Conversation loop",
  long_silence: "Long silence",
  off_script: "Off script",
  low_confidence: "Low confidence",
  other: "Needs attention",
}

/**
 * Login-session realtime notifications: opens ONE SSE for the whole session
 * (mounted around the authenticated shell) and toasts intervention alerts.
 * The toast carries category + score only — never patient details or the LLM
 * reason (PHI hygiene: minimum necessary on a surface that outlives the page).
 * A 4xx (expired session / revoked permission) stops the stream quietly;
 * RequireAuth handles the redirect on the next API call.
 */
export function NotificationsProvider({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    const controller = new AbortController()
    streamNotifications({
      signal: controller.signal,
      onNotification: (n) => {
        window.dispatchEvent(new CustomEvent(NOTIFICATION_EVENT))
        const alert = asInterventionNeeded(n)
        if (alert) {
          toast.warning("Call needs intervention", {
            description: `${FLAG_LABELS[alert.flag] ?? alert.flag} — health ${alert.score}%`,
          })
        }
      },
    }).catch(() => {
      // Non-retryable (4xx). Silent: the session flow owns re-auth UX.
    })
    return () => controller.abort()
  }, [])

  return (
    <>
      {children}
      <Toaster position="top-right" richColors closeButton />
    </>
  )
}
```

- [ ] **Step 6: Mount it.** In `App.tsx`, import it and wrap the authenticated shell:

```tsx
import { NotificationsProvider } from "@/components/notifications/NotificationsProvider"
```
```tsx
        <Route element={<RequireAuth />}>
          <Route
            element={
              <NotificationsProvider>
                <AppShell />
              </NotificationsProvider>
            }
          >
```
(`AppShell` renders an `<Outlet>`; wrapping its element keeps the layout-route semantics. Verify the nested `<Route element={...}>` structure compiles — if `AppShell` is used as `<Route element={<AppShell />}>` today, this is a drop-in wrap of that element.)

- [ ] **Step 7: Run the frontend gate pieces**

Run: `cd vera-frontend && npm test && npx tsc -b && npx eslint src/lib/api/notifications.ts src/components/notifications/`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add vera-frontend/package.json vera-frontend/package-lock.json \
        vera-frontend/src/lib/api/notifications.ts vera-frontend/src/lib/api/notifications.test.ts \
        vera-frontend/src/components/notifications/NotificationsProvider.tsx vera-frontend/src/App.tsx
git commit -m "feat(frontend): login-session notification SSE + intervention toasts"
```

---

### Task 14: Frontend — Live Monitoring health column

**Files:**
- Create: `vera-frontend/src/lib/monitoring/health.ts`
- Test: `vera-frontend/src/lib/monitoring/health.test.ts`
- Modify: `vera-frontend/src/pages/LiveMonitoring.tsx`

**Interfaces:**
- Consumes: `CallSummary.health_*` (Task 12), `NOTIFICATION_EVENT` (Task 13).
- Produces: `healthTone(score: number | null): "good" | "warn" | "bad" | "unknown"`; `isHealthStale(analyzedAt: string | null, nowMs: number): boolean`; `healthDisplay(score, analyzedAt, nowMs): { text: string; tone: ... ; stale: boolean }`.

- [ ] **Step 1: Write the failing test**

```ts
// vera-frontend/src/lib/monitoring/health.test.ts
import { describe, expect, it } from "vitest"

import { healthDisplay, healthTone, isHealthStale } from "@/lib/monitoring/health"

describe("healthTone", () => {
  it("buckets scores", () => {
    expect(healthTone(null)).toBe("unknown")
    expect(healthTone(85)).toBe("good")
    expect(healthTone(70)).toBe("good")
    expect(healthTone(69)).toBe("warn")
    expect(healthTone(40)).toBe("warn")
    expect(healthTone(39)).toBe("bad")
  })
})

describe("isHealthStale", () => {
  it("is stale past 3x the analysis interval (45s), never for unassessed", () => {
    const now = Date.parse("2026-07-17T10:01:00Z")
    expect(isHealthStale(null, now)).toBe(false)
    expect(isHealthStale("2026-07-17T10:00:30Z", now)).toBe(false) // 30s
    expect(isHealthStale("2026-07-17T10:00:00Z", now)).toBe(true) // 60s
  })
})

describe("healthDisplay", () => {
  it("renders assessing / score / stale states", () => {
    const now = Date.parse("2026-07-17T10:01:00Z")
    expect(healthDisplay(null, null, now)).toEqual({
      text: "Assessing…",
      tone: "unknown",
      stale: false,
    })
    expect(healthDisplay(82, "2026-07-17T10:00:50Z", now)).toEqual({
      text: "82%",
      tone: "good",
      stale: false,
    })
    expect(healthDisplay(82, "2026-07-17T10:00:00Z", now).stale).toBe(true)
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `cd vera-frontend && npm test`
Expected: FAIL — module not found

- [ ] **Step 3: Implement `src/lib/monitoring/health.ts`**

```ts
// Pure view logic for the observer health badge (unit-tested; no React).

export type HealthTone = "good" | "warn" | "bad" | "unknown"

/** 3x the observer's analysis interval (15s): older than this means the
 *  observer has gone quiet (LLM outage / silence) — gray out, don't assert. */
const STALE_AFTER_MS = 45_000

export function healthTone(score: number | null): HealthTone {
  if (score === null) return "unknown"
  if (score >= 70) return "good"
  if (score >= 40) return "warn"
  return "bad"
}

/** Never-assessed (null) is "not yet", not "stale". */
export function isHealthStale(analyzedAt: string | null, nowMs: number): boolean {
  if (!analyzedAt) return false
  const t = Date.parse(analyzedAt)
  return Number.isFinite(t) && nowMs - t > STALE_AFTER_MS
}

export function healthDisplay(
  score: number | null,
  analyzedAt: string | null,
  nowMs: number,
): { text: string; tone: HealthTone; stale: boolean } {
  if (score === null) return { text: "Assessing…", tone: "unknown", stale: false }
  return { text: `${score}%`, tone: healthTone(score), stale: isHealthStale(analyzedAt, nowMs) }
}
```

- [ ] **Step 4: Wire the column into `LiveMonitoring.tsx`:**
  - imports:

```tsx
import { healthDisplay, type HealthTone } from "@/lib/monitoring/health"
import { NOTIFICATION_EVENT } from "@/components/notifications/NotificationsProvider"
```

  - tone → class map (next to the other style records):

```tsx
const healthText: Record<HealthTone, string> = {
  good: "text-emerald-600",
  warn: "text-amber-600",
  bad: "text-red-600",
  unknown: "text-muted-foreground",
}
```

  - refactor the load effect so a notification triggers an immediate refetch. Replace the existing load+poll effect body with:

```tsx
  // Load + poll (skip while the tab is hidden); a realtime notification
  // (intervention alert) refetches immediately instead of waiting the poll out.
  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const items = await listCalls()
        if (!cancelled) {
          setCalls(items)
          setError(null)
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "Could not load calls.")
      }
    }
    void load()
    const id = setInterval(() => {
      if (document.visibilityState === "visible") void load()
    }, POLL_MS)
    const onNotification = () => void load()
    window.addEventListener(NOTIFICATION_EVENT, onNotification)
    return () => {
      cancelled = true
      clearInterval(id)
      window.removeEventListener(NOTIFICATION_EVENT, onNotification)
    }
  }, [])
```

  - table header: after `<TableHead>Duration</TableHead>` add `<TableHead>Call Health</TableHead>`;
  - table body: after the Duration cell add:

```tsx
                  <TableCell>
                    {(() => {
                      const h = healthDisplay(call.health_score, call.health_analyzed_at, now)
                      return (
                        <span
                          className={cn(
                            "font-semibold tabular-nums",
                            h.stale ? "text-muted-foreground" : healthText[h.tone],
                          )}
                          title={
                            call.health_flag && call.health_flag !== "none"
                              ? call.health_flag.replaceAll("_", " ")
                              : undefined
                          }
                        >
                          {h.text}
                          {h.stale && " (stale)"}
                        </span>
                      )
                    })()}
                  </TableCell>
```

  - bump the empty-state `colSpan={7}` to `colSpan={8}`.

- [ ] **Step 5: Run the gate pieces**

Run: `cd vera-frontend && npm test && npx tsc -b && npx eslint src/pages/LiveMonitoring.tsx src/lib/monitoring/health.ts`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add vera-frontend/src/lib/monitoring/health.ts vera-frontend/src/lib/monitoring/health.test.ts \
        vera-frontend/src/pages/LiveMonitoring.tsx
git commit -m "feat(frontend): live health column + instant refetch on intervention alerts"
```

---

### Task 15: Frontend — live health in the call modal

**Files:**
- Modify: `vera-frontend/src/lib/mock-data.ts` (`LiveCall` type)
- Modify: `vera-frontend/src/pages/LiveMonitoring.tsx` (`toLiveCall`)
- Modify: `vera-frontend/src/components/monitoring/CallTranscript.tsx` (thread `onHealth`)
- Modify: `vera-frontend/src/components/monitoring/LiveCallModal.tsx` (header cell)

**Interfaces:**
- Consumes: `asCallHealth`/`CallHealth` (Task 12), `healthTone` (Task 14).
- Produces: `CallTranscript` prop `onHealth?: (h: CallHealth) => void`; `LiveCall.healthScore?: number | null`; the modal's third header cell shows **Call Health** (live SSE value, falling back to the polled list value), replacing the mock "Confidence" cell.

- [ ] **Step 1: Extend `LiveCall`.** In `mock-data.ts`, add to the `LiveCall` type (optional, so mock rows stay valid):

```ts
  /** Latest observer health score (0-100); null/undefined = not assessed. */
  healthScore?: number | null
```

In `LiveMonitoring.tsx` `toLiveCall(...)`, add `healthScore: c.health_score,`.

- [ ] **Step 2: Thread health out of the transcript stream.** In `CallTranscript.tsx`:
  - import `asCallHealth, type CallHealth` from `@/lib/api/callEvents`;
  - add the prop (after `onCallStatus` in the signature and its JSDoc):

```tsx
  /** Fires for every health envelope — the modal lifts this into its header badge. */
  onHealth?: (h: CallHealth) => void
```

  - mirror the existing ref pattern: `const onHealthRef = useRef(onHealth)` updated in the same effect that syncs `onCallStatusRef`; and inside `onEvent`, after the `asCallStatus` handling:

```tsx
        const health = asCallHealth(e)
        if (health) onHealthRef.current?.(health)
```

- [ ] **Step 3: Show it in the modal.** In `LiveCallModal.tsx`:
  - imports:

```tsx
import { healthTone, type HealthTone } from "@/lib/monitoring/health"
import type { CallHealth } from "@/lib/api/callEvents"
```

  - replace the `confidenceColor` helper with a tone→class map:

```tsx
const healthColor: Record<HealthTone, string> = {
  good: "text-emerald-600",
  warn: "text-amber-600",
  bad: "text-red-600",
  unknown: "text-muted-foreground",
}
```

  - state next to the others: `const [liveHealth, setLiveHealth] = useState<CallHealth | null>(null)`; reset it in `handleOpenChange`'s close branch (`setLiveHealth(null)`);
  - replace the "Confidence" header cell with:

```tsx
            <div className="text-right">
              <div className="text-xs text-muted-foreground">Call Health</div>
              {(() => {
                const score = liveHealth?.score ?? call?.healthScore ?? null
                return (
                  <div className={cn("font-semibold", healthColor[healthTone(score)])}>
                    {score === null ? "Assessing…" : `${score}%`}
                  </div>
                )
              })()}
            </div>
```

  - pass the callback to the transcript: `onHealth={setLiveHealth}` on `<CallTranscript ... />`.
  - remove the now-unused `confidenceColor` (and the `confidence` usage if nothing else references it).

- [ ] **Step 4: Run the gate pieces**

Run: `cd vera-frontend && npm test && npx tsc -b && npx eslint src/components/monitoring src/pages/LiveMonitoring.tsx src/lib/mock-data.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add vera-frontend/src/lib/mock-data.ts vera-frontend/src/pages/LiveMonitoring.tsx \
        vera-frontend/src/components/monitoring/CallTranscript.tsx \
        vera-frontend/src/components/monitoring/LiveCallModal.tsx
git commit -m "feat(frontend): live health badge in the call modal"
```

---

### Task 16: Frontend full gate

**Files:** none (verification only)

- [ ] **Step 1: All four commands, verbatim**

Run: `cd vera-frontend && npx tsc -b && npx eslint . && npm test && npm run build`
Expected: all green. Fix anything red (prove pre-existing failures against the merge-base if claimed), then re-run all four.

- [ ] **Step 2: Commit any fixes**

```bash
git add -A vera-frontend && git commit -m "fix(frontend): gate cleanups for call health feature"
```
(Skip if nothing changed.)

---

### Task 17: Simplify pass + final gates (repo-mandated)

**Files:** whatever the simplifier touches

- [ ] **Step 1:** Run the **code-simplifier** agent on the change ("simplify code" — targets recently modified code; behavior-preserving only).
- [ ] **Step 2:** Re-run `cd vera-backend && just check` — green.
- [ ] **Step 3:** Re-run `cd vera-frontend && npx tsc -b && npx eslint . && npm test && npm run build` — green.
- [ ] **Step 4:** Re-verify boot (Task 11 steps 2–3) if the simplifier touched any loop/wiring code.
- [ ] **Step 5:** Commit refinements:

```bash
git add -A && git commit -m "refactor: simplify call health observer implementation"
```

---

## Plan self-review notes

- **Spec coverage:** observer trigger/cadence (T6), cold-start + `assessable:false` (T2/T6), prompt-cache rules + re-anchor window (T2), takeover double-guard (T6), worker wiring/shutdown (T7), `TYPE_HEALTH` SSE (T4), `call.health` event + PHI note (T3), episode state machine incl. hysteresis/idempotency/terminal/intervener guards + `call.answered` CRITICAL guard + CRITICAL closeout (T8), notifications service/routing/endpoint/audit (T5/T9), list fields (T10), boot verification (T11), frontend narrower/types (T12), session SSE + toasts + reconnect-refetch (T13/T14), monitoring badge + staleness + Critical tab via existing categorizer (T14), modal live health (T15), gates (T11/T16), simplify (T17).
- Deliberately not implemented (spec non-goals): notification persistence, per-analysis history rows, automated actions from flags.
