# Retry Call Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the post-call eval finds required fields unsatisfied and retries remain, re-queue the form for a RETRY call that re-asks only those fields (metadata nudge), link the attempt in `call_lineage`, and fall through to `EXCEPTION_REVIEW` only when retries are exhausted.

**Architecture:** Extend Phase 1's `evaluate_call` status decision with a satisfaction check over required schema fields; add an `AI_PROCESSING → IN_QUEUE` retry edge (retry-cap-guarded); have the dispatcher attach non-PHI `retry_fields` labels to a RETRY call's room metadata and populate `call_lineage`; the DB-less worker focuses its static IBV script on those labels.

**Tech Stack:** Python 3.12, asyncio, SQLAlchemy async, Redis Streams, pytest, ruff, mypy --strict.

## Global Constraints

- **PHI never in the LLM/metadata/logs as values.** `retry_fields` carries field **labels/paths only** (schema metadata), never values. Audit `detail` = names/counts only.
- **Timestamps from the DB clock** (`func.now()` / model mixins), never `datetime.now()`.
- **All DB work inside a tenant-scoped session** (RLS `SET LOCAL app.tenant_id`).
- **Single confidence threshold = 70** (`settings.post_call_review_floor`, bumped 60→70) governs required-field satisfaction.
- **Required field satisfied** iff current value is trusted (`intake`/`human`) OR (`ai_call` AND judge-supported AND confidence ≥ 70 AND not token-valued).
- **Token-valued fields** route the form to `EXCEPTION_REVIEW` (not retry) — decided inside `evaluate_call` (only it knows what got tokenized this call).
- **No schema/migrations** — `call_lineage`, `call.mode`, `retry_count` all exist. If a column is unexpectedly needed, use the idempotent-migration rules.
- **PEP 695 type params** (`class Foo[T]`, `def f[T]`); no `TypeVar`/`Generic`. **asyncio only** (no `anyio`).
- **New tests live under `vera-backend/tests/unit/<area>/` and `vera-backend/tests/integration/`** — NOT `packages/vera_core/tests/` (not in `testpaths`; the gate won't collect it). Verify with `uv run pytest --collect-only -q | grep <name>`.
- **Verification gate:** `just check` green (agent_worker/livekit collection errors + local dev-DB migration drift are pre-existing local-env noise, green on CI) → `/simplify` → re-check. Commit all `/simplify` output.
- **Commit trailers:** end commit bodies with the repo `Co-Authored-By:` / `Claude-Session:` lines.

---

## File structure

| File | Responsibility |
|---|---|
| `packages/vera_core/src/vera_core/forms/review.py` (modify) | `FieldStatus` dataclass, `is_field_satisfied`, `retryable_required_paths`, `field_labels` — pure required-field satisfaction + labels (v1/v2). |
| `packages/vera_core/src/vera_core/services/field_status.py` (create) | `load_field_status(session, form_id) -> dict[str, FieldStatus]` — PHI-free DB read (current answer source/confidence + latest eval `supported`). |
| `packages/vera_core/src/vera_core/services/form_state_machine.py` (modify) | add `AI_PROCESSING → IN_QUEUE`; generalize retry-cap guard to `{CALL_FAILED, AI_PROCESSING} → IN_QUEUE`. |
| `packages/vera_core/src/vera_core/services/post_call_eval.py` (modify) | split token-valued fields from low-confidence; apply the COMPLETED/retry/REVIEW matrix; `_finish` sets `enqueued_at` on `IN_QUEUE`. |
| `packages/vera_core/src/vera_core/services/queue_dispatcher.py` (modify) | for a RETRY candidate: build per-form `retry_fields` labels into room metadata; insert `CallLineage` after the call row. |
| `packages/vera_core/src/vera_core/config/settings.py` (modify) | `post_call_review_floor` 60 → 70. |
| `apps/agent_worker/src/agent_worker/prompt.py` (modify) | `build_instructions(tweak, *, retry_fields=None)` prepends a retry-focus block. |
| `apps/agent_worker/src/agent_worker/main.py` (modify) | read `retry_fields` from dispatch metadata, pass to `build_instructions`. |

---

## Task 1: Pure satisfaction + label helpers in `review.py`

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/review.py`
- Test: `tests/unit/forms/test_retryable_fields.py` (create; `tests/unit/forms/` exists)

**Interfaces:**
- Consumes: `all_required_paths(schema_json)` (v1, exists), `leaf_gates`/`is_applicable`/`is_required` (from `vera_core.forms.conditions`, exist), `is_v2` (from `vera_core.forms.conditions`), `FormSchemaDoc`, `COLLECTED_ROLES` (from `vera_core.forms.dsl`), `AnswerSource`.
- Produces:
  - `@dataclass(frozen=True) FieldStatus: filled: bool; source: str | None; ai_supported: bool | None; ai_confidence: int | None`
  - `def is_field_satisfied(status: FieldStatus, *, floor: int) -> bool`
  - `def retryable_required_paths(status_by_path: Mapping[str, FieldStatus], schema_json: Mapping[str, Any], *, floor: int) -> list[str]` — required(+applicable in v2) **askable** paths that are not satisfied.
  - `def field_labels(schema_json: Mapping[str, Any], paths: Sequence[str]) -> list[str]` — `Leaf.title` per path (v2), else the path.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/forms/test_retryable_fields.py
from vera_core.forms.review import (
    FieldStatus, is_field_satisfied, retryable_required_paths, field_labels,
)

FLOOR = 70

# Minimal v2 schema: one required askable leaf + one required readonly leaf.
V2 = {
    "dsl_version": "2.1",
    "sections": {
        "cov": {"section_key": "cov", "title": "Coverage", "role": "collect",
                "properties": {
                    "network_status": {"type": "text", "title": "Network status", "role": "ask", "required": True},
                    "plan_name": {"type": "text", "title": "Plan name", "role": "readonly", "required": True},
                }},
    },
    "tasks": [], "shared_conditions": {}, "system_fields": {},
}


def _unfilled(): return FieldStatus(filled=False, source=None, ai_supported=None, ai_confidence=None)
def _ai(conf, sup=True): return FieldStatus(filled=True, source="ai_call", ai_supported=sup, ai_confidence=conf)
def _human(): return FieldStatus(filled=True, source="human", ai_supported=None, ai_confidence=None)


def test_is_field_satisfied_rules():
    assert is_field_satisfied(_human(), floor=FLOOR) is True          # trusted
    assert is_field_satisfied(_ai(90), floor=FLOOR) is True           # ai supported, >=70
    assert is_field_satisfied(_ai(60), floor=FLOOR) is False          # ai <70
    assert is_field_satisfied(_ai(90, sup=False), floor=FLOOR) is False  # unsupported
    assert is_field_satisfied(_unfilled(), floor=FLOOR) is False


def test_retryable_only_unsatisfied_askable_required():
    p = "cov.network_status"
    # unfilled required askable -> retryable
    assert retryable_required_paths({p: _unfilled()}, V2, floor=FLOOR) == [p]
    # low-conf ai_call required askable -> retryable
    assert retryable_required_paths({p: _ai(50)}, V2, floor=FLOOR) == [p]
    # satisfied -> not retryable
    assert retryable_required_paths({p: _ai(90)}, V2, floor=FLOOR) == []
    # readonly required field never retryable even if unfilled (not askable)
    assert "cov.plan_name" not in retryable_required_paths({p: _ai(90)}, V2, floor=FLOOR)


def test_field_labels_uses_titles():
    assert field_labels(V2, ["cov.network_status"]) == ["Network status"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/unit/forms/test_retryable_fields.py -v`
Expected: FAIL with `ImportError: cannot import name 'FieldStatus'`.

- [ ] **Step 3: Implement the helpers in `review.py`**

```python
from collections.abc import Mapping, Sequence  # extend existing import
from dataclasses import dataclass

from vera_core.forms.conditions import is_applicable, is_required, is_v2, leaf_gates
from vera_core.forms.dsl import COLLECTED_ROLES, FormSchemaDoc


@dataclass(frozen=True)
class FieldStatus:
    filled: bool
    source: str | None
    ai_supported: bool | None
    ai_confidence: int | None


def is_field_satisfied(status: FieldStatus, *, floor: int) -> bool:
    if not status.filled:
        return False
    if status.source in (AnswerSource.INTAKE.value, AnswerSource.HUMAN.value):
        return True
    if status.source == AnswerSource.AI_CALL.value:
        return bool(status.ai_supported) and (status.ai_confidence or 0) >= floor
    return True  # unknown source but filled — treat as satisfied


def _required_askable_paths(schema_json: Mapping[str, Any], values: Mapping[str, Any]) -> list[str]:
    if is_v2(schema_json):
        doc = FormSchemaDoc.model_validate(schema_json)
        shared = doc.shared_conditions or {}
        return [
            path
            for path, leaf, gates in leaf_gates(doc)
            if leaf.role in COLLECTED_ROLES
            and is_applicable(gates, values, shared)
            and is_required(leaf, values, shared)
        ]
    # v1: no role concept — all required paths are candidates.
    return all_required_paths(schema_json)


def retryable_required_paths(
    status_by_path: Mapping[str, FieldStatus], schema_json: Mapping[str, Any], *, floor: int
) -> list[str]:
    values = {p: "x" for p, s in status_by_path.items() if s.filled}  # applicability needs filled-ness only
    out: list[str] = []
    for path in _required_askable_paths(schema_json, values):
        st = status_by_path.get(path) or FieldStatus(False, None, None, None)
        if not is_field_satisfied(st, floor=floor):
            out.append(path)
    return out


def field_labels(schema_json: Mapping[str, Any], paths: Sequence[str]) -> list[str]:
    if not is_v2(schema_json):
        return list(paths)
    doc = FormSchemaDoc.model_validate(schema_json)
    titles = {path: leaf.title for path, leaf, _ in leaf_gates(doc)}
    return [titles.get(p, p) for p in paths]
```

> **Implementer note:** confirm `is_v2` is exported from `vera_core.forms.conditions` (it is — `post_call_eval.py` imports it there). `all_required_paths` and `AnswerSource` are already imported in `review.py`. Adjust imports to avoid duplicates.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/unit/forms/test_retryable_fields.py -v`
Expected: PASS.

- [ ] **Step 5: Lint/type + commit**

Run: `cd vera-backend && uv run ruff check packages/vera_core/src/vera_core/forms/review.py && uv run mypy packages/vera_core/src/vera_core/forms/review.py`
```bash
git add packages/vera_core/src/vera_core/forms/review.py tests/unit/forms/test_retryable_fields.py
git commit -m "feat(forms): required-field satisfaction + retryable-paths + labels helpers"
```

---

## Task 2: `load_field_status` DB helper (PHI-free)

**Files:**
- Create: `packages/vera_core/src/vera_core/services/field_status.py`
- Test: `tests/integration/test_load_field_status.py` (create)

**Interfaces:**
- Consumes: `FieldStatus` (Task 1), models `FieldAnswer`, `FieldEvaluation`.
- Produces: `async def load_field_status(session: AsyncSession, form_id: UUID) -> dict[str, FieldStatus]` — one entry per **current** field, joining the latest `FieldEvaluation` for that answer. Selects columns only (`field_path`, `source`, `confidence`, eval `supported`) — **no values/evidence** (PHI-free).

- [ ] **Step 1: Write the failing test** (seed a form with one ai_call answer + eval, one human answer)

```python
# tests/integration/test_load_field_status.py
import pytest
from vera_core.services.field_status import load_field_status
pytestmark = pytest.mark.integration

@pytest.mark.asyncio
async def test_load_field_status_maps_source_conf_supported(seeded_form_with_answers):
    ctx = seeded_form_with_answers  # fixture: form with ai_call(path=a, conf=55, supported=False) + human(path=b)
    status = await load_field_status(ctx.session, ctx.form_id)
    assert status["cov.a"].source == "ai_call" and status["cov.a"].ai_confidence == 55
    assert status["cov.a"].ai_supported is False and status["cov.a"].filled is True
    assert status["cov.b"].source == "human" and status["cov.b"].ai_supported is None
```

Add a `seeded_form_with_answers` fixture to `tests/integration/conftest.py` (reuse the tenant/session fixtures; insert a `PatientForm`, then two `FieldAnswer` rows (`is_current=True`) and one `FieldEvaluation` for the ai_call answer). Model the seeding on `tests/integration/test_post_call_eval.py`'s fixture.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && just up && uv run pytest tests/integration/test_load_field_status.py -v`
Expected: FAIL with `ModuleNotFoundError: vera_core.services.field_status`.

- [ ] **Step 3: Implement**

```python
# packages/vera_core/src/vera_core/services/field_status.py
"""Load per-field satisfaction inputs for the retry decision — PHI-free (no values)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.forms.review import FieldStatus
from vera_core.models.field_answer import FieldAnswer, FieldEvaluation


async def load_field_status(session: AsyncSession, form_id: UUID) -> dict[str, FieldStatus]:
    # Latest evaluation per current answer (LEFT JOIN; an answer may have no eval).
    rows = (
        await session.execute(
            select(
                FieldAnswer.field_path,
                FieldAnswer.source,
                FieldAnswer.confidence,
                FieldEvaluation.supported,
            )
            .outerjoin(FieldEvaluation, FieldEvaluation.answer_id == FieldAnswer.id)
            .where(FieldAnswer.form_id == form_id, FieldAnswer.is_current.is_(True))
        )
    ).all()
    out: dict[str, FieldStatus] = {}
    for path, source, confidence, supported in rows:
        out[path] = FieldStatus(
            filled=True, source=source, ai_supported=supported, ai_confidence=confidence
        )
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/integration/test_load_field_status.py -v`
Expected: PASS.

- [ ] **Step 5: Lint/type + commit**

Run: `cd vera-backend && uv run ruff check packages/vera_core/src/vera_core/services/field_status.py && uv run mypy packages/vera_core/src/vera_core/services/field_status.py`
```bash
git add packages/vera_core/src/vera_core/services/field_status.py tests/integration/test_load_field_status.py tests/integration/conftest.py
git commit -m "feat(services): load_field_status — PHI-free per-field satisfaction inputs"
```

---

## Task 3: State machine — `AI_PROCESSING → IN_QUEUE` + generalized retry-cap guard

**Files:**
- Modify: `packages/vera_core/src/vera_core/services/form_state_machine.py`
- Test: `tests/unit/services/test_form_state_machine.py` (add cases)

**Interfaces:**
- Produces: `AI_PROCESSING → IN_QUEUE` legal; the retry-cap guard (`retry_count >= max → InvalidTransitionError`, else `retry_count += 1`) fires for `current in {CALL_FAILED, AI_PROCESSING}` when `target == IN_QUEUE`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/services/test_form_state_machine.py (add)
def test_ai_processing_to_in_queue_retries_and_increments():
    form = _form(FormStatus.AI_PROCESSING); form.retry_count = 0
    FormStateMachine().transition(form, FormStatus.IN_QUEUE, tenant_max_retries=3)
    assert form.status == FormStatus.IN_QUEUE.value and form.retry_count == 1


def test_ai_processing_to_in_queue_blocked_when_cap_hit():
    form = _form(FormStatus.AI_PROCESSING); form.retry_count = 3
    with pytest.raises(InvalidTransitionError):
        FormStateMachine().transition(form, FormStatus.IN_QUEUE, tenant_max_retries=3)
```

(Reuse the existing `_form` helper in that file. If it doesn't set `retry_count`, set it in the test.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd vera-backend && uv run pytest tests/unit/services/test_form_state_machine.py -k ai_processing_to_in_queue -v`
Expected: FAIL (edge not allowed).

- [ ] **Step 3: Implement**

Add `FormStatus.IN_QUEUE` to the `AI_PROCESSING` target set:
```python
    FormStatus.AI_PROCESSING: frozenset(
        {FormStatus.COMPLETED, FormStatus.CALL_FAILED, FormStatus.EXCEPTION_REVIEW, FormStatus.IN_QUEUE}
    ),
```
Generalize the guard (replace the `CALL_FAILED`-specific block):
```python
        _RETRY_SOURCES = (FormStatus.CALL_FAILED, FormStatus.AI_PROCESSING)
        if target == FormStatus.IN_QUEUE and current in _RETRY_SOURCES:
            if form.retry_count >= tenant_max_retries:
                raise InvalidTransitionError(current.value, target.value, reason="retries exhausted")
            form.retry_count += 1
```
(Define `_RETRY_SOURCES` as a module constant rather than inline if cleaner.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/unit/services/test_form_state_machine.py -v`
Expected: PASS (new + existing, incl. the existing `CALL_FAILED → IN_QUEUE` cases).

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/services/form_state_machine.py tests/unit/services/test_form_state_machine.py
git commit -m "feat(forms): AI_PROCESSING->IN_QUEUE retry edge + generalized retry-cap guard"
```

---

## Task 4: `settings.post_call_review_floor` 60 → 70

**Files:**
- Modify: `packages/vera_core/src/vera_core/config/settings.py:52`

**Interfaces:** Produces the unified satisfaction threshold (70).

- [ ] **Step 1: Change the default**

In `settings.py`, change:
```python
    post_call_review_floor: int = 70  # VERA_POST_CALL_REVIEW_FLOOR
```

- [ ] **Step 2: Verify nothing asserts the old default**

Run: `cd vera-backend && grep -rn "post_call_review_floor\|REVIEW_CONFIDENCE_FLOOR\|floor=60\|== 60" tests packages apps --include=*.py | grep -iv "livekit"`
Expected: no test asserts `60`. (If one does, update it to 70 and note it.)

- [ ] **Step 3: Commit**

```bash
git add packages/vera_core/src/vera_core/config/settings.py
git commit -m "chore(config): unify post-call confidence floor at 70"
```

---

## Task 5: `evaluate_call` — retry decision matrix

**Files:**
- Modify: `packages/vera_core/src/vera_core/services/post_call_eval.py`
- Test: `tests/integration/test_post_call_eval.py` (add cases)

**Interfaces:**
- Consumes: `retryable_required_paths`, `FieldStatus` (Task 1); `load_field_status` (Task 2); the generalized state machine (Task 3); `Tenant.max_retries` (already loaded as `tenant`).
- Produces: the status decision replacing `post_call_eval.py:307-309`, using a `token_fields` list (fields skipped by `has_phi_token` this call) split out from `reviewed`, and `_finish` setting `enqueued_at` on an `IN_QUEUE` target.

**Behavior (replace the `(9-12)` block):**
1. Track token-valued fields separately: where the extract loop currently does `reviewed.append(ef.field_path)` on `has_phi_token`, append to a new `token_fields: list[str]` instead (keep `reviewed` for the low-confidence/needs_review path).
2. After the completion-% recompute, build `status = await load_field_status(session, form_id)` and `retryable = retryable_required_paths(status, version.schema_json, floor=deps.floor)`.
3. Decide:
   - `token_fields` non-empty → `_finish(EXCEPTION_REVIEW, reason="token_value", reviewed=token_fields)`
   - `retryable` empty → `_finish(COMPLETED)`
   - `retryable` and `form.retry_count < tenant.max_retries` → `_finish(IN_QUEUE, reason="retry", reviewed=[])`
   - else → `_finish(EXCEPTION_REVIEW, reason="retries_exhausted", reviewed=retryable)`
4. Extend `_finish`: when `target == FormStatus.IN_QUEUE`, set `form.enqueued_at = func.now()` after the successful transition (so the dispatcher orders it). Import `func` from sqlalchemy if not already.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_post_call_eval.py (add)
@pytest.mark.asyncio
async def test_incomplete_with_retries_left_requeues(seeded_ai_processing_form, fake_audit, fake_livekit):
    ctx = seeded_ai_processing_form  # form with a required askable field the LLM will NOT fill
    turns = [TranscriptTurn(0, "user", "sorry I cannot share that")]
    llm = FakeLLMClient(extracted=[], verdicts=[])  # nothing extracted -> required field stays unfilled
    outcome = await evaluate_call(ctx.session, EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit),
        tenant_id=ctx.tenant_id, form_id=ctx.form_id, call_id=ctx.call_id, turns=turns)
    assert outcome.status == FormStatus.IN_QUEUE
    form = await ctx.reload_form()
    assert form.status == "in_queue" and form.retry_count == 1 and form.enqueued_at is not None


@pytest.mark.asyncio
async def test_incomplete_retries_exhausted_goes_to_review(seeded_ai_processing_form_maxed, fake_audit, fake_livekit):
    ctx = seeded_ai_processing_form_maxed  # retry_count == tenant.max_retries
    llm = FakeLLMClient(extracted=[], verdicts=[])
    outcome = await evaluate_call(ctx.session, EvalDeps(llm=llm, audit=fake_audit, livekit=fake_livekit),
        tenant_id=ctx.tenant_id, form_id=ctx.form_id, call_id=ctx.call_id, turns=[TranscriptTurn(0,"user","no")])
    assert outcome.status == FormStatus.EXCEPTION_REVIEW
```

Extend the existing fixture(s) so the seeded form's schema has at least one **required askable** field, and add a `_maxed` variant with `retry_count == max_retries`. The existing happy-path test (`test_evaluate_call_writes_answers_and_completes`) must still pass — ensure its schema's required fields are all satisfied by the extracted answer (adjust the fixture schema so the single extracted field IS the only required field), else it will now re-queue instead of completing. **Update that test's fixture accordingly and note it in the report.**

- [ ] **Step 2: Run to verify they fail**

Run: `cd vera-backend && uv run pytest tests/integration/test_post_call_eval.py -k "requeues or exhausted" -v`
Expected: FAIL (currently routes to COMPLETED/EXCEPTION_REVIEW without the retry branch).

- [ ] **Step 3: Implement the decision + `_finish` change**

Split token tracking in the extract loop and replace the decision block:
```python
    # decision (replaces `target = EXCEPTION_REVIEW if reviewed else COMPLETED`)
    status = await load_field_status(session, form_id)
    retryable = retryable_required_paths(status, version.schema_json, floor=deps.floor)
    if token_fields:
        return await _finish(FormStatus.EXCEPTION_REVIEW, written=len(kept),
                             reviewed=token_fields, reason="token_value")
    if not retryable:
        return await _finish(FormStatus.COMPLETED, written=len(kept), reviewed=[])
    if form.retry_count < tenant.max_retries:
        return await _finish(FormStatus.IN_QUEUE, written=len(kept), reviewed=[], reason="retry")
    return await _finish(FormStatus.EXCEPTION_REVIEW, written=len(kept),
                         reviewed=retryable, reason="retries_exhausted")
```
In `_finish`, after `sm.transition(...)`:
```python
        if target == FormStatus.IN_QUEUE:
            form.enqueued_at = func.now()
```
Add imports: `retryable_required_paths` from `vera_core.forms.review`, `load_field_status` from `vera_core.services.field_status`. Rename the `has_phi_token` branch's `reviewed.append` to `token_fields.append`; initialize `token_fields: list[str] = []` near `reviewed`.

> **Note:** `_finish` already calls `try_dispatch` after the transition — for the `IN_QUEUE` (retry) branch that correctly kicks the dispatcher to create the retry call.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/integration/test_post_call_eval.py -v`
Expected: PASS (new + the adjusted happy-path/redelivery/token/llm-failure tests).

- [ ] **Step 5: Lint/type + commit**

Run: `cd vera-backend && uv run ruff check packages/vera_core/src/vera_core/services/post_call_eval.py && uv run mypy packages/vera_core/src/vera_core/services/post_call_eval.py`
```bash
git add packages/vera_core/src/vera_core/services/post_call_eval.py tests/integration/test_post_call_eval.py
git commit -m "feat(post-call): retry decision — requeue incomplete forms, review when exhausted"
```

---

## Task 6: Dispatcher — `retry_fields` metadata + `call_lineage`

**Files:**
- Modify: `packages/vera_core/src/vera_core/services/queue_dispatcher.py`
- Test: `tests/integration/control_plane/test_call_queue.py` (add)

**Interfaces:**
- Consumes: `load_field_status` (Task 2), `retryable_required_paths` + `field_labels` (Task 1), `CallLineage`, `SchemaVersion`, `Call`.
- Produces: for a `RETRY`-mode dispatch, the room metadata dict gains `retry_fields: list[str]` (labels), and a `CallLineage(parent_call_id, retry_call_id)` row is inserted (parent = most-recent prior `call` for the form).

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/control_plane/test_call_queue.py (add)
@pytest.mark.asyncio
async def test_retry_dispatch_attaches_retry_fields_and_lineage(retry_form_ctx, fake_livekit_capture):
    # retry_form_ctx: a form with retry_count=1, IN_QUEUE, a prior COMPLETED call, and a required
    # askable field left unfilled; fake_livekit_capture records create_call_room(room, metadata).
    await try_dispatch(retry_form_ctx.session, retry_form_ctx.tenant_id, fake_livekit_capture)
    md = fake_livekit_capture.last_metadata
    assert "retry_fields" in md and md["retry_fields"]  # labels present
    # new call is mode=RETRY and a lineage row links it to the prior call
    lineage = await retry_form_ctx.get_lineage_for_form(retry_form_ctx.form_id)
    assert lineage.parent_call_id == retry_form_ctx.prior_call_id
```

Add `retry_form_ctx` + `fake_livekit_capture` to the test's conftest (the fake livekit records the `metadata` arg of `create_call_room`). Model seeding on the existing call-queue fixtures.

- [ ] **Step 2: Run to verify it fails**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_call_queue.py -k retry_dispatch_attaches -v`
Expected: FAIL (no `retry_fields`, no lineage).

- [ ] **Step 3: Implement in `try_dispatch`**

In the per-form loop, when `call_mode == CallMode.RETRY`, build per-form metadata and lineage. Inside the `session.begin_nested()` block, after `session.add(call); await session.flush()` and room creation:
```python
        call_metadata = metadata  # tenant-level base
        parent_call_id = None
        if call_mode == CallMode.RETRY:
            version = (await session.execute(
                select(SchemaVersion).where(SchemaVersion.id == form.schema_version_id))).scalar_one()
            status = await load_field_status(session, form.id)
            labels = field_labels(
                version.schema_json,
                retryable_required_paths(status, version.schema_json, floor=settings_floor),
            )[:MAX_RETRY_FIELDS]
            call_metadata = {**metadata, "retry_fields": labels}
            parent_call_id = (await session.execute(
                select(Call.id).where(Call.form_id == form.id, Call.id != call.id)
                .order_by(Call.created_at.desc()).limit(1))).scalar_one_or_none()
        # use call_metadata in create_call_room(...)
        # after the call row + event:
        if parent_call_id is not None:
            session.add(CallLineage(tenant_id=tenant_id, parent_call_id=parent_call_id, retry_call_id=call.id))
```
Adjust the existing `create_call_room(room_name, metadata=metadata)` to use `call_metadata`. Define `MAX_RETRY_FIELDS = 25` module constant. The floor: read `settings.post_call_review_floor` — `try_dispatch` does not currently take settings; pass it or read via the tenant/config already available. **Check `try_dispatch`'s signature** — if it has no settings access, thread `floor: int = 70` as a keyword with the settings default at the call sites, or load `get_settings().post_call_review_floor` inside (match how the module reads other config). Keep it PHI-free: only labels enter metadata.

> **Note:** computing `retryable_required_paths` here (labels for the prompt) is best-effort and token-unaware; the retry-vs-review decision was already made authoritatively in `evaluate_call` (Task 5). A RETRY candidate always has ≥1 genuinely-retryable field, so `labels` is non-empty in practice; if it is empty, still dispatch (the worker falls back to the full script).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_call_queue.py -v`
Expected: PASS (new + existing dispatch tests).

- [ ] **Step 5: Lint/type + commit**

Run: `cd vera-backend && uv run ruff check packages/vera_core/src/vera_core/services/queue_dispatcher.py && uv run mypy packages/vera_core/src/vera_core/services/queue_dispatcher.py`
```bash
git add packages/vera_core/src/vera_core/services/queue_dispatcher.py tests/integration/control_plane/test_call_queue.py
git commit -m "feat(dispatch): retry_fields metadata + call_lineage on RETRY calls"
```

---

## Task 7: Worker partial prompt (`retry_fields` nudge)

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/prompt.py`
- Modify: `apps/agent_worker/src/agent_worker/main.py`
- Test: `tests/unit/worker/test_retry_prompt.py` (create; imports only `prompt.py` + `vera_core`, no livekit)

**Interfaces:**
- Produces: `build_instructions(tweak: PersonaTweak | None, *, retry_fields: list[str] | None = None) -> str` — prepends a retry-focus block when `retry_fields` is a non-empty list; unchanged otherwise. `main.py` reads `meta.get("retry_fields")` and passes it.

- [ ] **Step 1: Write the failing test** (pure string behavior, no livekit import)

```python
# tests/unit/worker/test_retry_prompt.py
from agent_worker.prompt import build_instructions

def test_retry_fields_prepends_focus_block():
    out = build_instructions(None, retry_fields=["Network status", "Specialist copay"])
    assert "RETRY" in out.upper()
    assert "Network status" in out and "Specialist copay" in out
    # base script still present
    assert "verifying insurance coverage" in out.lower()

def test_no_retry_fields_is_unchanged():
    assert build_instructions(None, retry_fields=None) == build_instructions(None)
    assert build_instructions(None, retry_fields=[]) == build_instructions(None)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd vera-backend && uv run pytest tests/unit/worker/test_retry_prompt.py -v`
Expected: FAIL (`build_instructions` has no `retry_fields` param).

- [ ] **Step 3: Implement**

In `prompt.py`, read the current `build_instructions` signature and add the keyword param + a prepended block when present:
```python
def build_instructions(tweak: PersonaTweak | None = None, *, retry_fields: list[str] | None = None) -> str:
    base = <existing body that builds the instruction string>
    if retry_fields:
        focus = (
            "RETRY CALL. A previous call already collected most of this verification. "
            "You must collect ONLY the following still-missing data points, confirm them, "
            "then politely close and end the call: " + ", ".join(retry_fields) + ". "
            "Do not re-verify anything else.\n\n"
        )
        return focus + base
    return base
```
Preserve the exact existing behavior for the no-`retry_fields` path (wrap current logic; don't change it).

In `main.py`, where instructions are built (`instructions = build_instructions(tweak)`, ~line 266), pass the metadata field:
```python
    retry_fields = meta.get("retry_fields") if isinstance(meta, dict) else None
    instructions = build_instructions(tweak, retry_fields=retry_fields)
```
(`meta` is the parsed dispatch metadata dict already present at `main.py:242`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd vera-backend && uv run pytest tests/unit/worker/test_retry_prompt.py -v`
Expected: PASS.

> If agent_worker collection fails on `livekit.agents` for OTHER files, run this file in isolation (as above) — it does not import livekit. Note that in the report.

- [ ] **Step 5: Lint/type + commit**

Run: `cd vera-backend && uv run ruff check apps/agent_worker/src/agent_worker/prompt.py apps/agent_worker/src/agent_worker/main.py`
```bash
git add apps/agent_worker/src/agent_worker/prompt.py apps/agent_worker/src/agent_worker/main.py tests/unit/worker/test_retry_prompt.py
git commit -m "feat(worker): retry_fields partial-prompt nudge in build_instructions"
```

---

## Self-review

**Spec coverage:**
- §2 satisfaction/decision matrix → Task 1 (helpers) + Task 5 (evaluate_call). ✅
- §3 missing-fields calc (shared, PHI-free) → Task 1 (pure) + Task 2 (DB load). ✅
- §4 partial prompt (metadata nudge) → Task 6 (dispatcher metadata) + Task 7 (worker). ✅
- §5 lineage (single point, both retry sources) → Task 6. ✅
- §6 state machine (edge + generalized guard + enqueued_at) → Task 3 + Task 5 (`_finish`). ✅
- §7 threshold 70 → Task 4. ✅
- §8 edge cases (cap, nothing-askable, concurrent edit, token→review) → Task 1 (askable filter), Task 5 (token split + cap), Task 6 (recompute at dispatch). ✅
- §9 testing → per-task unit + integration. ✅

**Placeholder scan:** The only `<existing body>` placeholder (Task 7 Step 3) instructs the implementer to wrap the current `build_instructions` body verbatim — it's a "preserve existing code" directive with the file named, not a missing spec. No `TBD`/`add error handling`.

**Type consistency:** `FieldStatus` (Task 1) consumed unchanged in Tasks 2 & 5. `retryable_required_paths(status_by_path, schema_json, *, floor)` and `field_labels(schema_json, paths)` signatures identical across Tasks 1/5/6. `load_field_status(session, form_id) -> dict[str, FieldStatus]` consistent Tasks 2/5/6. `build_instructions(tweak, *, retry_fields=None)` consistent Tasks 7. `_RETRY_SOURCES` guard (Task 3) matches the `IN_QUEUE` transitions used in Task 5.
