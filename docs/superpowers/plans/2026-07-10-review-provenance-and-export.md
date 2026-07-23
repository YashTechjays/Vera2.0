# Review Provenance & Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the post-call pipeline's record (per-field provenance, call-attempt timeline, review reason) to the reviewer, and add an XLSX export of COMPLETED forms with a disclosure ledger + audit (spec: `docs/superpowers/specs/2026-07-10-review-provenance-and-export-design.md`).

**Architecture:** New read-side service `vera_core/services/call_provenance.py` (attempt numbering, lineage, snapshot diffs, judge join) feeds three consumers: the extended form-detail response, a new `GET /patient-forms/{id}/calls` endpoint, and the export builder. Export is a pure openpyxl mapping layer + one streaming endpoint that writes an `export_artifact` ledger row and a `FORM_EXPORTED` audit. Frontend extends the existing modal (provenance tooltip, Call history tab, Export button) and worklist (Needs Review tab).

**Tech Stack:** Python 3.12, SQLAlchemy async, FastAPI, openpyxl, pytest; React + TypeScript, shadcn/ui, Tailwind, vitest.

## Global Constraints

- **PHI never in logs/URLs/filenames.** Export filename is `ibv-{form_id}.xlsx` (opaque UUID). `Cache-Control: no-store` on every PHI response. Audit `detail` carries field **names/counts** only. `changed_paths` carries **paths only**, never values.
- **Timestamps from the DB clock** (`func.now()` / model mixins), never `datetime.now()`.
- **All DB work inside a tenant-scoped session** (RLS `SET LOCAL app.tenant_id`); new table `export_artifact` gets the standard tenant RLS policy.
- **Migrations idempotent** (`ADD COLUMN IF NOT EXISTS`, `checkfirst=True`, `ON CONFLICT DO NOTHING`, guarded `CREATE POLICY`) — the CI gate runs `alembic upgrade head` from `0001` on a fresh DB where `create_all` already made model-derived DDL. Revision IDs are alembic's random hex via `just makemigration` — never hand-numbered. Autogenerate emits known drift ops (`ix_audit_log_tenant_seq`, `ix_auth_audit_log_*` index drops) — delete them, keep only your change.
- **Response contract:** `ResponseModel[T]` via `ok()`, errors via `CustomAPIException` subclasses. The export endpoint is the one binary exception (raw `fastapi.Response`); its errors still ride the envelope.
- **PEP 695 type params; asyncio only; mypy --strict; ruff.**
- **New backend tests under `vera-backend/tests/unit/<area>/` and `vera-backend/tests/integration/`** — NOT `packages/vera_core/tests/`. Verify collection: `uv run pytest --collect-only -q | grep <name>`.
- **Verification gate:** backend `just check` (2 pre-existing local mypy livekit errors + `test_platform_mfa_enroll.py` DB-drift failures are known local noise, green on CI); frontend `npm run build` + `npm run lint` + `npm test`. After all tasks: run `/simplify`, re-check, commit its output.
- **Commit trailers:** end commit bodies with the repo `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` / `Claude-Session:` lines.
- All backend commands run from `vera-backend/`, frontend commands from `vera-frontend/`.

---

## File structure

| File | Responsibility |
|---|---|
| `packages/vera_core/src/vera_core/services/call_provenance.py` (create) | `CallAttempt`/`FieldProvenance`/`JudgeInfo` dataclasses; pure `snapshot_changed_paths`; DB loaders `load_call_attempts`, `load_field_provenance`. |
| `packages/vera_core/src/vera_core/forms/export.py` (create) | Pure `build_workbook(...) -> bytes` (openpyxl; Form + Provenance sheets). |
| `packages/vera_core/src/vera_core/models/export_artifact.py` (create) | `ExportArtifact` ledger model. |
| `packages/vera_core/src/vera_core/models/patient_form.py` (modify) | `review_reason` column. |
| `packages/vera_core/src/vera_core/models/audit_log.py` (modify) | `AuditEvent.FORM_EXPORTED`. |
| `packages/vera_core/src/vera_core/models/rbac_defaults.py` (modify) | `forms:export` permission + TENANT_ADMIN/SUPERVISOR grants. |
| `packages/vera_core/src/vera_core/services/post_call_eval.py` (modify) | `_finish` stamps/clears `review_reason`. |
| `migrations/versions/` (3 new) | review_reason column; export_artifact table + RLS; forms:export seed. |
| `apps/control_plane/src/control_plane/api/v1/patient_forms.py` (modify) | provenance on `FieldView`; `review_reason` on summary + clear on manual transition; `GET .../calls`; `POST .../export`. |
| `vera-frontend/src/lib/patient-forms/types.ts`, `api.ts`, `display.ts` (modify) | new DTOs, API fns, `ageLabel`. |
| `vera-frontend/src/lib/api/client.ts` (modify) | `apiRequestBlob`. |
| `vera-frontend/src/components/ibv/IbvProvider.tsx` (modify) | expose `formId` + `provenanceFor`. |
| `vera-frontend/src/components/ibv/FieldRow.tsx` (modify) | provenance tooltip. |
| `vera-frontend/src/components/ibv/CallHistoryTab.tsx` (create) | attempt timeline tab. |
| `vera-frontend/src/components/ibv/IbvFormModal.tsx` (modify) | Form/Call-history tabs + Export button. |
| `vera-frontend/src/pages/DataManagement.tsx` (modify) | Needs Review tab + Reason/Age columns. |

---

## Task 1: `review_reason` — column, stamp, clear, expose

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/patient_form.py` (after `retry_count`)
- Modify: `packages/vera_core/src/vera_core/services/post_call_eval.py` (`_finish`)
- Modify: `apps/control_plane/src/control_plane/api/v1/patient_forms.py` (`PatientFormSummary`, list serializer, `update_patient_form_status`)
- Create: migration (via `just makemigration`)
- Test: `tests/integration/test_post_call_eval.py` (add cases), `tests/integration/control_plane/test_patient_forms_review.py` (add case)

**Interfaces:**
- Produces: `PatientForm.review_reason: str | None`; `PatientFormSummary.review_reason: str | None`; `_finish` sets `form.review_reason = reason if target == FormStatus.EXCEPTION_REVIEW else None`; the manual status endpoint sets it to `None` when leaving EXCEPTION_REVIEW.

- [ ] **Step 1: Write the failing tests**

In `tests/integration/test_post_call_eval.py`, extend the existing retries-exhausted test's asserts and add a clearing case (reuse the existing fixtures — `seeded_ai_processing_form_maxed` already routes to EXCEPTION_REVIEW with reason `retries_exhausted`; `seeded_ai_processing_form` routes to IN_QUEUE):

```python
# In test_incomplete_retries_exhausted_goes_to_review, after the existing status assert:
    form = await ctx.reload_form()
    assert form.review_reason == "retries_exhausted"

# In test_incomplete_with_retries_left_requeues, after the existing asserts:
    assert form.review_reason is None  # non-review outcome always clears it
```

In `tests/integration/control_plane/test_patient_forms_review.py` add (reuse that file's existing client/admin fixtures and status helper — model on the neighboring status-transition tests):

```python
@pytest.mark.asyncio
async def test_requeue_clears_review_reason(
    client, rbac_world, review_form_id, admin_sessionmaker
) -> None:
    """Leaving EXCEPTION_REVIEW by hand nulls review_reason."""
    # Seed the reason directly (the pipeline normally stamps it).
    async with admin_sessionmaker() as session, session.begin():
        await session.execute(
            text("UPDATE patient_form SET review_reason = 'llm_error' WHERE id = :fid").bindparams(
                fid=review_form_id
            )
        )
    resp = await client.put(
        f"/api/v1/patient-forms/{review_form_id}/status",
        headers={"Authorization": f"Bearer {rbac_world.admin_token}"},
        json={"status": "in_queue"},
    )
    assert resp.status_code == 200, resp.text
    async with admin_sessionmaker() as session:
        reason = (
            await session.execute(
                text("SELECT review_reason FROM patient_form WHERE id = :fid").bindparams(
                    fid=review_form_id
                )
            )
        ).scalar_one()
    assert reason is None
```

Use whatever fixture in that file seeds a form in EXCEPTION_REVIEW (grep the file for `exception_review` — a fixture exists for the manual-transition tests; name it `review_form_id` here to match, or adapt to the actual fixture name).

- [ ] **Step 2: Run to verify they fail**

Run: `cd vera-backend && uv run pytest tests/integration/test_post_call_eval.py -k "exhausted or requeues" tests/integration/control_plane/test_patient_forms_review.py -k review_reason -v`
Expected: FAIL — `review_reason` column does not exist / attribute error.

- [ ] **Step 3: Implement**

`patient_form.py` — after the `retry_count` column:

```python
    # Why the pipeline sent this form to EXCEPTION_REVIEW (token_value,
    # retries_exhausted, llm_error, no_transcript). NULL outside review and for
    # manual transitions — a pipeline artifact, not a user field. Not PHI.
    review_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
```

`post_call_eval.py` — in `_finish`, right after the `sm.transition(...)` call (before the `enqueued_at` block):

```python
        form.review_reason = reason if target == FormStatus.EXCEPTION_REVIEW else None
```

`patient_forms.py` —
1. `PatientFormSummary`: add `review_reason: str | None` after `completion_pct`.
2. The list serializer (`PatientFormSummary(...)` construction in `list_patient_forms`): add `review_reason=r.review_reason,`.
3. `update_patient_form_status`: after the successful `sm.transition(form, target, ...)` call add:

```python
    if current == FormStatus.EXCEPTION_REVIEW:
        form.review_reason = None
```

Migration: run `just makemigration` (message: `patient_form review_reason`), delete every autogenerated op (including the known drift index ops), and replace with:

```python
def upgrade() -> None:
    op.execute("ALTER TABLE patient_form ADD COLUMN IF NOT EXISTS review_reason VARCHAR(32)")


def downgrade() -> None:
    op.execute("ALTER TABLE patient_form DROP COLUMN IF EXISTS review_reason")
```

Run `uv run alembic upgrade head` against the local DB if it is at head; if the local DB has drift (known local issue), rely on the integration suite's migrated test DB.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && uv run pytest tests/integration/test_post_call_eval.py tests/integration/control_plane/test_patient_forms_review.py -v`
Expected: PASS (new + existing).

- [ ] **Step 5: Lint/type + commit**

Run: `cd vera-backend && uv run ruff check . && uv run ruff format . && uv run mypy packages/vera_core/src/vera_core apps/control_plane/src/control_plane/api/v1/patient_forms.py`

```bash
git add -A && git commit -m "feat(forms): review_reason column — stamped by evaluate_call, cleared on manual exit, on the worklist row"
```

---

## Task 2: `call_provenance` service — diff helper + loaders

**Files:**
- Create: `packages/vera_core/src/vera_core/services/call_provenance.py`
- Test: `tests/unit/services/test_snapshot_diff.py` (create), `tests/integration/test_call_provenance.py` (create)

**Interfaces:**
- Consumes: models `Call`, `CallLineage`, `CallFormSnapshot`, `FieldAnswer`, `FieldEvaluation`; `AnswerSource`.
- Produces (exact signatures used by Tasks 3, 4, 7):

```python
@dataclass(frozen=True)
class JudgeInfo:
    confidence: int | None
    supported: bool
    evidence: str | None

@dataclass(frozen=True)
class FieldProvenance:
    attempt: int              # 1-based call order
    mode: str                 # "full" | "retry"
    judge: JudgeInfo | None

@dataclass(frozen=True)
class CallAttempt:
    id: UUID
    attempt: int
    mode: str
    status: str
    created_at: datetime
    retry_of: UUID | None
    changed_paths: list[str]

def snapshot_changed_paths(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> list[str]: ...
async def load_call_attempts(session: AsyncSession, form_id: UUID) -> list[CallAttempt]: ...
async def load_field_provenance(
    session: AsyncSession, form_id: UUID, attempt_by_call: Mapping[UUID, tuple[int, str]]
) -> dict[str, FieldProvenance]: ...
```

- [ ] **Step 1: Write the failing unit test**

```python
# tests/unit/services/test_snapshot_diff.py
from vera_core.services.call_provenance import snapshot_changed_paths


def test_changed_added_removed_paths_sorted() -> None:
    before = {"a": "1", "b": "2", "c": "3"}
    after = {"a": "1", "b": "9", "d": "4"}  # b changed, c removed, d added
    assert snapshot_changed_paths(before, after) == ["b", "c", "d"]


def test_none_and_missing_snapshots_are_empty() -> None:
    assert snapshot_changed_paths(None, None) == []
    assert snapshot_changed_paths({}, {}) == []


def test_absent_key_differs_from_present_none() -> None:
    # A key whose value is None is still "present" — only true absence/difference counts.
    assert snapshot_changed_paths({"a": None}, {"a": None}) == []
    assert snapshot_changed_paths({}, {"a": None}) == ["a"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd vera-backend && uv run pytest tests/unit/services/test_snapshot_diff.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the module**

```python
# packages/vera_core/src/vera_core/services/call_provenance.py
"""Read-side helpers for call attempt history and per-field provenance.

Loads which call wrote each current AI answer, the judge's latest verdict, the
form's attempt timeline (lineage + snapshot diffs). PHI discipline: snapshot
values are read to compute diffs but only field *paths* leave this module;
judge `evidence` is de-identified (tokenized before the LLM saw it).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.models.call import Call, CallFormSnapshot, CallLineage
from vera_core.models.enums import AnswerSource
from vera_core.models.field_answer import FieldAnswer, FieldEvaluation

_MISSING = object()


@dataclass(frozen=True)
class JudgeInfo:
    confidence: int | None
    supported: bool
    evidence: str | None


@dataclass(frozen=True)
class FieldProvenance:
    attempt: int
    mode: str
    judge: JudgeInfo | None


@dataclass(frozen=True)
class CallAttempt:
    id: UUID
    attempt: int
    mode: str
    status: str
    created_at: datetime
    retry_of: UUID | None
    changed_paths: list[str]


def snapshot_changed_paths(
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None
) -> list[str]:
    """Field paths whose value differs between a call's before/after snapshots.
    Paths only — values never leave. Tolerates None/partial snapshots."""
    b = before or {}
    a = after or {}
    return sorted(p for p in set(b) | set(a) if b.get(p, _MISSING) != a.get(p, _MISSING))


async def load_call_attempts(session: AsyncSession, form_id: UUID) -> list[CallAttempt]:
    """The form's calls as a 1-based attempt timeline, oldest first
    (created_at, then id — UUIDv7 — as the deterministic tie-break)."""
    calls = (
        await session.execute(
            select(Call.id, Call.mode, Call.current_status, Call.created_at)
            .where(Call.form_id == form_id)
            .order_by(Call.created_at.asc(), Call.id.asc())
        )
    ).all()
    if not calls:
        return []
    ids = [c.id for c in calls]
    retry_of = dict(
        (
            await session.execute(
                select(CallLineage.retry_call_id, CallLineage.parent_call_id).where(
                    CallLineage.retry_call_id.in_(ids)
                )
            )
        )
        .tuples()
        .all()
    )
    snapshots = {
        row.call_id: (row.before_state, row.after_state)
        for row in (
            await session.execute(
                select(
                    CallFormSnapshot.call_id,
                    CallFormSnapshot.before_state,
                    CallFormSnapshot.after_state,
                ).where(CallFormSnapshot.call_id.in_(ids))
            )
        ).all()
    }
    out: list[CallAttempt] = []
    for attempt, c in enumerate(calls, start=1):
        before, after = snapshots.get(c.id, (None, None))
        out.append(
            CallAttempt(
                id=c.id,
                attempt=attempt,
                mode=c.mode,
                status=c.current_status,
                created_at=c.created_at,
                retry_of=retry_of.get(c.id),
                changed_paths=snapshot_changed_paths(before, after),
            )
        )
    return out


async def load_field_provenance(
    session: AsyncSession, form_id: UUID, attempt_by_call: Mapping[UUID, tuple[int, str]]
) -> dict[str, FieldProvenance]:
    """Per-path provenance for the form's current ai_call answers: which attempt
    wrote it (via *attempt_by_call*, from load_call_attempts) + the latest judge
    verdict. Same latest-eval MAX(created_at) join as load_field_status."""
    latest_eval = (
        select(
            FieldEvaluation.answer_id,
            func.max(FieldEvaluation.created_at).label("max_created_at"),
        )
        .group_by(FieldEvaluation.answer_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                FieldAnswer.field_path,
                FieldAnswer.call_id,
                FieldEvaluation.confidence,
                FieldEvaluation.supported,
                FieldEvaluation.evidence,
            )
            .outerjoin(latest_eval, latest_eval.c.answer_id == FieldAnswer.id)
            .outerjoin(
                FieldEvaluation,
                (FieldEvaluation.answer_id == FieldAnswer.id)
                & (FieldEvaluation.created_at == latest_eval.c.max_created_at),
            )
            .where(
                FieldAnswer.form_id == form_id,
                FieldAnswer.is_current.is_(True),
                FieldAnswer.source == AnswerSource.AI_CALL.value,
                FieldAnswer.call_id.is_not(None),
            )
        )
    ).all()
    out: dict[str, FieldProvenance] = {}
    for path, call_id, confidence, supported, evidence in rows:
        am = attempt_by_call.get(call_id)
        if am is None:  # answer's call not in the timeline (shouldn't happen) — skip
            continue
        judge = (
            JudgeInfo(confidence=confidence, supported=supported, evidence=evidence)
            if supported is not None
            else None
        )
        out[path] = FieldProvenance(attempt=am[0], mode=am[1], judge=judge)
    return out
```

- [ ] **Step 4: Run the unit test**

Run: `cd vera-backend && uv run pytest tests/unit/services/test_snapshot_diff.py -v`
Expected: PASS.

- [ ] **Step 5: Write the failing integration test**

Seed via a superuser engine exactly like `tests/integration/control_plane/test_call_queue.py::retry_form_ctx` does (Tenant → find-or-create FormSchema → SchemaVersion (version=996) → PatientForm → two Calls + CallLineage + CallFormSnapshot + FieldAnswer + FieldEvaluation), with FK-ordered teardown. Key content:

```python
# tests/integration/test_call_provenance.py
import pytest

from vera_core.services.call_provenance import load_call_attempts, load_field_provenance

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_attempts_lineage_and_diffs(two_call_form_ctx) -> None:
    """Fixture seeds: call1 (full, completed, snapshot {} -> {'cov.a': 'x'}) then
    call2 (retry, completed, snapshot {'cov.a': 'x'} -> {'cov.a': 'x', 'cov.b': 'y'},
    lineage call2->call1); current ai_call answer for cov.b from call2 with one
    FieldEvaluation(confidence=88, supported=True, evidence='said y')."""
    ctx = two_call_form_ctx
    attempts = await load_call_attempts(ctx.session, ctx.form_id)
    assert [a.attempt for a in attempts] == [1, 2]
    assert attempts[0].mode == "full" and attempts[1].mode == "retry"
    assert attempts[1].retry_of == attempts[0].id
    assert attempts[0].changed_paths == ["cov.a"]
    assert attempts[1].changed_paths == ["cov.b"]

    prov = await load_field_provenance(
        ctx.session, ctx.form_id, {a.id: (a.attempt, a.mode) for a in attempts}
    )
    assert prov["cov.b"].attempt == 2 and prov["cov.b"].mode == "retry"
    assert prov["cov.b"].judge is not None
    assert prov["cov.b"].judge.confidence == 88 and prov["cov.b"].judge.supported is True
```

Add the `two_call_form_ctx` fixture in this file (session-scoped engine per test is fine; copy the seeding/teardown skeleton from `retry_form_ctx` and extend it with the second call, lineage, snapshots, answer, and evaluation rows; the ctx dataclass carries `session`, `form_id`).

- [ ] **Step 6: Run it**

Run: `cd vera-backend && just up && uv run pytest tests/integration/test_call_provenance.py -v`
Expected: PASS (implementation exists from Step 3; the test validates the SQL against real Postgres).

- [ ] **Step 7: Lint/type + commit**

Run: `cd vera-backend && uv run ruff check packages/vera_core/src/vera_core/services/call_provenance.py && uv run mypy packages/vera_core/src/vera_core/services/call_provenance.py`

```bash
git add -A && git commit -m "feat(services): call_provenance — attempt timeline, lineage, snapshot diffs, judge join"
```

---

## Task 3: Form-detail provenance (`FieldView.provenance`)

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/patient_forms.py` (`FieldView`, `_build_detail`, new view models)
- Test: `tests/integration/control_plane/test_form_provenance.py` (create)

**Interfaces:**
- Consumes: `load_call_attempts`, `load_field_provenance`, `FieldProvenance` (Task 2).
- Produces (JSON shape the frontend Task 9 consumes):

```python
class JudgeView(BaseModel):
    confidence: int | None
    supported: bool
    evidence: str | None

class ProvenanceView(BaseModel):
    attempt: int
    mode: str
    judge: JudgeView | None

class FieldView(BaseModel):
    field_path: str
    value: Any
    source: str
    confidence: int | None
    dispute: DisputeView | None
    provenance: ProvenanceView | None = None
```

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/control_plane/test_form_provenance.py
import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_detail_carries_provenance_for_ai_fields(
    client, rbac_world, provenance_form_id
) -> None:
    """provenance_form_id: form with one ai_call answer (from a retry call, judged
    supported/88) and one human answer. AI field gets provenance; human gets null."""
    resp = await client.get(
        f"/api/v1/patient-forms/{provenance_form_id}",
        headers={"Authorization": f"Bearer {rbac_world.admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    fields = {f["field_path"]: f for f in resp.json()["data"]["fields"]}
    ai = fields["cov.b"]
    assert ai["provenance"]["mode"] == "retry"
    assert ai["provenance"]["attempt"] == 2
    assert ai["provenance"]["judge"]["confidence"] == 88
    assert ai["provenance"]["judge"]["supported"] is True
    human = fields["cov.a"]
    assert human["provenance"] is None
```

Fixture `provenance_form_id`: seed under `rbac_world.tenant_id` (so the authed client can see it through RLS) with the same two-call shape as Task 2's fixture plus a current human `FieldAnswer` for `cov.a`. Put the fixture in this test file; FK-ordered teardown as in `test_call_queue.py`.

- [ ] **Step 2: Run to verify it fails**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_form_provenance.py -v`
Expected: FAIL — response has no `provenance` key.

- [ ] **Step 3: Implement**

In `patient_forms.py`:
1. Add `JudgeView` / `ProvenanceView` models (exact code above) next to `DisputeView`; extend `FieldView` with `provenance: ProvenanceView | None = None`.
2. Import: `from vera_core.services.call_provenance import FieldProvenance, load_call_attempts, load_field_provenance`.
3. Add the converter next to the view models:

```python
def _provenance_view(p: FieldProvenance | None) -> ProvenanceView | None:
    if p is None:
        return None
    judge = (
        JudgeView(confidence=p.judge.confidence, supported=p.judge.supported, evidence=p.judge.evidence)
        if p.judge is not None
        else None
    )
    return ProvenanceView(attempt=p.attempt, mode=p.mode, judge=judge)
```

4. In `_build_detail`, replace `fields=[FieldView(**view) for view in views]` with:

```python
    attempts = await load_call_attempts(session, form.id)
    prov = await load_field_provenance(
        session, form.id, {a.id: (a.attempt, a.mode) for a in attempts}
    )
    ...
        fields=[
            FieldView(**view, provenance=_provenance_view(prov.get(view["field_path"])))
            for view in views
        ],
```

- [ ] **Step 4: Run tests**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_form_provenance.py tests/integration/control_plane/test_patient_forms_review.py -v`
Expected: PASS (new + existing detail tests — `provenance` defaults keep old assertions valid).

- [ ] **Step 5: Lint/type + commit**

Run: `cd vera-backend && uv run ruff check apps/control_plane/src/control_plane/api/v1/patient_forms.py && uv run mypy apps/control_plane/src/control_plane/api/v1/patient_forms.py`

```bash
git add -A && git commit -m "feat(forms): per-field provenance (attempt, mode, judge verdict) on the form detail"
```

---

## Task 4: `GET /patient-forms/{form_id}/calls` — attempt timeline

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/patient_forms.py`
- Test: `tests/integration/control_plane/test_form_provenance.py` (add case)

**Interfaces:**
- Consumes: `load_call_attempts` (Task 2).
- Produces (frontend Task 10 consumes):

```python
class CallAttemptView(BaseModel):
    id: UUID
    attempt: int
    mode: str
    status: str
    created_at: datetime
    retry_of: UUID | None
    changed_paths: list[str]
```

- [ ] **Step 1: Write the failing test** (same file/fixture as Task 3)

```python
@pytest.mark.asyncio
async def test_calls_timeline(client, rbac_world, provenance_form_id) -> None:
    resp = await client.get(
        f"/api/v1/patient-forms/{provenance_form_id}/calls",
        headers={"Authorization": f"Bearer {rbac_world.admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    calls = resp.json()["data"]
    assert [c["attempt"] for c in calls] == [1, 2]
    assert calls[1]["mode"] == "retry"
    assert calls[1]["retry_of"] == calls[0]["id"]
    assert calls[1]["changed_paths"] == ["cov.b"]
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_calls_timeline_unknown_form_404(client, rbac_world) -> None:
    from vera_core.db import uuid7

    resp = await client.get(
        f"/api/v1/patient-forms/{uuid7()}/calls",
        headers={"Authorization": f"Bearer {rbac_world.admin_token}"},
    )
    assert resp.status_code == 404
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_form_provenance.py -k timeline -v`
Expected: FAIL 404/405 (route missing).

- [ ] **Step 3: Implement** (in `patient_forms.py`, after `get_patient_form`)

```python
@router.get(
    "/patient-forms/{form_id}/calls",
    response_model=ResponseModel[list[CallAttemptView]],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def list_form_calls(
    form_id: UUID,
    request: Request,
    response: Response,
    session: TenantSession,
    tenant_id: TenantId,
    caller: VerifiedIdentity = require("forms:read"),
) -> ResponseModel[list[CallAttemptView]]:
    """The form's call-attempt timeline: mode, status, lineage, and which field
    paths each call changed. Paths and timings only — no field values."""
    response.headers["Cache-Control"] = "no-store"
    form = (
        await session.execute(select(PatientForm).where(PatientForm.id == form_id))
    ).scalar_one_or_none()
    if form is None:
        raise NotFoundError(message="patient form not found")
    attempts = await load_call_attempts(session, form_id)
    await get_audit(request).emit(
        _audit_phi_read(
            request, tenant_id, caller, str(form_id),
            sorted({p for a in attempts for p in a.changed_paths}),
        )
    )
    return ok(
        [
            CallAttemptView(
                id=a.id, attempt=a.attempt, mode=a.mode, status=a.status,
                created_at=a.created_at, retry_of=a.retry_of, changed_paths=a.changed_paths,
            )
            for a in attempts
        ]
    )
```

Place `CallAttemptView` next to the other response models; `datetime` is already imported in the module.

- [ ] **Step 4: Run tests**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_form_provenance.py -v`
Expected: PASS.

- [ ] **Step 5: Lint/type + commit**

```bash
cd vera-backend && uv run ruff check . && uv run mypy apps/control_plane/src/control_plane/api/v1/patient_forms.py
git add -A && git commit -m "feat(forms): GET /patient-forms/{id}/calls — attempt timeline with lineage + changed paths"
```

---

## Task 5: `forms:export` permission + `export_artifact` model/migrations + `FORM_EXPORTED` audit event

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/rbac_defaults.py`
- Modify: `packages/vera_core/src/vera_core/models/audit_log.py`
- Create: `packages/vera_core/src/vera_core/models/export_artifact.py`
- Modify: `packages/vera_core/src/vera_core/models/__init__.py` (register the model — mirror how `CallLineage` is exported)
- Create: 2 migrations (permission seed; export_artifact table + RLS)
- Test: `tests/unit/db/test_export_artifact_model.py` (create)

**Interfaces:**
- Produces: `ExportArtifact(tenant_id, form_id, format, sha256, gcs_uri=None, exported_by)`; `AuditEvent.FORM_EXPORTED = "form.exported"`; permission code `forms:export` on TENANT_ADMIN + SUPERVISOR.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/db/test_export_artifact_model.py
from vera_core.models import ExportArtifact
from vera_core.models.audit_log import AuditEvent
from vera_core.models.rbac_defaults import ALL_PERMISSIONS, SYSTEM_ROLES


def test_export_artifact_table_shape() -> None:
    cols = {c.name for c in ExportArtifact.__table__.columns}
    assert {"id", "tenant_id", "form_id", "format", "sha256", "gcs_uri", "exported_by", "created_at"} <= cols


def test_forms_export_permission_seeded_to_admin_and_supervisor() -> None:
    assert "forms:export" in ALL_PERMISSIONS
    assert "forms:export" in SYSTEM_ROLES["TENANT_ADMIN"]
    assert "forms:export" in SYSTEM_ROLES["SUPERVISOR"]


def test_form_exported_audit_event() -> None:
    assert AuditEvent.FORM_EXPORTED.value == "form.exported"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd vera-backend && uv run pytest tests/unit/db/test_export_artifact_model.py -v`
Expected: FAIL with ImportError.

- [ ] **Step 3: Implement**

`export_artifact.py`:

```python
"""Export disclosure ledger — one row per file that left the perimeter.

No file is stored: sha256 identifies the exact bytes streamed to the caller;
the paired FORM_EXPORTED audit record carries who/when/what-fields. `gcs_uri`
is reserved for a future stored-artifact variant and stays NULL today.
"""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from vera_core.db.base import Base, CreatedAtMixin, TenantColumnMixin, UUIDv7PKMixin


class ExportArtifact(Base, UUIDv7PKMixin, CreatedAtMixin, TenantColumnMixin):
    __tablename__ = "export_artifact"
    __table_args__ = (CheckConstraint("format IN ('xlsx')", name="format_valid"),)

    form_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patient_form.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    format: Mapped[str] = mapped_column(String(8), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    gcs_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    exported_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
```

`rbac_defaults.py`: add to `ALL_PERMISSIONS` (keep dict ordering with the other `forms:` entries):

```python
    "forms:export": "Export a completed form as a file (PHI disclosure; every export is audited)",
```

and add `"forms:export"` to the `TENANT_ADMIN` and `SUPERVISOR` frozensets in `SYSTEM_ROLES`.

`audit_log.py`: add to `AuditEvent`:

```python
    # A user exported a completed form as a file — PHI left the perimeter.
    # Detail carries artifact id, format, and field NAMES only, never values.
    FORM_EXPORTED = "form.exported"
```

**Migration A (table + RLS):** `just makemigration` (message: `export_artifact table`), replace the body:

```python
import vera_core.models  # noqa: F401 — registers export_artifact on Base.metadata
from vera_core.db import Base
from vera_core.db.rls import rls_policy_ddl


def upgrade() -> None:
    bind = op.get_bind()
    # Fresh DBs get the table (and generic RLS) from 0001's create_all; this
    # covers already-provisioned DBs. checkfirst + guarded policies = idempotent.
    Base.metadata.tables["export_artifact"].create(bind, checkfirst=True)
    for stmt in rls_policy_ddl("export_artifact"):
        if stmt.lstrip().upper().startswith("CREATE POLICY"):
            op.execute(
                f"DO $$ BEGIN {stmt}; EXCEPTION WHEN duplicate_object THEN NULL; END $$"
            )
        else:  # ALTER TABLE ENABLE/FORCE RLS — natively idempotent
            op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS export_artifact")
```

(Check `rls_policy_ddl`'s signature in `vera_core/db/rls.py` — 0001 calls it as `rls_policy_ddl(table_name)` for standard tenant tables; if the policy-name/statement mix differs from the CREATE POLICY prefix assumption above, adjust the guard to match the actual statements.)

**Migration B (permission seed):** `just makemigration` (message: `seed forms export permission`), replace the body (modeled on `20260706_1730_25e54e43fcf3`):

```python
_PERMISSION_CODE = "forms:export"
_DESCRIPTION = "Export a completed form as a file (PHI disclosure; every export is audited)"
_ROLE_NAMES = ("TENANT_ADMIN", "SUPERVISOR")


def upgrade() -> None:
    op.execute(
        "INSERT INTO permission (id, code, description) "
        f"VALUES (gen_random_uuid(), '{_PERMISSION_CODE}', '{_DESCRIPTION}') "
        "ON CONFLICT (code) DO NOTHING"
    )
    for role_name in _ROLE_NAMES:
        op.execute(
            "INSERT INTO role_permission (id, tenant_id, role_id, permission_id) "
            "SELECT gen_random_uuid(), NULL, r.id, p.id FROM role r, permission p "
            f"WHERE r.tenant_id IS NULL AND r.name = '{role_name}' "
            f"AND p.code = '{_PERMISSION_CODE}' "
            "ON CONFLICT (role_id, permission_id) DO NOTHING"
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permission WHERE permission_id IN "
        f"(SELECT id FROM permission WHERE code = '{_PERMISSION_CODE}')"
    )
    op.execute(f"DELETE FROM permission WHERE code = '{_PERMISSION_CODE}'")
```

Ensure Migration B's `down_revision` chains after Migration A (linear chain; run `uv run alembic heads` — exactly one head).

- [ ] **Step 4: Run tests**

Run: `cd vera-backend && uv run pytest tests/unit/db/ -v`
Expected: PASS (new + the existing enum-enumerating tests in `tests/unit/db/test_authoring.py` are insurance-type-scoped and unaffected; if any RBAC test enumerates `ALL_PERMISSIONS`, update its expected set and note it).

- [ ] **Step 5: Lint/type + commit**

```bash
cd vera-backend && uv run ruff check . && uv run mypy packages/vera_core/src/vera_core
git add -A && git commit -m "feat(export): export_artifact ledger, forms:export permission (admin+supervisor), FORM_EXPORTED audit event"
```

---

## Task 6: `build_workbook` — pure XLSX mapping layer

**Files:**
- Modify: `packages/vera_core/pyproject.toml` (add `openpyxl`)
- Create: `packages/vera_core/src/vera_core/forms/export.py`
- Test: `tests/unit/forms/test_export_workbook.py` (create)

**Interfaces:**
- Consumes: `FormSchemaDoc`, `is_v2`, `leaf_gates`, `is_applicable` (forms.conditions/dsl); `field_labels` (forms.review); `CallAttempt`, `FieldProvenance` (Task 2).
- Produces: `def build_workbook(schema_json, values, sources, provenance, attempts) -> bytes` — Task 7 calls it.

- [ ] **Step 1: Add the dependency**

In `packages/vera_core/pyproject.toml` `[project] dependencies`, add `"openpyxl>=3.1"`. Run `cd vera-backend && uv sync`. If `uv run mypy packages/vera_core` later fails on missing openpyxl stubs, add `"types-openpyxl"` to the dev dependency group in the root `pyproject.toml` (match how other type-stub deps are declared there) and `uv sync` again.

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/forms/test_export_workbook.py
from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

from openpyxl import load_workbook

from vera_core.forms.export import build_workbook
from vera_core.services.call_provenance import CallAttempt, FieldProvenance, JudgeInfo

V2 = {
    "dsl_version": "2.1",
    "name": "Test",
    "insurance_type": "infertility_treatment",
    "sections": {
        "cov": {
            "title": "Coverage",
            "role": "collect",
            "fields": {
                "network_status": {
                    "type": "text", "title": "Network status", "role": "ask",
                    "required": True, "prompt": {"ask": "What is the network status?"},
                },
            },
        },
    },
    "tasks": [{"task_key": "t1", "title": "Task 1", "sections": ["cov"]}],
}


def _attempt(n: int, mode: str) -> CallAttempt:
    return CallAttempt(
        id=uuid4(), attempt=n, mode=mode, status="completed",
        created_at=datetime(2026, 7, 10, tzinfo=UTC), retry_of=None, changed_paths=[],
    )


def test_workbook_has_form_and_provenance_sheets() -> None:
    path = "sections.cov.network_status"
    data = build_workbook(
        V2,
        values={path: "in-network"},
        sources={path: "ai_call"},
        provenance={path: FieldProvenance(attempt=1, mode="full", judge=JudgeInfo(90, True, "e"))},
        attempts=[_attempt(1, "full")],
    )
    wb = load_workbook(BytesIO(data))
    assert wb.sheetnames == ["Form", "Provenance"]
    form_cells = [tuple(r) for r in wb["Form"].iter_rows(values_only=True)]
    assert ("Coverage", None) in form_cells or ("Coverage",) in form_cells
    assert ("Network status", "in-network") in form_cells
    prov_rows = [tuple(r) for r in wb["Provenance"].iter_rows(values_only=True)]
    assert any(r[0] == path and r[2] == "ai_call" and r[3] == 1 for r in prov_rows if r[0])
    assert any(r and r[0] == "Call history" for r in prov_rows)


def test_v1_falls_back_to_flat_listing() -> None:
    data = build_workbook(
        {"sections": []}, values={"cov.a": "x"}, sources={"cov.a": "human"},
        provenance={}, attempts=[],
    )
    wb = load_workbook(BytesIO(data))
    rows = [tuple(r) for r in wb["Form"].iter_rows(values_only=True)]
    assert ("cov.a", "x") in rows
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd vera-backend && uv run pytest tests/unit/forms/test_export_workbook.py -v`
Expected: FAIL with ModuleNotFoundError (`vera_core.forms.export`).

- [ ] **Step 4: Implement**

```python
# packages/vera_core/src/vera_core/forms/export.py
"""Pure XLSX mapping layer for the form export — DB-free, format-agnostic inputs.

The workbook IS PHI (it carries field values): callers stream it inside an
authed, audited, no-store response and never log its contents. A future PDF
renderer consumes the same arguments.
"""

from collections.abc import Mapping, Sequence
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font

from vera_core.forms.conditions import is_applicable, is_v2, leaf_gates
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.review import field_labels
from vera_core.services.call_provenance import CallAttempt, FieldProvenance

_BOLD = Font(bold=True)


def build_workbook(
    schema_json: Mapping[str, Any],
    values: Mapping[str, Any],
    sources: Mapping[str, str],
    provenance: Mapping[str, FieldProvenance],
    attempts: Sequence[CallAttempt],
) -> bytes:
    wb = Workbook()
    form_ws = wb.active
    form_ws.title = "Form"

    if is_v2(schema_json):
        doc = FormSchemaDoc.model_validate(schema_json)
        shared = doc.shared_conditions or {}
        current_section: str | None = None
        for path, leaf, gates in leaf_gates(doc):
            if not is_applicable(gates, values, shared):
                continue
            # v2 paths are root-anchored: sections.<key>.<...>
            section_key = path.split(".")[1]
            if section_key != current_section:
                current_section = section_key
                form_ws.append([])
                form_ws.append([doc.sections[section_key].title])
                form_ws.cell(row=form_ws.max_row, column=1).font = _BOLD
            value = values.get(path)
            if value is None and leaf.default is not None:
                value = leaf.default  # DSL §4.4: defaults count as filled on export
            form_ws.append([leaf.title, "" if value is None else str(value)])
    else:
        for path in sorted(values):
            form_ws.append([path, "" if values[path] is None else str(values[path])])

    prov_ws = wb.create_sheet("Provenance")
    prov_ws.append(
        ["Field path", "Label", "Source", "Attempt", "Mode", "Judge confidence", "Supported"]
    )
    prov_ws.cell(row=1, column=1).font = _BOLD
    paths = sorted(values)
    labels = dict(zip(paths, field_labels(schema_json, paths), strict=True))
    for path in paths:
        p = provenance.get(path)
        prov_ws.append(
            [
                path,
                labels.get(path, path),
                sources.get(path, ""),
                p.attempt if p else None,
                p.mode if p else None,
                p.judge.confidence if p and p.judge else None,
                p.judge.supported if p and p.judge else None,
            ]
        )

    attempt_by_id = {a.id: a.attempt for a in attempts}
    prov_ws.append([])
    prov_ws.append(["Call history"])
    prov_ws.cell(row=prov_ws.max_row, column=1).font = _BOLD
    prov_ws.append(["Attempt", "Mode", "Status", "Created at", "Retry of attempt"])
    for a in attempts:
        prov_ws.append(
            [
                a.attempt,
                a.mode,
                a.status,
                a.created_at.isoformat(),
                attempt_by_id.get(a.retry_of) if a.retry_of else None,
            ]
        )

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
```

(If mypy flags `wb.active` as `Worksheet | None`, bind it via `form_ws = wb.active; assert form_ws is not None` → no: `assert` is banned under `-O` discipline for runtime guards, but this is a type-narrowing of a library invariant in pure code — use `cast(Worksheet, wb.active)` with `from openpyxl.worksheet.worksheet import Worksheet` instead.)

- [ ] **Step 5: Run tests**

Run: `cd vera-backend && uv run pytest tests/unit/forms/test_export_workbook.py -v`
Expected: PASS.

- [ ] **Step 6: Lint/type + commit**

```bash
cd vera-backend && uv run ruff check packages/vera_core/src/vera_core/forms/export.py && uv run mypy packages/vera_core/src/vera_core/forms/export.py
git add -A && git commit -m "feat(export): pure openpyxl workbook builder — Form + Provenance sheets"
```

---

## Task 7: `POST /patient-forms/{form_id}/export` endpoint

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/patient_forms.py`
- Test: `tests/integration/control_plane/test_form_export.py` (create)

**Interfaces:**
- Consumes: `build_workbook` (Task 6), `ExportArtifact` + `AuditEvent.FORM_EXPORTED` (Task 5), `load_call_attempts`/`load_field_provenance` (Task 2), `load_current_values`/`load_field_status` (existing, `vera_core.services.field_status`).
- Produces: binary XLSX response; one `export_artifact` row; one FORM_EXPORTED audit per successful export.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/control_plane/test_form_export.py
from io import BytesIO

import pytest
from openpyxl import load_workbook
from sqlalchemy import text

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_export_streams_xlsx_and_writes_ledger(
    client, rbac_world, completed_form_id, admin_sessionmaker
) -> None:
    """completed_form_id: a COMPLETED form under rbac_world.tenant_id with at
    least one current answer (reuse/extend the Task 3 fixture, status='completed')."""
    resp = await client.post(
        f"/api/v1/patient-forms/{completed_form_id}/export",
        headers={"Authorization": f"Bearer {rbac_world.admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert f"ibv-{completed_form_id}.xlsx" in resp.headers["content-disposition"]
    assert resp.headers["cache-control"] == "no-store"
    wb = load_workbook(BytesIO(resp.content))
    assert wb.sheetnames == ["Form", "Provenance"]

    async with admin_sessionmaker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT format, sha256, gcs_uri FROM export_artifact WHERE form_id = :fid"
                ).bindparams(fid=completed_form_id)
            )
        ).one()
    assert row.format == "xlsx" and len(row.sha256) == 64 and row.gcs_uri is None


@pytest.mark.asyncio
async def test_export_rejects_non_completed(client, rbac_world, provenance_form_id) -> None:
    # provenance_form_id (Task 3 fixture) is NOT completed.
    resp = await client.post(
        f"/api/v1/patient-forms/{provenance_form_id}/export",
        headers={"Authorization": f"Bearer {rbac_world.admin_token}"},
    )
    assert resp.status_code in (400, 422)  # the VALIDATION_ERROR envelope status


@pytest.mark.asyncio
async def test_export_requires_permission(client, rbac_world, completed_form_id) -> None:
    resp = await client.post(
        f"/api/v1/patient-forms/{completed_form_id}/export",
        headers={"Authorization": f"Bearer {rbac_world.norole_token}"},
    )
    assert resp.status_code == 403
```

Note: `rbac_world` seeds roles via `scripts/seed._seed_permissions/_seed_system_roles`, which read `rbac_defaults` — Task 5's change means the admin already holds `forms:export` here. Add a `completed_form_id` fixture (same seeding skeleton, `status="completed"`, ≥1 current answer + one call with snapshot). FK teardown must also delete `export_artifact` rows for the form (before `patient_form`).

- [ ] **Step 2: Run to verify it fails**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_form_export.py -v`
Expected: FAIL — 404/405 (route missing).

- [ ] **Step 3: Implement** (in `patient_forms.py`)

Imports to add: `import hashlib`; `from vera_core.forms.export import build_workbook`; `from vera_core.models import ExportArtifact`; `from vera_core.services.field_status import load_current_values, load_field_status` (extend the existing field_status import).

```python
_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.post(
    "/patient-forms/{form_id}/export",
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.VALIDATION_ERROR,
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
    ),
)
async def export_patient_form(
    form_id: UUID,
    request: Request,
    session: TenantSession,
    tenant_id: TenantId,
    caller: VerifiedIdentity = require("forms:export"),
) -> Response:
    """Stream the COMPLETED form as XLSX — a PHI disclosure. Writes one
    export_artifact ledger row + a FORM_EXPORTED audit (field names only).
    The one binary endpoint: errors still ride the standard envelope."""
    form = (
        await session.execute(select(PatientForm).where(PatientForm.id == form_id))
    ).scalar_one_or_none()
    if form is None:
        raise NotFoundError(message="patient form not found")
    if form.status != FormStatus.COMPLETED.value:
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR,
            message="only completed forms can be exported",
            data={"status": form.status},
        )
    version = (
        await session.execute(
            select(SchemaVersion).where(SchemaVersion.id == form.schema_version_id)
        )
    ).scalar_one()
    values = await load_current_values(session, form_id)
    sources = {p: s.source or "" for p, s in (await load_field_status(session, form_id)).items()}
    attempts = await load_call_attempts(session, form_id)
    prov = await load_field_provenance(
        session, form_id, {a.id: (a.attempt, a.mode) for a in attempts}
    )
    data = build_workbook(version.schema_json, values, sources, prov, attempts)

    artifact = ExportArtifact(
        tenant_id=tenant_id,
        form_id=form_id,
        format="xlsx",
        sha256=hashlib.sha256(data).hexdigest(),
        exported_by=caller.user_id,
    )
    session.add(artifact)
    await session.flush()
    await get_audit(request).emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=caller.user_id,
            actor_label=caller.email or caller.subject,
            event_type=AuditEvent.FORM_EXPORTED.value,
            resource_type="patient_form",
            resource_id=str(form_id),
            detail={"artifact_id": str(artifact.id), "format": "xlsx", "fields": sorted(values)},
        )
    )
    return Response(
        content=data,
        media_type=_XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="ibv-{form_id}.xlsx"',
            "Cache-Control": "no-store",
        },
    )
```

- [ ] **Step 4: Run tests**

Run: `cd vera-backend && uv run pytest tests/integration/control_plane/test_form_export.py -v`
Expected: PASS.

- [ ] **Step 5: Lint/type + commit**

```bash
cd vera-backend && uv run ruff check . && uv run mypy apps/control_plane/src/control_plane/api/v1/patient_forms.py
git add -A && git commit -m "feat(export): POST /patient-forms/{id}/export — streamed XLSX, ledger row, disclosure audit"
```

---

## Task 8: Frontend API layer — types, blob client, api fns, `formId`/provenance in IbvProvider

**Files:**
- Modify: `vera-frontend/src/lib/patient-forms/types.ts`
- Modify: `vera-frontend/src/lib/api/client.ts`
- Modify: `vera-frontend/src/lib/patient-forms/api.ts`
- Modify: `vera-frontend/src/lib/patient-forms/display.ts`
- Modify: `vera-frontend/src/components/ibv/IbvProvider.tsx`
- Test: `vera-frontend/src/lib/patient-forms/display.test.ts` (create or extend if one exists)

**Interfaces (produced; Tasks 9–12 consume):**

```typescript
// types.ts
export type FieldJudge = { confidence: number | null; supported: boolean; evidence: string | null }
export type FieldProvenance = { attempt: number; mode: "full" | "retry"; judge: FieldJudge | null }
// PatientFormField gains: provenance: FieldProvenance | null
// PatientFormSummary gains: review_reason: string | null
export type CallAttempt = {
  id: string; attempt: number; mode: "full" | "retry"; status: string
  created_at: string; retry_of: string | null; changed_paths: string[]
}
// api.ts
export function getPatientFormCalls(formId: string): Promise<CallAttempt[]>
export function exportPatientForm(formId: string): Promise<Blob>
// client.ts
export function apiRequestBlob(path: string, opts?: RequestOptions): Promise<Blob>
// display.ts
export function ageLabel(iso: string | null): string   // "3d", "5h", "12m", "—"
// IbvProvider context additions
formId: string | null
provenanceFor: (path: string) => FieldProvenance | null
```

- [ ] **Step 1: Write the failing test**

```typescript
// vera-frontend/src/lib/patient-forms/display.test.ts (create; merge in if the file exists)
import { describe, expect, it } from "vitest"
import { ageLabel } from "./display"

describe("ageLabel", () => {
  it("renders day/hour/minute buckets", () => {
    const now = Date.now()
    expect(ageLabel(new Date(now - 3 * 864e5).toISOString())).toBe("3d")
    expect(ageLabel(new Date(now - 5 * 36e5).toISOString())).toBe("5h")
    expect(ageLabel(new Date(now - 12 * 6e4).toISOString())).toBe("12m")
  })
  it("dashes on null/invalid", () => {
    expect(ageLabel(null)).toBe("—")
    expect(ageLabel("not-a-date")).toBe("—")
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd vera-frontend && npm test`
Expected: FAIL — `ageLabel` is not exported.

- [ ] **Step 3: Implement**

`display.ts` — append:

```typescript
/** Rough relative age for worklist columns: "3d" / "5h" / "12m", or "—". */
export function ageLabel(iso: string | null): string {
  if (!iso) return "—"
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return "—"
  const mins = Math.max(0, Math.floor((Date.now() - t) / 60_000))
  if (mins >= 1440) return `${Math.floor(mins / 1440)}d`
  if (mins >= 60) return `${Math.floor(mins / 60)}h`
  return `${mins}m`
}
```

`types.ts` — add `FieldJudge`, `FieldProvenance`, `CallAttempt` (exact shapes above); add `provenance: FieldProvenance | null` to `PatientFormField`; add `review_reason: string | null` to `PatientFormSummary`.

`client.ts` — add below `apiRequest` (mirror its existing 401/authFailureHandler behavior — read the rest of `apiRequest` first and reuse the same failure branch):

```typescript
/** apiRequest for binary downloads: returns the raw Blob. Failures still arrive
 *  as the JSON envelope, so parse it for the error message when present. */
export async function apiRequestBlob(path: string, opts: RequestOptions = {}): Promise<Blob> {
  const { method = "GET", body, auth = true, headers: extraHeaders } = opts
  const headers: Record<string, string> = { ...extraHeaders }
  if (body !== undefined) headers["Content-Type"] = "application/json"
  if (auth) {
    const token = getToken()
    if (token) headers.Authorization = `Bearer ${token}`
  }
  let res: Response
  try {
    res = await fetch(`${BASE_URL}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "Could not reach the server. Is the API running?")
  }
  if (!res.ok) {
    let envelope: Envelope<unknown> | null = null
    try {
      envelope = (await res.json()) as Envelope<unknown>
    } catch {
      /* non-JSON error body */
    }
    // Reuse apiRequest's 401 handling so an expired session clears auth state.
    if (res.status === 401) authFailureHandler?.()
    throw new ApiError(
      res.status,
      envelope?.error_code ?? "HTTP_ERROR",
      envelope?.message ?? `Request failed (${res.status})`,
    )
  }
  return res.blob()
}
```

`api.ts` — add:

```typescript
/** GET /patient-forms/{id}/calls — the attempt timeline. */
export function getPatientFormCalls(formId: string): Promise<CallAttempt[]> {
  return apiRequest<CallAttempt[]>(
    `/patient-forms/${encodeURIComponent(formId)}/calls`,
  )
}

/** POST /patient-forms/{id}/export — streamed XLSX (forms:export). */
export function exportPatientForm(formId: string): Promise<Blob> {
  return apiRequestBlob(`/patient-forms/${encodeURIComponent(formId)}/export`, {
    method: "POST",
  })
}
```

(import `apiRequestBlob` from `@/lib/api/client` and `CallAttempt` from `./types`).

`IbvProvider.tsx` —
1. Add `formId: string | null` and `provenanceFor: (path: string) => FieldProvenance | null` to `IbvContextValue`, and include both in the provided value (`formId` state already exists at ~line 127).
2. Add provenance state next to the disputes state, filled wherever the fetched/refreshed `PatientFormDetail` is adapted (both the initial `openFormById` load and the post-save refresh):

```typescript
const [provenance, setProvenance] = useState<Record<string, FieldProvenance>>({})
// wherever detail is adapted into values/disputes:
const prov: Record<string, FieldProvenance> = {}
for (const f of detail.fields) if (f.provenance) prov[f.field_path] = f.provenance
setProvenance(prov)
// accessor in the context value:
const provenanceFor = useCallback((path: string) => provenance[path] ?? null, [provenance])
```

- [ ] **Step 4: Run the gates**

Run: `cd vera-frontend && npm test && npm run lint && npm run build`
Expected: PASS / clean build.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(fe): provenance/calls/export API layer + formId & provenance in IbvProvider"
```

---

## Task 9: Provenance tooltip on `FieldRow`

**Files:**
- Modify: `vera-frontend/src/components/ibv/FieldRow.tsx`

**Interfaces:**
- Consumes: `provenanceFor` (Task 8), the Tooltip components already used by `DisputeControls` (`@/components/ui/tooltip`), `Info` from `lucide-react`.

- [ ] **Step 1: Implement**

In `FieldRow`, pull `provenanceFor` from `useIbv()`, compute `const prov = provenanceFor(path)`, and render an info affordance in the label cell after the required-`*` span (AI-sourced fields only — `prov` is non-null only for them). Mirror the Tooltip usage pattern in `DisputeControls.tsx` (same imports and structure):

```tsx
{prov && (
  <Tooltip>
    <TooltipTrigger asChild>
      <button
        type="button"
        aria-label="Field provenance"
        className="shrink-0 text-muted-foreground hover:text-foreground"
      >
        <Info className="h-3 w-3" />
      </button>
    </TooltipTrigger>
    <TooltipContent className="max-w-[280px]">
      <p className="font-medium">
        Attempt {prov.attempt} ({prov.mode})
        {prov.judge &&
          ` · judge ${prov.judge.confidence ?? "—"}, ${prov.judge.supported ? "supported" : "unsupported"}`}
      </p>
      {prov.judge?.evidence && (
        <p className="mt-1 text-xs text-muted-foreground">“{prov.judge.evidence}”</p>
      )}
    </TooltipContent>
  </Tooltip>
)}
```

(If `DisputeControls.tsx` wraps tooltips in a `TooltipProvider`, match that; the evidence text is de-identified tokens — rendering it is the same disclosure the dispute tooltip already makes.)

- [ ] **Step 2: Run the gates**

Run: `cd vera-frontend && npm run lint && npm run build && npm test`
Expected: clean.

- [ ] **Step 3: Verify visually (optional but recommended)**

Boot backend (`just api`) + frontend (`npm run dev`), open a form with AI answers, hover the info icon.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat(fe): per-field provenance tooltip — attempt, mode, judge verdict, evidence"
```

---

## Task 10: Call history tab in the review modal

**Files:**
- Create: `vera-frontend/src/components/ibv/CallHistoryTab.tsx`
- Modify: `vera-frontend/src/components/ibv/IbvFormModal.tsx`

**Interfaces:**
- Consumes: `getPatientFormCalls`, `CallAttempt` (Task 8), `formId` from `useIbv()`, `fieldLabel`/`formatDate`/`statusLabel` from display.ts.

- [ ] **Step 1: Create the tab component**

```tsx
// vera-frontend/src/components/ibv/CallHistoryTab.tsx
import { useEffect, useState } from "react"

import { cn } from "@/lib/utils"
import { ApiError } from "@/lib/api/client"
import { getPatientFormCalls } from "@/lib/patient-forms/api"
import type { CallAttempt } from "@/lib/patient-forms/types"
import { fieldLabel, formatDate, statusLabel } from "@/lib/patient-forms/display"
import { useIbv } from "./IbvProvider"

/** The form's call-attempt timeline — fetched once per modal open. */
export function CallHistoryTab() {
  const { formId } = useIbv()
  const [attempts, setAttempts] = useState<CallAttempt[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  useEffect(() => {
    if (!formId) return
    let cancelled = false
    getPatientFormCalls(formId)
      .then((res) => {
        if (!cancelled) setAttempts(res)
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof ApiError ? err.message : "Could not load call history.")
      })
    return () => {
      cancelled = true
    }
  }, [formId])

  if (error)
    return (
      <p className="text-sm text-destructive" role="alert">
        {error}
      </p>
    )
  if (attempts === null) return <p className="text-sm text-muted-foreground">Loading…</p>
  if (attempts.length === 0)
    return <p className="text-sm text-muted-foreground">No calls have been made for this form.</p>

  const attemptOf = new Map(attempts.map((a) => [a.id, a.attempt]))
  return (
    <div className="flex flex-col gap-3">
      {attempts.map((a) => (
        <div key={a.id} className="rounded-md border border-border bg-white p-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-semibold">Attempt {a.attempt}</span>
            <span
              className={cn(
                "rounded-full px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide",
                a.mode === "retry" ? "bg-purple-100 text-purple-700" : "bg-slate-100 text-slate-600",
              )}
            >
              {a.mode}
            </span>
            <span className="text-muted-foreground">{statusLabel(a.status)}</span>
            <span className="text-muted-foreground">·</span>
            <span className="text-muted-foreground">{formatDate(a.created_at)}</span>
            {a.retry_of && attemptOf.has(a.retry_of) && (
              <span className="text-xs text-muted-foreground">
                retry of attempt {attemptOf.get(a.retry_of)}
              </span>
            )}
          </div>
          <button
            type="button"
            className="mt-1 text-xs text-muted-foreground underline-offset-2 hover:underline disabled:no-underline"
            disabled={a.changed_paths.length === 0}
            onClick={() => setExpanded((e) => ({ ...e, [a.id]: !e[a.id] }))}
          >
            {a.changed_paths.length} field{a.changed_paths.length === 1 ? "" : "s"} updated
          </button>
          {expanded[a.id] && a.changed_paths.length > 0 && (
            <ul className="mt-1 list-inside list-disc text-xs text-muted-foreground">
              {a.changed_paths.map((p) => (
                <li key={p}>{fieldLabel(p)}</li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </div>
  )
}
```

- [ ] **Step 2: Add the tab bar to the modal**

In `IbvFormModal.tsx`: add `const [tab, setTab] = useState<"form" | "calls">("form")` (reset to `"form"` when `modalOpen` flips true — add a `useEffect` on `modalOpen`). Insert a tab bar between the status bar and the body, and switch the body content:

```tsx
<div className="flex gap-1 border-b border-border px-4 pt-2">
  {(["form", "calls"] as const).map((t) => (
    <button
      key={t}
      type="button"
      onClick={() => setTab(t)}
      className={cn(
        "rounded-t-md px-3 py-1.5 text-sm font-medium",
        tab === t
          ? "border border-b-0 border-border bg-background"
          : "text-muted-foreground hover:text-foreground",
      )}
    >
      {t === "form" ? "Form" : "Call history"}
    </button>
  ))}
</div>
```

and in the body div: `{!loading && !error && (tab === "form" ? <SchemaForm /> : <CallHistoryTab />)}`. Import `CallHistoryTab` and `useState`/`useEffect`.

- [ ] **Step 3: Run the gates**

Run: `cd vera-frontend && npm run lint && npm run build && npm test`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat(fe): Call history tab — attempt timeline with lineage + changed fields"
```

---

## Task 11: Needs Review worklist tab

**Files:**
- Modify: `vera-frontend/src/pages/DataManagement.tsx`

**Interfaces:**
- Consumes: `review_reason` on `PatientFormSummary`, `ageLabel` + `statusLabel` from display.ts (Task 8).

- [ ] **Step 1: Implement**

1. Extend the tab model:

```typescript
type TabKey = "all" | "needs_review" | "completed"
const TABS: { key: TabKey; label: string }[] = [
  { key: "all", label: "All Data" },
  { key: "needs_review", label: "Needs Review" },
  { key: "completed", label: "Completed" },
]
```

2. Extend the forced-status logic:

```typescript
const effectiveStatus =
  tab === "completed" ? "completed" : tab === "needs_review" ? "exception_review" : status || undefined
```

(hide the free status `Select` while `tab !== "all"` if it isn't already).

3. When `tab === "needs_review"`, render two extra (unsortable) header cells after the existing sortable columns — `Reason` and `Age` — and the matching row cells:

```tsx
{tab === "needs_review" && (
  <>
    <TableCell>
      {row.review_reason ? (
        <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-semibold text-amber-700">
          {statusLabel(row.review_reason)}
        </span>
      ) : (
        "—"
      )}
    </TableCell>
    <TableCell>{ageLabel(row.updated_at)}</TableCell>
  </>
)}
```

(`statusLabel` humanizes `retries_exhausted` → "Retries Exhausted". Import `ageLabel`.)

4. Reset `page` to 1 on tab change if the existing tab handler doesn't already.

- [ ] **Step 2: Run the gates**

Run: `cd vera-frontend && npm run lint && npm run build && npm test`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "feat(fe): Needs Review worklist tab with reason + age columns"
```

---

## Task 12: Export button

**Files:**
- Modify: `vera-frontend/src/components/ibv/IbvFormModal.tsx`

**Interfaces:**
- Consumes: `exportPatientForm` (Task 8), `usePermission` (existing), `formId`/`status` from `useIbv()`.

- [ ] **Step 1: Implement**

In `IbvFormModal.tsx`: `const canExport = usePermission("forms:export")`; pull `formId` from `useIbv()`; add local state `const [exporting, setExporting] = useState(false)` and `const [exportError, setExportError] = useState<string | null>(null)`. In the status bar's right-hand button group (before the transition buttons), render:

```tsx
{canExport && status === "completed" && formId && (
  <Button
    size="sm"
    variant="outline"
    disabled={exporting}
    onClick={async () => {
      setExporting(true)
      setExportError(null)
      try {
        const blob = await exportPatientForm(formId)
        const url = URL.createObjectURL(blob)
        const a = document.createElement("a")
        a.href = url
        a.download = `ibv-${formId}.xlsx`
        a.click()
        URL.revokeObjectURL(url)
      } catch (err) {
        setExportError(err instanceof ApiError ? err.message : "Export failed.")
      } finally {
        setExporting(false)
      }
    }}
  >
    {exporting ? "Exporting…" : "Export XLSX"}
  </Button>
)}
```

Render `exportError` in the same style/slot as the existing `statusError` paragraph. Imports: `exportPatientForm`, `ApiError`, `useState`.

Note the transition-button group is currently gated on `canWrite && transitions.length > 0`; the export button must render for COMPLETED forms too (where `transitions` is empty) — structure the JSX so `canExport && status === "completed"` shows the button regardless of `transitions`.

- [ ] **Step 2: Run the gates**

Run: `cd vera-frontend && npm run lint && npm run build && npm test`
Expected: clean.

- [ ] **Step 3: End-to-end check**

Boot `just api` + `npm run dev`; complete a form (or flip one to `completed` in the DB), click **Export XLSX**, open the file; confirm the Form + Provenance sheets and that an `export_artifact` row + `form.exported` audit row exist.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat(fe): Export XLSX button on completed forms (forms:export gated)"
```

---

## Task 13: Full gates + simplify pass

- [ ] **Step 1: Backend gate**

Run: `cd vera-backend && just check`
Expected: green, modulo the two known pre-existing local mypy livekit errors and the `test_platform_mfa_enroll.py` local-DB-drift failures (both green on CI). `uv run alembic heads` → exactly one head.

- [ ] **Step 2: Frontend gate**

Run: `cd vera-frontend && npm run lint && npm test && npm run build`
Expected: clean.

- [ ] **Step 3: Simplify pass (repo-mandated)**

Run the `/simplify` skill on the branch diff, apply its fixes, re-run both gates, and commit the output.

- [ ] **Step 4: Push + PR**

```bash
git push -u origin feat/review-and-export
gh pr create --base dev --title "Review provenance + export (phases 3+4)" --body "..."
```

(Note: the branch is cut from `feat/retry-call`; if PR #75 hasn't merged yet, mark this PR as stacked on it.)

---

## Self-review

**Spec coverage:** §2.1 provenance → Tasks 2+3; §2.2 timeline → Tasks 2+4; §2.3 review_reason → Task 1; §3.1 popover → Task 9; §3.2 history tab → Task 10; §3.3 Needs Review tab → Task 11; §4.1 endpoint → Task 7; §4.2 mapping layer → Task 6; §4.3 ledger → Task 5; §4.4 permission → Task 5; §4.5 button → Task 12; §5 error cases → covered in Tasks 2 (None snapshots), 4 (404), 7 (status gate/403), 10 (empty timeline), 9 (judge-less fields); §7 testing → per-task; §8 sequencing → task order.

**Type consistency:** `FieldProvenance`/`CallAttempt`/`JudgeInfo` (Task 2) consumed unchanged in Tasks 3, 4, 6, 7; `build_workbook(schema_json, values, sources, provenance, attempts)` identical in Tasks 6/7; FE `FieldProvenance`/`CallAttempt` (Task 8) match the JSON emitted by Tasks 3/4; `provenanceFor`/`formId` (Task 8) consumed in Tasks 9/10/12; `ageLabel` (Task 8) consumed in Task 11.

**Known judgment points for the implementer:** exact fixture names in `test_patient_forms_review.py` (Task 1) and the `rls_policy_ddl` statement shapes (Task 5) are verified in-place rather than assumed — both steps say what to check and what to do with the answer.
