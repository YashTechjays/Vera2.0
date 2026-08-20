# Plan E — dispute suppression + per-attempt form view

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop showing a reviewer 152 meaningless disputes, tell them which answers came from a call
that proved nothing, and let them see what each attempt actually collected.

**Architecture:** `collected_per="call"` paths are excluded from the dispute derivation, and both
provenance DTOs gain an `authoritative` flag. The per-attempt view is assembled in the frontend from
data that already exists — `CallAttempt.changed_paths` (computed from `CallFormSnapshot`) and
`FieldProvenance.attempt`.

**Tech Stack:** Python 3.12, pydantic v2, SQLAlchemy async, pytest; React + TypeScript + vitest.

**Spec:** `vera-backend/docs/superpowers/specs/2026-08-21-retry-call-scoping-design.md` (D6, D9)

## Global Constraints

- Backend commands run from `vera-backend/`, frontend from `vera-frontend/`.
- **Do not change the global dispute rule.** `dispute_view` flagging any `ai_call` value that
  diverges from an absent intake baseline is a deliberate product stance — every AI-collected value
  gets a reviewer's eye. Suppression is **by declaration only**, for `collected_per="call"` paths
  (spec D6). Changing the global rule is a compliance-flavoured decision and is out of scope.
- **Non-authoritative answers stay `is_current`.** Nothing is demoted or deleted; a reviewer can
  still see what the rep said and accept it by hand via `dispute_action` (spec D9). Do not add a
  demote-and-restore path — `fa_current_uq` is a partial unique index and the invariant is
  fiddly to hold, and it blinds the reviewer for no gain.
- `build_field_views` is the **single source of truth for "is this a dispute"** — both the detail
  view and the `→ COMPLETED` gate go through `_field_views`, so the count and the detail can never
  disagree. Keep that property: change the rule in one place.
- PHI: field **values** flow through these views. Never log them. The `authoritative` flag and
  `call_id` are ids/booleans and safe.
- `after_state == {}` is the documented "not yet finalized" sentinel, written only by
  `post_call_eval`. It is **not** "nothing changed" — a call whose eval never ran has it, and the
  observed 08:01 call is a real example.
- Depends on Plan A (`collected_per_call_paths()`) and Plan B (`load_authoritative_call_ids`).
- `just check` verbatim, then `/simplify`, then `just check`. Frontend: `tsc` + `eslint` + `vitest`
  + `build`.

---

## File Structure

- **Modify** `packages/vera_core/src/vera_core/forms/review.py` — `build_field_views` (line 189)
- **Modify** `apps/control_plane/src/control_plane/api/v1/patient_forms.py`
  - `_field_views` (line 671) — needs the call-scoped set, so it needs the doc
  - `_call_attempt_view` (line 630) — carry `authoritative`
- **Modify** `packages/vera_core/src/vera_core/services/call_provenance.py`
  - `FieldProvenance` + `CallAttempt` gain `authoritative`
  - `load_field_provenance` / `load_call_attempts` take the authoritative set
- **Modify** `vera-frontend/src/lib/patient-forms/types.ts` — both DTOs
- **Modify** `vera-frontend/src/components/ibv/CallHistoryTab.tsx` — mark an attempt unverified
- **Modify** `vera-frontend/src/components/ibv/FieldRow.tsx` — mark a field unverified
- **Test** `tests/unit/forms/test_review.py`, `tests/integration/control_plane/` (the detail view),
  `vera-frontend/src/components/ibv/CallHistoryTab.test.tsx`

**Interfaces:**

```python
def build_field_views(
    current_answers: Iterable[AnswerRow],
    baseline_value_by_path: Mapping[str, Any],
    *,
    call_scoped_paths: Collection[str] = (),
) -> list[dict[str, Any]]: ...

@dataclass(frozen=True)
class FieldProvenance:
    attempt: int
    mode: str
    judge: JudgeInfo | None
    authoritative: bool = True      # appended with a default: existing constructions keep working

@dataclass(frozen=True)
class CallAttempt:
    ...
    authoritative: bool = True
```

---

### Task 1: suppress disputes on call-scoped paths

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/review.py:189`
- Modify: `apps/control_plane/src/control_plane/api/v1/patient_forms.py:671`
- Test: `tests/unit/forms/test_review.py`

**Interfaces:**
- Consumes: `FormSchemaDoc.collected_per_call_paths()` (Plan A).
- Produces: `build_field_views(..., call_scoped_paths=…)`.

- [ ] **Step 1: Write the failing test**

```python
class TestCallScopedPathsAreNeverDisputed:
    """A call-scoped answer has no form-level baseline by definition, so it cannot diverge from
    one. Today the rep's name and the call reference number are flagged on EVERY call with
    `previous_value: null` — 152 such views on the seeded form."""

    def _rows(self) -> list[AnswerRow]:
        return [
            AnswerRow(uuid4(), REP_NAME, {"value": "Priya Raman"}, "ai_call", 90, None),
            AnswerRow(uuid4(), REF, {"value": "9310-KT-04"}, "ai_call", 90, None),
            AnswerRow(uuid4(), COPAY, {"value": "$25"}, "ai_call", 90, None),
        ]

    def test_a_call_scoped_path_is_not_disputed(self) -> None:
        views = {
            v["field_path"]: v
            for v in build_field_views(self._rows(), {}, call_scoped_paths={REP_NAME, REF})
        }
        assert views[REP_NAME]["dispute"] is None
        assert views[REF]["dispute"] is None

    def test_a_form_scoped_path_is_still_disputed(self) -> None:
        """The global rule is untouched — only the declared paths are exempt."""
        views = {
            v["field_path"]: v
            for v in build_field_views(self._rows(), {}, call_scoped_paths={REP_NAME, REF})
        }
        assert views[COPAY]["dispute"] is not None
        assert views[COPAY]["dispute"]["previous_value"] is None

    def test_evidence_survives_suppression(self) -> None:
        """`evidence` is top-level precisely because an answer with no dispute still has evidence
        worth reviewing — suppressing the dispute must not hide it."""
        rows = [AnswerRow(uuid4(), REF, {"value": "R"}, "ai_call", 90, "the rep read it back")]
        [view] = build_field_views(rows, {}, call_scoped_paths={REF})
        assert view["dispute"] is None
        assert view["evidence"] == "the rep read it back"

    def test_default_is_unchanged_behaviour(self) -> None:
        """Callers that pass nothing keep today's semantics exactly."""
        views = build_field_views(self._rows(), {})
        assert all(v["dispute"] is not None for v in views)
```

`REP_NAME`, `REF`, `COPAY` are the real IBV paths; read the module's existing constants first, as
several are already defined there.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/forms/test_review.py -k CallScopedPathsAreNever -v`
Expected: FAIL — `build_field_views()` got an unexpected keyword argument `call_scoped_paths`.

- [ ] **Step 3: Add the parameter**

```python
def build_field_views(
    current_answers: Iterable[AnswerRow],
    baseline_value_by_path: Mapping[str, Any],
    *,
    call_scoped_paths: Collection[str] = (),
) -> list[dict[str, Any]]:
    """... (keep the existing docstring, and add:)

    `call_scoped_paths` (the schema's `collected_per="call"` leaves) are never disputed: their
    value describes ONE CALL, so there is no form-level baseline for it to diverge from and never
    will be. Without this, the rep's name and the call reference number are flagged on every call
    with `previous_value: null` forever. `evidence` still rides on the view — an answer with no
    dispute can still have evidence worth reading.
    """
    exempt = set(call_scoped_paths)
    views: list[dict[str, Any]] = []
    for answer in sorted(current_answers, key=lambda a: a.field_path):
        dispute = (
            None
            if answer.field_path in exempt
            else dispute_view(
                source=answer.source,
                value=answer.value,
                confidence=answer.confidence,
                baseline_value=baseline_value_by_path.get(answer.field_path),
            )
        )
        views.append({...})   # unchanged
    return views
```

- [ ] **Step 4: Thread the set through `_field_views`**

`_field_views(session, form_id)` has no schema document. It has three callers —
`_build_detail`, `_open_dispute_paths` and (via that) `_unresolved_dispute_count` — so add a
parameter rather than re-querying the schema inside it:

```python
async def _field_views(
    session: TenantSession, form_id: UUID, *, call_scoped_paths: Collection[str]
) -> list[dict[str, Any]]:
```

and give each caller the set. `_build_detail` already parses nothing but has `form`, so resolve the
document once there and pass it down; `_open_dispute_paths` needs the same, which means the
`→ COMPLETED` gate resolves it too. That is deliberate — the count and the detail must come from the
same rule, and passing the set explicitly is what makes a divergence impossible.

Add a small helper beside them rather than repeating the query:

```python
async def _call_scoped_paths(session: TenantSession, form: PatientForm) -> frozenset[str]:
    """The form's pinned schema's `collected_per="call"` leaves. Empty for a document predating the
    marker, which simply means nothing is exempt — today's behaviour."""
    version = (
        await session.execute(
            select(SchemaVersion).where(SchemaVersion.id == form.schema_version_id)
        )
    ).scalar_one()
    if not is_v2(version.schema_json):
        return frozenset()
    return FormSchemaDoc.model_validate(version.schema_json).collected_per_call_paths()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/forms/test_review.py tests/integration/control_plane/ -v`
Expected: PASS. Any integration test asserting a dispute count on a form with rep/reference answers
will drop by the number of call-scoped paths — verify each new count by hand.

- [ ] **Step 6: Confirm the effect on the seeded form**

Run `just seed-retry-form`, then fetch the detail (log in first; the tenant login returns
`session_token`, and the path parameter is the tenant **slug**):

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/tenants/vera-health-example/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"<you>","password":"<pw>"}' | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['session_token'])")
FID=$(docker exec vera-backend-postgres-1 psql -U vera -d "$DB" -tAc \
  "select id from patient_form where chart_number='TEST-SEED-RETRY'")
curl -s "http://localhost:8000/api/v1/patient-forms/$FID" -H "authorization: Bearer $TOKEN" \
  | python3 -c "
import json,sys
d = json.load(sys.stdin)['data']
print('disputed:', sum(1 for f in d['fields'] if f['dispute']))
for f in d['fields']:
    if 'insurance_representative' in f['field_path']:
        print(' ', f['field_path'], '-> dispute', f['dispute'])"
```
Expected: the rep/reference rows show `dispute None`, and the total is **exactly 2 lower** than
before. Not zero — the other 150 are form-scoped `ai_call` answers with no intake baseline, which
the global rule still flags by design.

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/review.py \
        apps/control_plane/src/control_plane/api/v1/patient_forms.py \
        tests/unit/forms/test_review.py
git commit -m "fix(ibv): never dispute a call-scoped answer"
```

---

### Task 2: flag answers a non-authoritative call produced

**Files:**
- Modify: `packages/vera_core/src/vera_core/services/call_provenance.py`
- Modify: `apps/control_plane/src/control_plane/api/v1/patient_forms.py` (`_call_attempt_view`,
  and the `FieldProvenance` DTO mapping in `_field_view`)
- Test: `tests/integration/control_plane/` (the detail + calls endpoints)

**Interfaces:**
- Consumes: `load_authoritative_call_ids` (Plan B).
- Produces: `FieldProvenance.authoritative`, `CallAttempt.authoritative`.

- [ ] **Step 1: Write the failing test**

```python
async def test_an_attempt_with_no_reference_number_is_flagged_unauthoritative(...) -> None:
    """A call that captured no reference number proved nothing: its answers are always re-asked
    (spec D8) and its verified contribution is zero (Plan D). The reviewer has to be able to SEE
    that, or the value looks as solid as any other."""
    form = await seed_form(...)
    good = await seed_call_with_reference(form, "R1")
    bad = await seed_call_without_reference(form)
    await seed_answer(form, good, DEDUCTIBLE, "$3,000")
    await seed_answer(form, bad, COPAY, "$25")

    attempts = await get_calls(client, form.id)
    assert {a["attempt"]: a["authoritative"] for a in attempts} == {1: True, 2: False}

    detail = await get_detail(client, form.id)
    prov = {f["field_path"]: f["provenance"] for f in detail["fields"] if f["provenance"]}
    assert prov[DEDUCTIBLE]["authoritative"] is True
    assert prov[COPAY]["authoritative"] is False
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/integration/control_plane/ -k authoritative -v`
Expected: FAIL on `KeyError: 'authoritative'`.

- [ ] **Step 3: Add the flag to both dataclasses**

`call_provenance.py`. **Appended with a default** in both cases, so existing constructions and tests
keep working:

```python
@dataclass(frozen=True)
class FieldProvenance:
    attempt: int
    mode: str
    judge: JudgeInfo | None
    # False when the call that produced this value captured no rep call reference number: nothing
    # ties the conversation to a payer-side record, so the answer is never treated as collected by
    # the retry ask set (spec D8) and contributes nothing to verified_pct (Plan D). Shown so a
    # reviewer can tell an unproven value from a confirmed one — it is NOT hidden or demoted.
    authoritative: bool = True


@dataclass(frozen=True)
class CallAttempt:
    ...
    authoritative: bool = True
```

- [ ] **Step 4: Populate it in both loaders**

Both take the set rather than querying it themselves, so one definition of "authoritative" serves
the ask set, the percentages and the view:

```python
async def load_call_attempts(
    session: AsyncSession, form_id: UUID, *, authoritative_calls: Collection[UUID] = ()
) -> list[CallAttempt]: ...

async def load_field_provenance(
    session: AsyncSession,
    form_id: UUID,
    attempt_by_call: Mapping[UUID, tuple[int, str]],
    *,
    authoritative_calls: Collection[UUID] = (),
) -> dict[str, FieldProvenance]: ...
```

In each, set `authoritative=call_id in authoritative_calls`. The defaults keep every existing caller
compiling; update the three in `patient_forms.py` (lines 732, 1013, 1277) to pass the real set from
`load_authoritative_call_ids`, and leave any test-only caller on the default.

Note line 1277's caller only reads `.source` off `load_field_status` — check whether it needs the
attempts at all before threading anything through it.

- [ ] **Step 5: Carry it into the DTOs**

`_call_attempt_view` gains `authoritative=a.authoritative`, and the `CallAttemptView` /
`FieldProvenance` response models each gain the field. Both are non-PHI booleans.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/control_plane/ tests/unit/forms/ -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/src/vera_core/services/call_provenance.py \
        apps/control_plane/src/control_plane/api/v1/patient_forms.py \
        tests/integration/control_plane/
git commit -m "feat(ibv): flag answers a non-authoritative call produced"
```

---

### Task 3: surface both in the review UI

**Files:**
- Modify: `vera-frontend/src/lib/patient-forms/types.ts`
- Modify: `vera-frontend/src/components/ibv/CallHistoryTab.tsx`
- Modify: `vera-frontend/src/components/ibv/FieldRow.tsx`
- Test: `vera-frontend/src/components/ibv/CallHistoryTab.test.tsx`

Run everything in this task from `vera-frontend/`.

**Interfaces:**
- Consumes: the two DTO fields from Task 2.
- Produces: nothing the backend reads.

- [ ] **Step 1: Extend the types**

`types.ts` already has `FieldProvenance = { attempt; mode; judge }` and a `CallAttempt` type. Add to
each:

```ts
/** Which call attempt produced a field's current value and the judge verdict. */
export type FieldProvenance = {
  attempt: number
  mode: "full" | "retry"
  judge: FieldJudge | null
  /** False when that call captured no reference number — the value is unproven, and the next
   *  retry will ask for it again. Shown, never hidden: a reviewer may still accept it. */
  authoritative: boolean
}
```

and the same field on `CallAttempt`.

- [ ] **Step 2: Write the failing component test**

```tsx
it("marks an attempt that captured no reference number", () => {
  render(<AttemptRow attempt={{ ...base, attempt: 2, authoritative: false }} />)
  expect(screen.getByText(/no call reference/i)).toBeInTheDocument()
})

it("does not mark an authoritative attempt", () => {
  render(<AttemptRow attempt={{ ...base, attempt: 1, authoritative: true }} />)
  expect(screen.queryByText(/no call reference/i)).not.toBeInTheDocument()
})
```

`AttemptRow` is the props-driven row component at the top of `CallHistoryTab.tsx` — its comment says
it is "props-driven (no hooks) so the play-control gating is unit-testable", so it takes a prop
directly. Follow the existing test file's render helpers.

- [ ] **Step 3: Run it to verify it fails**

Run: `npm test -- CallHistoryTab.test.tsx`
Expected: FAIL — no such text.

- [ ] **Step 4: Render the markers**

In `AttemptRow`, beside the existing `retry of attempt N` line:

```tsx
{!a.authoritative && (
  <span className="text-xs text-amber-600">
    no call reference — answers unverified
  </span>
)}
```

And in `FieldRow.tsx`, wherever the judge confidence chip renders, add the same signal for
`provenance?.authoritative === false`. Match the file's existing chip/badge idiom rather than
introducing a new one — read how the confidence bands are rendered first
(`vera-frontend/src/lib/ibv/disputes.ts` holds the 95/85/75 thresholds).

- [ ] **Step 5: Run the frontend gate**

Run: `npx tsc -b && npx eslint . && npm test && npm run build`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add vera-frontend/src/lib/patient-forms/types.ts \
        vera-frontend/src/components/ibv/CallHistoryTab.tsx \
        vera-frontend/src/components/ibv/FieldRow.tsx \
        vera-frontend/src/components/ibv/CallHistoryTab.test.tsx
git commit -m "feat(ibv): show which attempts and answers are unverified"
```

---

### Task 4: the per-attempt collected view

**Files:**
- Modify: `vera-frontend/src/components/ibv/CallHistoryTab.tsx`
- Test: `vera-frontend/src/components/ibv/CallHistoryTab.test.tsx`

**Interfaces:**
- Consumes: `CallAttempt.changed_paths` and `FieldProvenance.attempt`, both already served.
- Produces: nothing.

**No backend work.** `CallAttempt.changed_paths` is already computed by `snapshot_changed_paths`
from `CallFormSnapshot.before_state`/`after_state` and already returned by
`GET /patient-forms/{id}/calls`. `FieldProvenance.attempt` already attributes each current value to
its attempt. The aggregated form view stays as it is — this adds a per-attempt lens over data the
API already sends.

- [ ] **Step 1: Write the failing test**

```tsx
it("lists what an attempt changed, with titles rather than paths", () => {
  render(<AttemptRow attempt={{ ...base, attempt: 1, changed_paths: [DEDUCTIBLE, COPAY] }}
                     schema={schema} />)
  expect(screen.getByText("Individual Deductible")).toBeInTheDocument()
  expect(screen.queryByText(DEDUCTIBLE)).not.toBeInTheDocument()   // never a raw dotted path
})

it("distinguishes an unfinalized attempt from one that changed nothing", () => {
  // after_state == {} is the "never finalized" sentinel, not "nothing changed" — the 08:01 call
  // in the investigation had exactly this.
  render(<AttemptRow attempt={{ ...base, changed_paths: [], finalized: false }} schema={schema} />)
  expect(screen.getByText(/not finalized/i)).toBeInTheDocument()
})
```

The second test needs the API to distinguish the two cases. `changed_paths` is `[]` for both today.
**Check `snapshot_changed_paths` first** — if it cannot tell them apart, add a `finalized` boolean to
`CallAttempt` (true when `after_state` is non-empty) in a small backend step before this test, rather
than inferring it in the frontend from an empty list.

- [ ] **Step 2: Run it to verify it fails**

Run: `npm test -- CallHistoryTab.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Render the changed set**

Resolve each path to its leaf title through the schema the modal already holds, and render the list
under the attempt row. Reuse the existing path→title lookup if the module has one — `field_labels`
exists on the backend and the frontend has `allLeaves`; do not add a second mapping.

**Never render a raw dotted path to a user**, and remember the values themselves are PHI: this list
names *which* fields changed. Showing the values alongside is a bigger decision (it duplicates the
form view) — do not add it without asking.

- [ ] **Step 4: Run the frontend gate**

Run: `npx tsc -b && npx eslint . && npm test && npm run build`
Expected: PASS.

- [ ] **Step 5: Check it against a real form**

`just seed-retry-form` seeds attempt 1 **without** a `CallFormSnapshot` row, so its `changed_paths`
is empty and it will render as "not finalized". That is the seed's gap, not the view's — fix it by
having the seed write a snapshot (`before_state` = the intake values, `after_state` = the
post-call values), then confirm the attempt lists the fields the call collected.

- [ ] **Step 6: Commit**

```bash
git add vera-frontend/src/components/ibv/CallHistoryTab.tsx \
        vera-frontend/src/components/ibv/CallHistoryTab.test.tsx \
        vera-backend/scripts/seed_retry_form.py
git commit -m "feat(ibv): show what each attempt collected"
```

---

## Verification

Plan E is done when:

- `just check` passes verbatim, and the frontend gate passes from `vera-frontend/`.
- The seeded form's dispute count drops by **exactly 2** — the rep name and the call reference
  number. Not to zero: the other 150 are form-scoped `ai_call` answers with no intake baseline, which
  the global rule still flags by design and which this plan deliberately does not touch.
- `build_field_views` called without `call_scoped_paths` behaves exactly as before — the default
  keeps every other caller and test unchanged.
- The dispute count and the detail view still agree: both go through `_field_views`, and the
  `→ COMPLETED` gate uses the same call-scoped set.
- An attempt that captured no reference number reads `authoritative: false` on
  `GET /patient-forms/{id}/calls`, its answers read `provenance.authoritative: false` on the detail,
  and both render as unverified.
- An attempt's changed-field list shows leaf titles, never dotted paths, and an unfinalized attempt
  is distinguishable from one that changed nothing.
- Non-authoritative answers are still `is_current` — `git diff` shows no demotion path anywhere.

No live call is needed: this plan is read-side only. Its correctness is visible on the seeded form.
