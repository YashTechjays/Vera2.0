# Retry Decision — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the retry decision coherent and legible — one gate reading one number, a call
labelled by what it actually asked, and the decision inputs exposed to the review UI.

**Architecture:** Six independent backend changes in `vera_core` and `control_plane`. Two are
pure plumbing corrections that are byte-identical on default settings (Tasks 1, 3). One
relabels a display field from the predicate that already exists (Task 2). Two change retry
behaviour and need the live merge gate (Tasks 4, 5). One is additive DTO exposure (Task 6).

**Tech Stack:** Python 3.12, SQLAlchemy 2 async, FastAPI, pydantic v2, pytest, openpyxl,
ruff + mypy --strict (which covers `tests/` in this repo).

**Spec:** `docs/superpowers/specs/2026-08-25-per-call-answers-review-ux-design.md`
(items B1–B7; B8 is a frontend file and belongs to the frontend plan)

## Global Constraints

- **PHI:** field values, transcript quotes and judge evidence never reach a log, span, URL,
  query string or browser store. Log `type(exc).__name__`, never an exception repr.
- **Every new assertion gets a named mutation that must fail it**, run and recorded. Review §11
  found eight tests on this branch that passed with their feature deleted.
- **`mypy --strict` covers `tests/`** — every test helper needs annotations, and mypy must be run
  on **every file you changed, named explicitly**. Scoping it to the production module is how an
  unannotated test helper reaches the gate.
- Integration tests run via `just test <path>`, **never** bare `uv run pytest`: `justfile` sets
  `dotenv-load := true` so a recipe exports the dotenv as real process vars, while
  `tests/integration/conftest.py` builds `Settings(_env_file=None)` and reads only real env vars.
  A bare pytest silently falls back to a different database and dies on a stale
  `alembic_version`. Symptom: a test that fails alone and passes under `just test`.
- Full gate: **`just check` verbatim** from `vera-backend/`, never a hand-picked subset — `ruff
  check` and `ruff format --check` are different gates, and running only the former is how a
  formatting-only failure reached dev CI post-merge (PR #105 → #107). ~3 min; do not leave
  Langfuse running, it inflates this ~3.4x (review §9.1).
- **No migration in this plan.** Task 6 adds pydantic response fields, not columns —
  `verified_pct`, `review_reason` and `retry_count` are existing `patient_form` columns and
  `max_retries` is an existing `tenant` column. If you find yourself reaching for
  `just makemigration`, stop: something has drifted from this plan.
- **Tasks 1 and 5 modify long-lived background loops** (`PipelineSweeper`, and
  `WorkerEventConsumer` — a Redis Streams consumer). Per the repo rule, a constructor/loop change
  is not verified by pytest: it must be checked by BOOTING the service. See the final gate.
- The confidence floor default is **70** in two places that must not diverge:
  `REVIEW_CONFIDENCE_FLOOR` (`vera_core/forms/review.py:39`) and
  `settings.post_call_review_floor` (`vera_core/config/settings.py:63`).
- Do not change `unsatisfied_required_paths` to require an *authoritative call*. Spec B7 is
  narrower (intake vs a confirm leaf). Requiring authoritativeness would mean a form whose rep
  never gives a reference number could never reach review — a retry storm.
- Services take settings-derived values as **parameters injected by the app layer**
  (`recording_config_from(settings)` is the house pattern). Do not call `get_settings()` inside
  `vera_core`.

---

## File Structure

| file | responsibility | task |
|---|---|---|
| `apps/control_plane/src/control_plane/dispatch.py` | thread `retry_floor` from the app layer to `try_dispatch` | 1 |
| `packages/vera_core/src/vera_core/services/queue_dispatcher.py` | correct the `retry_floor` docstring; derive `call_mode` + lineage from narrowing | 1, 2 |
| `apps/control_plane/src/control_plane/api/v1/patient_forms.py` | pass `authoritative_calls` to the export loaders; add the five detail fields | 3, 6 |
| `packages/vera_core/src/vera_core/forms/export.py` | render the authoritative column on both sheets | 3 |
| `packages/vera_core/src/vera_core/forms/review.py` | `_confirm_paths` + the confirm-role satisfaction rule | 4 |
| `packages/vera_core/src/vera_core/services/verification.py` | **new** — `load_verified_fraction`, the one place the verified fraction is computed from a session | 5 |
| `apps/control_plane/src/control_plane/post_call.py` | gate on the verified fraction instead of `completion_pct` | 5 |
| `apps/control_plane/src/control_plane/pipeline_sweeper.py`, `worker_events.py`, `api/v1/calls.py` | pass the floor at their dispatch / resolve call sites | 1, 5 |

`verification.py` is new rather than folded into `field_status.py` because it is the only module
that needs `SchemaVersion` + `satisfied_required_fraction` together, and keeping it separate lets
the `post_call.py` unit test monkeypatch exactly one symbol instead of extending the fake
session's entity routing.

> Drive-by observation, **no task**: `PatientFormDetail(...)` at `patient_forms.py:801` passes
> `member_id=form.member_id`, but the model (`:598`) declares no `member_id`, so pydantic's
> default `extra="ignore"` drops it silently. Harmless and pre-existing. Do not "fix" it as part
> of Task 6 — it is a separate decision about whether the detail should carry that column.

---

### Task 1: Wire the confidence floor to the dispatch path

The floor now selects the retry ask set (`focus_paths(floor=retry_floor)`,
`queue_dispatcher.py:418`), but `dispatch.py:149` never passes it, so production takes the module
default while `post_call_consumer.py:99` takes the env-configured one. With
`VERA_POST_CALL_REVIEW_FLOOR=85`, a field at confidence 78 is counted unsatisfied by the eval
(triggering the retry) and confirmed by the focus set (dropped from the ask set) — the retry
dials and never asks the question that caused it.

**Files:**
- Modify: `apps/control_plane/src/control_plane/dispatch.py` (3 signatures + 1 call)
- Modify: `packages/vera_core/src/vera_core/services/queue_dispatcher.py:158-166` (docstring)
- Modify: `apps/control_plane/src/control_plane/api/v1/patient_forms.py:1630`
- Modify: `apps/control_plane/src/control_plane/pipeline_sweeper.py:275` (+ constructor)
- Modify: `apps/control_plane/src/control_plane/worker_events.py:837`
- Modify: `apps/control_plane/src/control_plane/api/v1/calls.py:845`
- Modify: `apps/control_plane/src/control_plane/main.py` (sweeper construction)
- Test: `tests/unit/services/test_queue_dispatcher.py`, `tests/unit/config/test_review_floor_default.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `schedule_dispatch_pass(..., retry_floor: int | None = None)`,
  `run_dispatch_pass(..., retry_floor: int | None = None)`,
  `_dispatch_pass(..., retry_floor: int | None = None)`. `None` means "use `try_dispatch`'s own
  default", so no existing caller changes behaviour.

- [ ] **Step 1: Write the failing drift guard**

Create `tests/unit/config/test_review_floor_default.py`:

```python
"""The confidence floor has two defaults that must not diverge: the module constant every
`review.py` caller falls back to, and the setting the app layer injects."""

from vera_core.config.settings import Settings
from vera_core.forms.review import REVIEW_CONFIDENCE_FLOOR


def test_settings_default_matches_the_module_constant() -> None:
    # An import from `config` into `forms` would couple the layers, so the two are pinned
    # here instead. If this fails, one of them moved and every gate silently disagreed.
    assert Settings.model_fields["post_call_review_floor"].default == REVIEW_CONFIDENCE_FLOOR
```

- [ ] **Step 2: Run it — it should PASS already**

Run: `just test tests/unit/config/test_review_floor_default.py -v`
Expected: PASS (both are 70 today). This is a guard, not a red test.
**Mutation to record:** temporarily change `post_call_review_floor: int = 70` to `= 75` in
`settings.py` and confirm this test FAILS. Revert.

- [ ] **Step 3: Write the failing behavioural test for the wiring**

Add to `tests/unit/services/test_queue_dispatcher.py`. Read the file's existing dispatch
fixtures first and reuse them — do not invent a second harness. The test asserts the *ask set*,
not that a kwarg was passed: a kwarg assertion passes with the feature reduced to plumbing.

```python
async def test_dispatch_pass_forwards_the_settings_floor_to_the_focus_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A field whose confidence sits BETWEEN the module default (70) and an injected
    floor (85) must be in the ask set when the injected floor is used, and absent when
    the module default leaks through."""
    seen: dict[str, int] = {}

    async def _fake_try_dispatch(*args: object, retry_floor: int = 70, **kw: object) -> int:
        seen["floor"] = retry_floor
        return 0

    monkeypatch.setattr("control_plane.dispatch.try_dispatch", _fake_try_dispatch)
    await run_dispatch_pass(_SM, TENANT, _livekit(), _kms(), None, retry_floor=85)
    assert seen["floor"] == 85
```

> The kwarg check above is only the plumbing half. Pair it with the ask-set half in
> `tests/integration/control_plane/` using the existing focused-retry fixture: seed a form whose
> single gap carries judge confidence 78, dispatch with `retry_floor=85`, and assert the staged
> plan's questions **include** that path; dispatch with `retry_floor=70` and assert they
> **exclude** it. That test fails if the wiring is dropped; the unit test alone does not.

- [ ] **Step 4: Run it to verify it fails**

Run: `just test tests/unit/services/test_queue_dispatcher.py -k forwards_the_settings_floor -v`
Expected: FAIL — `run_dispatch_pass() got an unexpected keyword argument 'retry_floor'`

- [ ] **Step 5: Thread the parameter**

In `dispatch.py`, add to all three signatures, beside `plan_service`:

```python
    plan_service: "CallPlanService | None" = None,
    retry_floor: int | None = None,
```

and forward it in both inner calls (`schedule_dispatch_pass` -> `_dispatch_pass`,
`run_dispatch_pass` -> `_dispatch_pass`) as `retry_floor=retry_floor`. In `_dispatch_pass`, pass
it to `try_dispatch` only when set, so `None` keeps `try_dispatch`'s own default:

```python
            await try_dispatch(
                session,
                tenant_id,
                livekit,
                kms,
                audit=audit,
                recording=recording,
                plan_service=plan_service,
                **({} if retry_floor is None else {"retry_floor": retry_floor}),
            )
```

- [ ] **Step 6: Pass it at the four call sites**

`api/v1/patient_forms.py:1630` and `api/v1/calls.py:845` already have `settings` in scope — add
`retry_floor=settings.post_call_review_floor`. `pipeline_sweeper.py` and `worker_events.py` are both classes
(`WorkerEventConsumer` is at `worker_events.py:151`) and take theirs by injection like
`self._recording`: add a `review_floor: int` constructor parameter,
store it as `self._review_floor`, pass `retry_floor=self._review_floor` at the call site, and
supply `review_floor=settings.post_call_review_floor` where `main.py` constructs them.

- [ ] **Step 7: Correct the docstring**

`queue_dispatcher.py`, the `retry_floor:` entry (~`:158-166`). Replace the "Best-effort prompt
guidance only — the authoritative retry-vs-review decision happened earlier in ``evaluate_call``"
text with:

```
    retry_floor:
        Confidence floor for `is_call_confirmed`. Selects the FOCUSED retry ask
        set (`focus_paths`, below) and the field labels embedded in RETRY room
        metadata. Must be the same value the post-call eval uses
        (`settings.post_call_review_floor`) or the two gates measure different
        populations: a field between the two floors triggers a retry that then
        never asks it.
```

- [ ] **Step 8: Run the tests**

Run: `just test tests/unit/services/test_queue_dispatcher.py tests/unit/config/ -v`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add apps/control_plane/src/control_plane/dispatch.py \
        apps/control_plane/src/control_plane/pipeline_sweeper.py \
        apps/control_plane/src/control_plane/worker_events.py \
        apps/control_plane/src/control_plane/main.py \
        apps/control_plane/src/control_plane/api/v1/patient_forms.py \
        apps/control_plane/src/control_plane/api/v1/calls.py \
        packages/vera_core/src/vera_core/services/queue_dispatcher.py \
        tests/unit/services/test_queue_dispatcher.py \
        tests/unit/config/test_review_floor_default.py
git commit -m "fix(dispatch): wire the review floor to the retry ask set

retry_floor selects focus_paths' ask set since Plan B, but the dispatch call
site never passed it, so production used the module default while the post-call
eval used the env-configured one. A field between the two floors triggered a
retry that then never asked it."
```

---

### Task 2: `call_mode` and lineage describe the call, not the retry budget

Review §14.2. A manual requeue resets `retry_count`, so an operator-triggered focused retry
writes `Call.mode="full"` and no `CallLineage` row — the attempt timeline reports a narrowed
16-question call as a full call with no parent. Both consumers of `Call.mode`
(`call_provenance.py:115`, `calls.py:1098`) are display-only, so redefining it changes no gate.

**Files:**
- Modify: `packages/vera_core/src/vera_core/services/queue_dispatcher.py:369, 397-424, 470-480`
- Test: `tests/unit/services/test_queue_dispatcher.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Call.mode == "retry"` iff the staged plan was narrowed; a `CallLineage` row
  whenever the form has any prior call. `CallAttempt.mode` / `FieldProvenance.mode` inherit the
  new meaning with no signature change.

- [ ] **Step 1: Write the two failing tests**

```python
async def test_operator_requeue_with_a_reference_number_is_labelled_retry() -> None:
    """The branch's premise: a manual requeue resets retry_count, so the OLD rule labelled a
    narrowed call `full`. Mode must follow what was staged, not the retry budget."""
    form = _form(retry_count=0, schema_version_id=V2_ID)   # manual requeue resets the budget
    _seed_reference_number_captured_by_a_call(form)
    call = await _dispatch_one(form)
    assert call.mode == "retry"


async def test_a_prior_call_always_writes_lineage_even_when_the_plan_was_not_narrowed() -> None:
    """A second attempt is a retry of the first whether or not it was focused — the timeline's
    'retry of attempt N' must not depend on the mode label."""
    form = _form(retry_count=0, schema_version_id=V2_ID)
    parent = await _dispatch_one(form)          # first call, nothing on file, runs fresh
    child = await _dispatch_one(form)
    row = await _lineage_for(child.id)
    assert row is not None and row.parent_call_id == parent.id
```

Reuse the module's existing dispatch harness for `_form`, `_dispatch_one` and the reference-number
seeding — read the file and match its fixtures rather than adding new ones.

- [ ] **Step 2: Run them to verify they fail**

Run: `just test tests/unit/services/test_queue_dispatcher.py -k "labelled_retry or always_writes_lineage" -v`
Expected: FAIL — first asserts `"full" == "retry"`; second finds no lineage row.

- [ ] **Step 3: Derive the mode from narrowing**

At `:369`, keep the budget read but rename it so the two meanings are visibly separate:

```python
        # Retry BUDGET, not call shape: a manual requeue resets retry_count, so this cannot
        # decide whether the plan was narrowed. `focused` below does that.
        budgeted_retry = form.retry_count > 0
        focused = False
```

Inside the focus block, at the point the narrowed plan is staged:

```python
                if focus:
                    staged_plan = (
                        focus_call_plan(plan, focus, answers=values),
                        plan_prompt_version_id,
                    )
                    focused = True
```

Then, before the `Call(...)` construction:

```python
        # `mode` describes THIS call: "retry" means the question tree was narrowed, which is
        # what the attempt timeline reports. A budgeted retry that runs FRESH (no reference
        # number on file) is honestly a full call.
        call_mode = CallMode.RETRY if focused else CallMode.FULL
```

Delete the old `call_mode = CallMode.RETRY if form.retry_count > 0 else CallMode.FULL` line.

- [ ] **Step 4: Decouple the lineage insert**

Replace `if call_mode == CallMode.RETRY:` at `:472` with an unconditional lookup, since the
query already returns `None` when there is no prior call:

```python
                # Any prior call on this form makes this one its retry, whatever the plan's
                # shape — the timeline's "retry of attempt N" must not depend on the label.
                parent_call_id = (
                    await session.execute(
                        select(Call.id)
                        .where(Call.form_id == form.id, Call.id != call.id)
                        .order_by(Call.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
```

Leave the surrounding comment about the retry being scoped by the plan rather than a prompt
overlay — it is still true and still load-bearing.

If `budgeted_retry` is now unused after this, delete it rather than leaving a dead local; if the
dispatch-audit payload still reports a mode, have it report `call_mode.value` so the audit and
the row agree.

- [ ] **Step 5: Run the tests**

Run: `just test tests/unit/services/test_queue_dispatcher.py -v`
Expected: PASS, and no pre-existing dispatcher test regresses.

- [ ] **Step 6: Record the mutations**

- Revert Step 3's `focused = True` → `test_operator_requeue_with_a_reference_number_is_labelled_retry` must FAIL.
- Restore `if call_mode == CallMode.RETRY:` around the parent lookup →
  `test_a_prior_call_always_writes_lineage...` must FAIL.

Record both in the progress ledger, then restore.

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/src/vera_core/services/queue_dispatcher.py \
        tests/unit/services/test_queue_dispatcher.py
git commit -m "fix(dispatch): label a call by what it asked, not by the retry budget

Call.mode now means 'the question tree was narrowed' and CallLineage is written
whenever a prior call exists. An operator requeue resets retry_count, so the old
rule reported a narrowed 16-question retry as a full call with no parent."
```

---

### Task 3: The export states the authoritative flag instead of implying it

`build_workbook` already writes a Provenance sheet with `Attempt | Mode | Judge confidence |
Supported`, and the endpoint already loads `attempts` and `prov`. It calls both loaders without
`authoritative_calls`, so `_authoritative` returns the dataclass default and **every row would
read `True`** — including the unverified ones — the moment a column is added.

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/patient_forms.py:1339-1343`
- Modify: `packages/vera_core/src/vera_core/forms/export.py:63-92`
- Test: `tests/unit/forms/test_export_workbook.py`,
  `tests/integration/control_plane/test_call_authoritative.py`

**Interfaces:**
- Consumes: `_v2_doc_for` and `_authoritative_call_ids` (`patient_forms.py:684, 701`) — both
  already exist and are already used by the detail endpoint.
- Produces: the Provenance sheet's per-field heading row becomes
  `["Field path", "Label", "Source", "Attempt", "Mode", "Judge confidence", "Supported", "Authoritative"]`
  and the Call-history heading row becomes
  `["Attempt", "Mode", "Status", "Created at", "Retry of attempt", "Authoritative"]`.

- [ ] **Step 1: Write the failing unit test**

First give the module's existing `_attempt` helper (`tests/unit/forms/test_export_workbook.py:51`)
an `authoritative` parameter with a default, so no existing call site changes:

```python
def _attempt(n: int, mode: str, *, authoritative: bool = True) -> CallAttempt:
    return CallAttempt(
        id=uuid4(),
        attempt=n,
        mode=mode,
        status="completed",
        created_at=datetime(2026, 7, 10, tzinfo=UTC),
        retry_of=None,
        changed_paths=[],
        authoritative=authoritative,
    )
```

Then add the test. It reads cells the way this module already does — `prov[1]` for the header
row and `prov.iter_rows(values_only=True)` for the body — reusing `_judged_export`'s shape:

```python
def test_a_non_authoritative_call_is_reported_as_such_on_both_sheets() -> None:
    """The flag must be RENDERED, not defaulted: a workbook that silently claims every answer
    is payer-proven is worse than one that omits the column (spec E7)."""
    schema_json = json.loads((FORM_SCHEMA_DIR / "ibv_form_standard_v2.json").read_text("utf-8"))
    path = f"{IVF}.cpt_58970.covered"
    data = build_workbook(
        schema_json,
        values={path: "Yes"},
        sources={path: "ai_call"},
        provenance={
            path: FieldProvenance(attempt=1, mode="full", judge=None, authoritative=False)
        },
        attempts=[_attempt(1, "full", authoritative=False)],
    )
    prov = load_workbook(BytesIO(data))["Provenance"]

    header = [c.value for c in prov[1]]
    assert header[7] == "Authoritative"

    rows = {r[0]: r for r in prov.iter_rows(values_only=True) if r[0]}
    assert rows[path][7] is False

    # The Call-history block repeats a heading row further down the same sheet.
    history_header = next(
        r for r in prov.iter_rows(values_only=True) if r and r[0] == "Attempt"
    )
    assert history_header[5] == "Authoritative"
    history_row = next(r for r in prov.iter_rows(values_only=True) if r and r[0] == 1)
    assert history_row[5] is False
```

`IVF` and `FORM_SCHEMA_DIR` already exist in this module. Index by **position, not `[-1]`** — a
trailing `None` from openpyxl's ragged rows would make `[-1]` pass vacuously.

- [ ] **Step 2: Run it to verify it fails**

Run: `just test tests/unit/forms/test_export_workbook.py -k non_authoritative -v`
Expected: FAIL — `header[7]` is `None` (only 7 columns exist).

> Note on existing tests: `test_judged_export_labels_every_provenance_row_and_bolds_both_headers`
> asserts `header[:7] == [...]`, a **prefix slice**, so adding an eighth column does *not* break
> it — which is exactly why the new test must assert index 7 explicitly. Do widen that test's
> bold check from `range(1, 8)` to `range(1, 9)`, or the new heading's boldness goes unverified.

- [ ] **Step 3: Render the column**

In `export.py`, extend the per-field headings and row:

```python
    _column_headings(
        prov_ws,
        [
            "Field path", "Label", "Source", "Attempt", "Mode",
            "Judge confidence", "Supported",
            # False = the call that produced this value captured no rep reference number,
            # so nothing ties it to a payer-side record. The value is still current.
            "Authoritative",
        ],
    )
```

and append `p.authoritative if p else None` as the row's last cell. Then the Call-history
headings gain `"Authoritative"` and each row appends `a.authoritative`.

- [ ] **Step 4: Run it to verify it passes**

Run: `just test tests/unit/forms/test_export_workbook.py -v`
Expected: PASS, including the widened bold check.

- [ ] **Step 5: Write the failing endpoint test**

The unit test proves the renderer. This proves the *call site*, which is where the bug is.

Put it in `tests/integration/control_plane/test_call_authoritative.py`, **not** in
`test_form_export.py`. `test_call_authoritative.py` already owns this concept and has the fixture —
`two_call_form` (`:66`), "one form, two completed calls: `good` captured the rep's call reference
number" and the other did not — plus `ibv_schema_version_id`, `REF` and `_auth`. Do not hand-roll
a second one. (Review §13's note that the *seeder* has never built a non-authoritative form
stands; the test fixture is separate.)

The form must be COMPLETED for the export endpoint to accept it, so transition it before
exporting.

```python
async def test_export_reports_a_call_that_captured_no_reference_number(
    client: httpx.AsyncClient,
    reviewer_token: str,
    two_call_form: Any,
) -> None:
    """Regression for the real defect: the endpoint passed authoritative_calls=None, so the
    loaders' dataclass default made every Provenance row claim payer-side proof (spec E7)."""
    form_id = two_call_form.form_id
    await _mark_completed(form_id)          # export rejects a non-COMPLETED form
    resp = await client.get(
        f"/api/v1/patient-forms/{form_id}/export", headers=_auth(reviewer_token)
    )
    assert resp.status_code == 200
    prov = load_workbook(BytesIO(resp.content))["Provenance"]
    rows = {r[0]: r for r in prov.iter_rows(values_only=True) if r[0]}
    # The path answered by the call that captured NO reference number.
    assert rows[two_call_form.unproven_path][7] is False
    # And the one answered by the call that did — proves the column is computed, not constant.
    assert rows[two_call_form.proven_path][7] is True
```

Read `two_call_form` first and use whatever it actually names its two paths and its form id; the
attribute names above are placeholders for that fixture's real shape. Asserting **both** values
is what stops the test passing against a hardcoded `False`.

- [ ] **Step 6: Run it to verify it fails**

Run: `just test tests/integration/control_plane/test_call_authoritative.py -k no_reference_number -v`
Expected: FAIL — the unproven path's cell reads `True`.

- [ ] **Step 7: Pass the authoritative set at the call site**

In the export endpoint, between the `version` fetch and the `load_call_attempts` call:

```python
    doc = _v2_doc(version.schema_json)
    authoritative_calls = await _authoritative_call_ids(session, form_id, doc)
    attempts = await load_call_attempts(
        session, form_id, authoritative_calls=authoritative_calls
    )
    prov = await load_field_provenance(
        session,
        form_id,
        {a.id: (a.attempt, a.mode) for a in attempts},
        authoritative_calls=authoritative_calls,
    )
```

- [ ] **Step 8: Run both suites**

Run: `just test tests/unit/forms/test_export_workbook.py tests/integration/control_plane/test_call_authoritative.py -v`
Expected: PASS

- [ ] **Step 9: Record the mutation**

Revert Step 7 to `load_call_attempts(session, form_id)` (no `authoritative_calls`) → the Step 5
test must FAIL. This is the assertion that actually guards the defect; if it still passes, the
test is reading the renderer rather than the call site. Restore.

- [ ] **Step 10: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/patient_forms.py \
        packages/vera_core/src/vera_core/forms/export.py \
        tests/unit/forms/test_export_workbook.py \
        tests/integration/control_plane/test_call_authoritative.py
git commit -m "fix(export): state the authoritative flag instead of defaulting it True

The export endpoint was the last authoritative_calls=None caller, so every
Provenance row would have claimed payer-side proof once the column existed."
```

---

### Task 4: A confirm-role leaf needs a call, not just intake

Review §4.1. `is_field_satisfied` trusts any intake value, and `if not unsatisfied ->
READY_FOR_REVIEW` is the FIRST gate in `evaluate_call`. So a `policy_number` typed at intake lets
a form be declared ready with no payer confirmation of the field that identifies the policy.

Measured scope (spec E8): three confirm leaves exist; `spouse_partner_name` and
`spouse_partner_dob` both declare `default: "N/A"`, and `_required_paths` drops defaulted leaves,
so they are already outside every gate population — **this change affects `policy_number`
alone.** That is what keeps it from becoming a retry storm on data a payer will not disclose.

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/review.py:325-365`
- Test: `tests/unit/forms/test_retryable_fields.py`

**Interfaces:**
- Consumes: `AnswerSource` (already imported in `review.py`), `FormSchemaDoc.leaf_items()`.
- Produces: `_confirm_paths(schema_json) -> frozenset[str]`; `_satisfied(...)` gains a keyword-only
  `confirm_paths: Collection[str] = ()`. `_unsatisfied` computes and forwards it, so both
  `unsatisfied_required_paths` and `retryable_required_paths` adopt the rule — a form is not
  complete AND a retry can fix it, which is the coherent pair. `satisfied_required_fraction` and
  `focus_paths` route through `_confirmed`, not `_satisfied`, and are unaffected.

> Review §14 verified `_satisfied`, `unsatisfied_required_paths` and `retryable_required_paths`
> as byte-identical to the merge base. This task deliberately ends that guarantee. Say so in the
> commit message so the next reviewer does not read it as an accident.

- [ ] **Step 1: Write the failing tests**

```python
def test_intake_does_not_satisfy_a_confirm_leaf() -> None:
    """A confirm leaf's declared purpose is payer confirmation, so the value typed at intake
    is the thing to be confirmed, not the confirmation (spec §4.1)."""
    status = {POLICY_NUMBER: FieldStatus(AnswerSource.INTAKE.value, None, None)}
    assert POLICY_NUMBER in unsatisfied_required_paths(
        status, SCHEMA_JSON, floor=REVIEW_CONFIDENCE_FLOOR, values=INTAKE_VALUES
    )


def test_a_call_satisfies_a_confirm_leaf() -> None:
    status = {
        POLICY_NUMBER: FieldStatus(AnswerSource.AI_CALL.value, True, 90, uuid4()),
    }
    assert POLICY_NUMBER not in unsatisfied_required_paths(
        status, SCHEMA_JSON, floor=REVIEW_CONFIDENCE_FLOOR, values=INTAKE_VALUES
    )


def test_a_human_edit_satisfies_a_confirm_leaf() -> None:
    """A reviewer typing the value IS a decision — only intake is excluded. Without this the
    reviewer could never clear the field and the form could never complete."""
    status = {POLICY_NUMBER: FieldStatus(AnswerSource.HUMAN.value, None, None)}
    assert POLICY_NUMBER not in unsatisfied_required_paths(
        status, SCHEMA_JSON, floor=REVIEW_CONFIDENCE_FLOOR, values=INTAKE_VALUES
    )


def test_a_defaulted_confirm_leaf_stays_outside_every_gate() -> None:
    """The coverage-flip case: the rep says Family mid-call and will not disclose dependent
    PHI. Both spouse leaves declare `default: "N/A"`, so they must never enter the gate
    population and never point the retry loop at data the payer cannot give (spec E8)."""
    values = dict(INTAKE_VALUES) | {COVERAGE_TYPE: "Family"}
    status = {COVERAGE_TYPE: FieldStatus(AnswerSource.AI_CALL.value, True, 95, uuid4())}
    unsat = unsatisfied_required_paths(
        status, SCHEMA_JSON, floor=REVIEW_CONFIDENCE_FLOOR, values=values
    )
    retryable = retryable_required_paths(
        status, SCHEMA_JSON, floor=REVIEW_CONFIDENCE_FLOOR, values=values
    )
    for path in (SPOUSE_NAME, SPOUSE_DOB):
        assert path not in unsat
        assert path not in retryable


def test_no_confirm_leaf_is_an_either_or_member_in_any_shipped_catalog() -> None:
    """The confirm rule is applied to the leaf itself, not to its either/or siblings — sound
    only while no confirm leaf has any. If a catalog adds one, revisit `_satisfied`."""
    for schema_json in (IBV_STANDARD_V2, DISEASE_ONLY_V2):
        confirm = _confirm_paths(schema_json)
        members = {m for pair in alternative_pairs(
            FormSchemaDoc.model_validate(schema_json)) for m in pair}
        assert not (confirm & members)
```

Constants: `POLICY_NUMBER = "sections.insurance_information.policy_number"`,
`SPOUSE_NAME = "sections.patient_information.spouse_partner_name"`,
`SPOUSE_DOB = "sections.patient_information.spouse_partner_dob"`,
`COVERAGE_TYPE = "sections.benefit_coverage.coverage_type"`. `SCHEMA_JSON` is the compiled
artifact `data/form_schemas/ibv_form_standard_v2.json` — match how the module already loads it.
`INTAKE_VALUES` fills `required_intake_fields(SCHEMA_JSON)`.

- [ ] **Step 2: Run them to verify they fail**

Run: `just test tests/unit/forms/test_retryable_fields.py -k "confirm_leaf or defaulted_confirm or either_or_member" -v`
Expected: `test_intake_does_not_satisfy_a_confirm_leaf` FAILS (intake satisfies today) and
`test_no_confirm_leaf_is_an_either_or_member...` FAILS with `NameError: _confirm_paths`. The
other three PASS already — they are the guards that this change does **not** break the payer-
refusal path or the reviewer's ability to clear the field.

- [ ] **Step 3: Add `_confirm_paths`**

Beside `_alternatives` in `review.py`:

```python
def _confirm_paths(schema_json: Mapping[str, Any]) -> frozenset[str]:
    """Paths of `role="confirm"` leaves. Their declared purpose is payer CONFIRMATION, so the
    intake value is the thing to be confirmed, not the confirmation (spec §4.1). Empty for v1,
    which has no role concept."""
    if not is_v2(schema_json):
        return frozenset()
    doc = FormSchemaDoc.model_validate(schema_json)
    return frozenset(path for path, leaf in doc.leaf_items() if leaf.role == "confirm")
```

- [ ] **Step 4: Apply the rule in `_satisfied`**

```python
def _satisfied(
    path: str,
    status_by_path: Mapping[str, FieldStatus],
    alternatives: AlternativeIndex,
    *,
    floor: int,
    confirm_paths: Collection[str] = (),
) -> bool:
    """Satisfied itself, or by a sibling in its either/or group — one answer satisfies the pair.

    A `confirm`-role leaf is NOT satisfied by intake alone (spec §4.1); a human edit still is,
    since that is a reviewer's deliberate decision. The rule is applied to the leaf itself and
    not to its siblings: no confirm leaf is an either/or member in any shipped catalog, pinned
    by `test_no_confirm_leaf_is_an_either_or_member_in_any_shipped_catalog`."""
    status = status_by_path.get(path)
    if path in confirm_paths and status is not None and status.source == AnswerSource.INTAKE.value:
        own = False
    else:
        own = is_field_satisfied(status, floor=floor)
    return own or any(
        is_field_satisfied(status_by_path.get(other), floor=floor)
        for other in alternatives.get(path, ())
    )
```

and in `_unsatisfied`, compute it once and forward:

```python
    alternatives = _alternatives(schema_json)
    confirm_paths = _confirm_paths(schema_json)
    return applicable, [
        path
        for path in applicable
        if not _satisfied(
            path, status_by_path, alternatives, floor=floor, confirm_paths=confirm_paths
        )
    ]
```

- [ ] **Step 5: Run the tests**

Run: `just test tests/unit/forms/ -v`
Expected: PASS. `tests/unit/forms/test_retry_scope_simulation.py` and the Plan F suite exercise
these functions heavily — if any regresses, the regression is real, not fixture noise.

- [ ] **Step 6: Record the mutation**

Delete the `if path in confirm_paths ...` branch (leaving `own = is_field_satisfied(...)`) →
`test_intake_does_not_satisfy_a_confirm_leaf` must FAIL. Restore.

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/review.py \
        tests/unit/forms/test_retryable_fields.py
git commit -m "fix(review): a confirm-role leaf needs a call, not just intake

Ends review §14's byte-identical guarantee on _satisfied,
unsatisfied_required_paths and retryable_required_paths — deliberately.
Measured scope is policy_number alone: the two spouse confirm leaves declare
default 'N/A', so _required_paths already excludes them from every gate and the
payer-refusal path is untouched. A human edit still satisfies."
```

---

### Task 5: One retry gate, one number

Spec E3: `post_call_eval` gates on the verified fraction (population 34, requires an
authoritative call) while `post_call.py:93` gates on `completion_pct` (population 39, counts
defaults filled, trusts intake). Measured on the artifact, S4 at T=0.90 → the eval path RETRIES
and the fallback path PARKS on identical data. Which runs depends on whether the eval consumer
was configured.

**Files:**
- Create: `packages/vera_core/src/vera_core/services/verification.py`
- Modify: `apps/control_plane/src/control_plane/post_call.py:50-100, 169`
- Modify: `apps/control_plane/src/control_plane/pipeline_sweeper.py:238, 269`
- Modify: `apps/control_plane/src/control_plane/worker_events.py:830`
- Modify: `apps/control_plane/src/control_plane/api/v1/calls.py:842`
- Test: `tests/unit/control_plane/test_post_call.py`, `tests/unit/services/test_verification.py` (create)

**Interfaces:**
- Consumes: `satisfied_required_fraction`, `load_field_status`,
  `load_authoritative_call_ids`, `current_values_by_path`, `is_v2`, `FormSchemaDoc`.
- Produces:
  `async def load_verified_fraction(session: AsyncSession, form: PatientForm, *, floor: int) -> float | None`
  — the 0.0–1.0 fraction, or `None` for a legacy v1 schema (whose document declares no
  reference-number field), so the caller falls back to its previous gate. Also
  `resolve_ai_processing(..., review_floor: int = REVIEW_CONFIDENCE_FLOOR)` and
  `sweep_stuck_ai_processing(..., review_floor: int = REVIEW_CONFIDENCE_FLOOR)`.

It fetches its own `SchemaVersion` deliberately: `post_call.py` then adds **zero** new queries, so
`test_post_call.py`'s `_FakeSession` entity routing is untouched and the unit test monkeypatches
one symbol.

- [ ] **Step 1: Write the failing helper test**

Create `tests/unit/services/test_verification.py`. It needs a session; use the same fake-session
pattern `tests/unit/control_plane/test_post_call.py` establishes (read it first) or an integration
fixture if this module's neighbours use one — do not invent a third seam.

```python
async def test_returns_none_for_a_legacy_v1_schema() -> None:
    """v1 declares no rep_call_reference_number_field, so there is nothing to be authoritative
    ABOUT — the caller must fall back rather than read 0.0 as 'nothing verified'."""
    assert await load_verified_fraction(_session(V1_SCHEMA), _form(), floor=70) is None


async def test_a_call_with_no_reference_number_verifies_nothing() -> None:
    """Spec S3: the same answers read completion 100% and verified 0%."""
    fraction = await load_verified_fraction(
        _session(IBV_STANDARD_V2, answers=_all_askable_answered(authoritative=False)),
        _form(), floor=70,
    )
    assert fraction == 0.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `just test tests/unit/services/test_verification.py -v`
Expected: FAIL — `ModuleNotFoundError: vera_core.services.verification`

- [ ] **Step 3: Write the helper**

```python
"""The one place the verified fraction is computed from a live session.

`verified_pct` is the fraction of required, applicable, collectable leaves an AUTHORITATIVE
call confirmed. Both retry gates read it through here, so they cannot drift onto different
populations (spec E3). Values are PHI — passed to the pure helper, never logged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.forms.conditions import is_v2
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.review import satisfied_required_fraction
from vera_core.models import PatientForm, SchemaVersion
from vera_core.services.field_answers import current_values_by_path
from vera_core.services.field_status import load_authoritative_call_ids, load_field_status


async def load_verified_fraction(
    session: AsyncSession, form: PatientForm, *, floor: int
) -> float | None:
    """`verified_pct / 100` for *form*, or `None` for a legacy v1 schema — which declares no
    reference-number field, so "authoritative" is undefined and the caller must fall back to
    its previous gate rather than read 0.0 as "nothing verified"."""
    schema_json: Mapping[str, Any] = (
        await session.execute(
            select(SchemaVersion.schema_json).where(SchemaVersion.id == form.schema_version_id)
        )
    ).scalar_one()
    if not is_v2(schema_json):
        return None
    doc = FormSchemaDoc.model_validate(schema_json)
    status_by_path = await load_field_status(session, form.id)
    authoritative = await load_authoritative_call_ids(
        session, form.id, reference_field=doc.rep_call_reference_number_field
    )
    values = await current_values_by_path(session, form.id)
    return satisfied_required_fraction(
        status_by_path,
        schema_json,
        floor=floor,
        values=values,
        authoritative_calls=authoritative,
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `just test tests/unit/services/test_verification.py -v`
Expected: PASS

- [ ] **Step 5: Write the failing gate test**

Add to `tests/unit/control_plane/test_post_call.py`, monkeypatching
`post_call.load_verified_fraction` so the decision is tested without touching `_FakeSession`:

```python
async def test_the_fallback_gate_reads_the_verified_fraction_not_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec S3/S4: a call can read completion 100% and verified 0%. The fallback path must
    decide on the same number the eval path does, or which consumer closed the call decides
    whether the payer is redialled."""
    async def _fake(*_a: object, **_kw: object) -> float:
        return 0.0

    monkeypatch.setattr(post_call, "load_verified_fraction", _fake)
    form = _form(status=FormStatus.AI_PROCESSING, completion_pct=100)
    tenant = _tenant(retry_fill_threshold=0.80, auto_retry_enabled=True, max_retries=5)
    requeued = await resolve_ai_processing(
        _SM, _audit(), _ref(), trigger="call.ended", auto_retry_enabled=True, review_floor=70
    )
    assert requeued is True
    assert form.status == FormStatus.IN_QUEUE.value


async def test_a_legacy_v1_form_falls_back_to_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None means 'authoritative is undefined here', not 'nothing verified'."""
    async def _fake(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(post_call, "load_verified_fraction", _fake)
    form = _form(status=FormStatus.AI_PROCESSING, completion_pct=100)
    _tenant(retry_fill_threshold=0.80, auto_retry_enabled=True, max_retries=5)
    requeued = await resolve_ai_processing(
        _SM, _audit(), _ref(), trigger="call.ended", auto_retry_enabled=True, review_floor=70
    )
    # completion 100 >= 80, so no low_fill and no redial — the v1 fallback still works.
    assert requeued is False
    assert form.status == FormStatus.EXCEPTION_REVIEW.value
```

Match the module's existing `_form` / `_tenant` / `_audit` / `_ref` helpers rather than adding
new ones.

- [ ] **Step 6: Run to verify it fails**

Run: `just test tests/unit/control_plane/test_post_call.py -k "verified_fraction or legacy_v1" -v`
Expected: FAIL — `resolve_ai_processing() got an unexpected keyword argument 'review_floor'`

- [ ] **Step 7: Change the gate**

In `post_call.py`, import `load_verified_fraction` and `REVIEW_CONFIDENCE_FLOOR`, add
`review_floor: int = REVIEW_CONFIDENCE_FLOOR` to `resolve_ai_processing` and
`sweep_stuck_ai_processing` (forwarding it at `:169`), and replace the `low_fill` line:

```python
        # ONE gate, ONE number: the eval path compares the verified fraction against this same
        # threshold (post_call_eval), so reading completion_pct here made the decision depend on
        # which consumer closed the call. Computed fresh — the stored verified_pct column is a
        # display value that `recompute_form_projection` deliberately does not maintain.
        threshold = float(tenant.retry_fill_threshold)
        fraction = await load_verified_fraction(session, form, floor=review_floor)
        low_fill = (
            fraction < threshold
            if fraction is not None
            else float(form.completion_pct) < threshold * 100
        )
```

Then update the module docstring's first bullet: it says "completion below the tenant's
``retry_fill_threshold``" and now means the verified fraction. Also drop the stale "today nothing
raises ``completion_pct`` between calls" clause if the surrounding sentence no longer parses —
read it and rewrite the bullet as one accurate sentence rather than patching words.

- [ ] **Step 8: Pass the floor at the three external call sites**

`pipeline_sweeper.py:238` and `:269` use `self._review_floor` from Task 1's constructor
parameter. `worker_events.py:830` and `api/v1/calls.py:842` pass
`settings.post_call_review_floor`.

- [ ] **Step 9: Run the tests**

Run: `just test tests/unit/control_plane/test_post_call.py tests/unit/services/test_verification.py -v`
Expected: PASS

- [ ] **Step 10: Record the mutation**

Revert Step 7's `low_fill` to `float(form.completion_pct) < threshold * 100` →
`test_the_fallback_gate_reads_the_verified_fraction_not_completion` must FAIL. Then separately
make `load_verified_fraction` return `0.0` instead of `None` for v1 →
`test_a_legacy_v1_form_falls_back_to_completion` must FAIL. Restore both.

- [ ] **Step 11: Commit**

```bash
git add packages/vera_core/src/vera_core/services/verification.py \
        apps/control_plane/src/control_plane/post_call.py \
        apps/control_plane/src/control_plane/pipeline_sweeper.py \
        apps/control_plane/src/control_plane/worker_events.py \
        apps/control_plane/src/control_plane/api/v1/calls.py \
        tests/unit/control_plane/test_post_call.py \
        tests/unit/services/test_verification.py
git commit -m "fix(post-call): one retry gate, one number

Both paths now compare the verified fraction against retry_fill_threshold. The
fallback path read completion_pct, so at T=0.90 a form at completion 100% /
verified 88% retried on the eval path and parked on the fallback path -- the
decision depended on whether the eval consumer was configured. Computed fresh;
the stored verified_pct column stays a display value."
```

---

### Task 6: The detail endpoint exposes the retry decision

Spec E4: `verified_pct` is computed, persisted, gates the decision, and appears nowhere in the
API. `review_reason` is on the summary only, so the worklist chip shows it and the review modal
does not. `retry_count` / `max_retries` are absent entirely. Without these the frontend cannot
explain any retry decision however well designed — and they carry S3, where a form reads
`READY_FOR_REVIEW` at 0% verified.

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/patient_forms.py:598-616, 780-816`
- Test: `tests/integration/control_plane/test_form_provenance.py` (it already owns the
  form-detail assertions — `test_detail_carries_provenance_for_ai_fields` at `:267` — and has a
  `_get_detail` helper)

**Interfaces:**
- Consumes: `AppSettings` (already a dependency type in this module, `:409`, `:1495`), `Tenant`
  (already imported, `:94`).
- Produces: five new `PatientFormDetail` fields —
  `verified_pct: float`, `review_reason: str | None`, `retry_count: int`, `max_retries: int`,
  `review_floor: int`. The frontend plan's F0/F4/B8 consume all five.

- [ ] **Step 1: Write the failing test**

```python
async def test_detail_exposes_the_retry_decision_inputs(...) -> None:
    """A reviewer cannot be shown WHY a form parked without these. verified_pct in particular
    is the number the gate reads and never left the database before (spec E4)."""
    body = await _get_detail(client, form_id)
    assert body["verified_pct"] == pytest.approx(0.0)
    assert body["review_reason"] == ReviewReason.FILL_THRESHOLD_MET.value
    assert body["retry_count"] == 2
    assert body["max_retries"] == 5
    assert body["review_floor"] == 70
```

Build the fixture on a form whose `verified_pct` differs from its `completion_pct` — otherwise
the assertion passes with `verified_pct=float(form.completion_pct)` wired in by mistake, which is
exactly the review §11 vacuity pattern.

- [ ] **Step 2: Run to verify it fails**

Run: `just test tests/integration/control_plane/test_form_provenance.py -k retry_decision_inputs -v`
Expected: FAIL — `KeyError: 'verified_pct'`

- [ ] **Step 3: Add the model fields**

```python
    completion_pct: float
    # The retry gate's own number: the fraction of required, applicable, collectable leaves an
    # AUTHORITATIVE call confirmed. Diverges sharply from completion_pct — a call that answered
    # everything but captured no reference number reads 100% complete and 0% verified.
    verified_pct: float
    # Why the form parked (vera_core ReviewReason). On the worklist row too; here so the review
    # modal can say it where the reviewer actually decides.
    review_reason: str | None
    # Retry budget: how many auto-redials this enqueue episode has consumed, and the tenant cap.
    retry_count: int
    max_retries: int
    # The confidence floor `is_call_confirmed` applies (settings.post_call_review_floor). The UI
    # renders confidence scores, and their meaning is defined entirely by this boundary.
    review_floor: int
```

- [ ] **Step 4: Populate them**

Add `settings: AppSettings` to the handler's parameters, load the tenant for its cap, and pass
the five fields:

```python
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one()
```

```python
        completion_pct=float(form.completion_pct),
        verified_pct=float(form.verified_pct),
        review_reason=form.review_reason,
        retry_count=form.retry_count,
        max_retries=tenant.max_retries,
        review_floor=settings.post_call_review_floor,
```

The endpoint already audits this response as a PHI disclosure; these five are non-PHI and need no
change to the audit detail.

- [ ] **Step 5: Run to verify it passes**

Run: `just test tests/integration/control_plane/test_form_provenance.py -v`
Expected: PASS

- [ ] **Step 6: Record the mutation**

Change `verified_pct=float(form.verified_pct)` to `float(form.completion_pct)` → the Step 1 test
must FAIL. If it passes, the fixture's two numbers are equal and the test proves nothing — rebuild
the fixture. Restore.

- [ ] **Step 7: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/patient_forms.py tests/
git commit -m "feat(patient-forms): expose the retry decision on the form detail

verified_pct gates the retry and never left the database; review_reason was on
the worklist row only; retry_count, max_retries and review_floor were absent.
Without these no review UI can explain why a form parked or retried."
```

---

## Final gate

- [ ] **Run mypy explicitly on every changed file**

Run: `git diff --name-only <merge-base>...HEAD | grep '\.py$' | xargs uv run mypy`
Expected: clean. `just check`'s mypy step covers the configured tree, but naming your changed
files catches an unannotated test helper before the gate does.

- [ ] **Boot the two background loops**

Tasks 1 and 5 change `PipelineSweeper`'s and `WorkerEventConsumer`'s constructors, and pytest
fakes never exercise a real BLOCK read or an idle tick. Per the repo rule:

Run: `just up`, then `just api` (with `LOCAL_KMS_MASTER_KEY` and `VERA_LIVEKIT_URL` set so the
worker-event consumer actually starts), and watch it idle through **at least two sweeper
windows**. Expected: no traceback, no back-off log spam, and the sweeper's periodic pass logs
normally. A `redis.exceptions.TimeoutError` escaping an idle `xreadgroup` means the new
constructor argument was threaded into the wrong place — the idle raise is expected and must stay
caught (see the `RedisCallStreamStore.read` handling).

- [ ] **Run the full backend gate**

Run: `just check` from `vera-backend/`, **verbatim** (~3 min; Langfuse must be stopped — review
§9.1).
Expected: no regressions against the branch baseline of `2666 passed, 3 skipped, 21 deselected,
1 xfailed`. Tasks 4 and 6 add tests, so the count rises; a *drop* means something was deselected
or silently skipped.

- [ ] **Run `/simplify` over the change, then re-run `just check`.**

Per the repo-wide rule in `CLAUDE.md`, this is part of "done", not an optional polish pass.

- [ ] **Live merge gate — one focused retry.**

Tasks 4 and 5 change which forms redial live payers, so `just check` is not sufficient.
`just seed-retry-form` then `just arm-retry-form`; bring Langfuse up with the **`langfuse-adc`
skill's command** (never `just langfuse-up`, and note `just langfuse-down` is not profile-scoped
— it tears down postgres too; recover with `just up`). Take one focused retry, then confirm:

1. the attempt timeline labels it `retry` with a `CallLineage` row (Task 2) — note that
   `arm-retry-form` preserves `retry_count=1`, so to exercise Task 2's actual scenario you must
   requeue through the **operator** surface (`PUT /patient-forms/{id}/status`, `manual=True`),
   which resets the budget. Review §15.1 explicitly did not exercise this;
2. the gate decided from the verified fraction (Task 5) — check `audit_log` for the
   `form.status_change` reason;
3. `verified_pct` and `review_reason` appear on `GET /patient-forms/{id}` (Task 6).

Stop Langfuse afterwards.

- [ ] **Known-good failure:** if
  `test_admin.py::test_invite_records_inviter_and_role_grant_provenance` fails, that is test-DB
  residue, not this change: `DROP DATABASE vera_retry_call_fix_test WITH (FORCE)` and the session
  fixture rebuilds it.
