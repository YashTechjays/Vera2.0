# Plan C — question-tree narrowing on a focused retry (P7)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a focused retry *say* only the questions it is focused on. Today it narrows what the
plan tracks and not one word of what the agent asks.

**Architecture:** `focus_call_plan` gains the doc and the answer map, narrows each task's
`panels` with `focus_questions(..., explode=True)` — the same primitive and flag the gap pass
already uses — derives `fields` from the kept questions' target paths, and re-renders `prompt` from
the narrowed tree using the reassembly the worker already pins.

**Tech Stack:** Python 3.12, pydantic v2, pytest, ruff, mypy `--strict`.

**Spec:** `vera-backend/docs/superpowers/specs/2026-08-21-retry-call-scoping-design.md`

## Global Constraints

- Every command runs from `vera-backend/`.
- **`focus_call_plan` stays pure** — it returns a copy and never mutates the staged plan.
- **The payer rep must never be told a prior call happened.** Narrowing is done by removing
  questions, never by adding retry language.
- **The reassembly invariant:** `"\n\n".join(lead_in, render_panels(panels), trailing) == prompt`,
  pinned by `TestPanelsMatchThePrompt` in `tests/unit/forms/test_call_plan.py:404`. The compiled
  prompt carries **no** completeness block — `PlanTaskAgent._assembled_block` inserts
  `_completeness_block(panels)` at task entry from whatever panels it holds, so a narrowed plan gets
  a correctly-counted completeness rule for free. Do not try to render one here;
  `_completeness_block` is worker-side and `vera_core/forms/` must stay DB-free and worker-free.
- **`fields` and `panels` must narrow to the SAME set.** This is the invariant today's code breaks
  and the reason for the bug. A rendered question whose paths are absent from `task.fields` is
  invisible to `owed_now` (which joins questions against `by_path = {f.path for f in task.fields}`),
  so the `task_complete` refusal and the gap pass would never track it.
- `on_file_values` stays cleared: it drives read-back confirmations, precisely the "re-verify
  everything" a focused retry must avoid.
- Never log a field value.
- Depends on Plan A and Plan B. `focus_paths` must exist and `bookend_paths` must be gone.
- `just check` verbatim, then `/simplify`, then `just check` again, then the eval harness, then a
  live browser-callee retry. **Spoken behaviour is not verified by pytest** — the assertions here
  are on strings; the defect lives in the audio.

---

## File Structure

- **Modify** `packages/vera_core/src/vera_core/forms/call_plan.py` — `focus_call_plan` (line 437)
- **Modify** `packages/vera_core/src/vera_core/services/queue_dispatcher.py` — the one call site
- **Test** `tests/unit/forms/test_call_plan.py` — extend `TestFocusCallPlan` and
  `TestPanelsMatchThePrompt`

**Interfaces:**

- Consumes: `focus_paths(...)` (Plan B), `keep_questions` / `focus_questions` / `iter_questions`
  (`forms/question_plan.py`, `forms/call_plan.py`), `render_panels` (`forms/prompting.py`).
- Produces the **signature change**:

```python
def focus_call_plan(
    doc: FormSchemaDoc,
    plan: CallPlan,
    paths: Collection[str],
    *,
    answers: Mapping[str, Any],
) -> CallPlan: ...
```

`doc` is a new first parameter and `answers` a new keyword. `queue_dispatcher.py` is the only
production caller; both are already in scope there.

---

### Task 1: narrow `panels`, derive `fields` from them, re-render `prompt`

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/call_plan.py:437`
- Test: `tests/unit/forms/test_call_plan.py`

**Interfaces:**
- Consumes: `focus_questions`, `iter_questions`, `render_panels`.
- Produces: the new `focus_call_plan` signature and behaviour.

- [ ] **Step 1: Write the failing tests**

Extend `TestFocusCallPlan` in `tests/unit/forms/test_call_plan.py`. It already has `PLAN` and an
`_all_paths` helper — reuse them, and read the class first so the new tests match its fixtures.

```python
    def test_narrows_the_question_tree_not_just_the_fields(self) -> None:
        """P7: `focus_call_plan` copied `fields` and left `panels` and `prompt` untouched, so a
        focused retry spoke every question of every surviving task."""
        target = "sections.deductibles.individual.total"
        focused = focus_call_plan(DOC, PLAN, {target}, answers={})
        task = plan_task(focused, "financial")
        spoken = {p for q in iter_questions(task.panels) for p in q.target_paths}
        assert target in spoken
        assert "sections.out_of_pocket.individual.total" not in spoken

    def test_re_renders_the_prompt_from_the_narrowed_tree(self) -> None:
        target = "sections.deductibles.individual.total"
        full = plan_task(PLAN, "financial")
        focused = plan_task(focus_call_plan(DOC, PLAN, {target}, answers={}), "financial")
        assert focused.prompt != full.prompt
        assert len(focused.prompt) < len(full.prompt)

    def test_the_reassembly_invariant_survives_narrowing(self) -> None:
        """`PlanTaskAgent._assembled_block` rebuilds the block from these three pieces; if they
        stop agreeing, a narrowed task says something the plan does not carry."""
        focused = focus_call_plan(DOC, PLAN, {"sections.deductibles.individual.total"}, answers={})
        for task in focused.tasks:
            if not task.panels:
                continue
            parts = (task.lead_in, render_panels(task.panels), task.trailing)
            assert "\n\n".join(p for p in parts if p) == task.prompt, task.task_key

    def test_fields_and_panels_narrow_to_the_same_set(self) -> None:
        """`owed_now` joins questions against `task.fields`; a question whose fields are missing is
        invisible to the refusal and the gap pass."""
        focused = focus_call_plan(DOC, PLAN, {"sections.deductibles.individual.total"}, answers={})
        for task in focused.tasks:
            if not task.panels:
                continue
            spoken = {p for q in iter_questions(task.panels) for p in q.target_paths}
            tracked = {f.path for f in task.fields}
            assert tracked <= spoken, task.task_key

    def test_explode_pulls_in_the_follow_ups_of_an_unanswered_gate_parent(self) -> None:
        """The failure `focus_questions(explode=True)` exists to prevent: the agent asks whether
        infertility treatment is covered, the rep says yes, and — because the Observer extracts in
        a detached pass — nothing is owed yet, so an agent with no sanctioned next question
        invents one."""
        parent = "sections.infertility_treatment.infertility_tx_covered"
        focused = focus_call_plan(DOC, PLAN, {parent}, answers={})
        task = plan_task(focused, "infertility_coverage")
        spoken = {p for q in iter_questions(task.panels) for p in q.target_paths}
        assert parent in spoken
        assert len(spoken) > 1, "the parent's dependents were not pre-loaded"

    def test_an_already_answered_follow_up_is_not_pre_loaded(self) -> None:
        """`_exploded` adds only targets with nothing on file — adding answered ones would make a
        partly-answered fan-out look wholly owed."""
        parent = "sections.infertility_treatment.infertility_tx_covered"
        answered = {
            p: "Yes" for p, _leaf in DOC.leaf_items()
            if p.startswith("sections.infertility_treatment.") and p != parent
        }
        focused = focus_call_plan(DOC, PLAN, {parent}, answers=answered)
        task = plan_task(focused, "infertility_coverage")
        spoken = {p for q in iter_questions(task.panels) for p in q.target_paths}
        assert spoken == {parent}

    def test_still_clears_on_file_values_and_keeps_the_session(self) -> None:
        focused = focus_call_plan(DOC, PLAN, {"sections.deductibles.individual.total"}, answers={})
        assert focused.on_file_values is None
        assert focused.session == PLAN.session
        assert focused.stt_key_terms == PLAN.stt_key_terms

    def test_original_plan_not_mutated(self) -> None:
        before = len(self._all_paths(PLAN))
        focus_call_plan(DOC, PLAN, {self._all_paths(PLAN)[0]}, answers={})
        assert len(self._all_paths(PLAN)) == before
```

`DOC` is `build_ibv_standard()`, memoized at module scope beside `PLAN` if it is not already there.
`plan_task(plan, key)` already exists in this module (used at line 479); `iter_questions` and
`render_panels` need importing.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/forms/test_call_plan.py -k FocusCallPlan -v`
Expected: FAIL — `focus_call_plan()` takes 2 positional arguments. Then, once the signature is in
place, `test_narrows_the_question_tree_not_just_the_fields` fails on the unnarrowed tree. That
second failure is the actual defect; confirm you see it before fixing.

- [ ] **Step 3: Rewrite `focus_call_plan`**

```python
def focus_call_plan(
    doc: FormSchemaDoc,
    plan: CallPlan,
    paths: Collection[str],
    *,
    answers: Mapping[str, Any],
) -> CallPlan:
    """Narrow a fused plan to a FOCUSED retry: keep only what `paths` asks for, in both the tracked
    fields AND the spoken question tree. The agent then asks ONLY the still-missing data points —
    with no announcement that this is a retry (the payer rep must never be told a prior call
    happened).

    `focus_questions(..., explode=True)` does the tree narrowing — the same primitive and flag the
    gap pass uses — so a question whose gate reads a path being asked comes along carrying its own
    "Ask only if …" prose. Without that, a retry whose only gap is a gate parent asks one question
    and has nothing sanctioned to follow the answer with.

    `fields` is derived from the kept questions rather than from `paths`, because `explode` grows
    the set: `owed_now` joins questions against `task.fields`, so a rendered question with no
    matching field is invisible to the `task_complete` refusal and the gap pass.

    `prompt` is re-rendered from the narrowed tree using the same three-piece reassembly
    `PlanTaskAgent._assembled_block` pins (`TestPanelsMatchThePrompt`). The completeness rule is
    deliberately NOT rendered here — the worker inserts it at task entry from whatever panels it
    holds, so it counts the narrowed list automatically.

    Persona/goal/base_instructions and the ``known_information`` background block are preserved
    (context the agent needs), but ``on_file_values`` is cleared — that block drives read-back
    confirmations of already-known values, exactly the "re-verify everything" behavior a focused
    retry must avoid. A confirm-role field kept in *paths* degrades to a plain ask, which is the
    intended re-collect.
    """
    keep = set(paths)
    shared = plan.shared_conditions
    tasks: list[PlanTask] = []
    for task in plan.tasks:
        if not task.panels:
            # The compiler shipped no tree for this task: it carries only speech, so there is
            # nothing to narrow and nothing to count. Keep it whole.
            tasks.append(task)
            continue
        panels = focus_questions(task, keep, answers, shared, explode=True)
        spoken = {path for question in iter_questions(panels) for path in question.target_paths}
        fields = [field for field in task.fields if field.path in spoken]
        if not fields:
            continue
        parts = (task.lead_in, render_panels(panels), task.trailing)
        tasks.append(
            task.model_copy(
                update={
                    "panels": panels,
                    "fields": fields,
                    "prompt": "\n\n".join(part for part in parts if part),
                }
            )
        )
    return plan.model_copy(update={"tasks": tasks, "on_file_values": None})
```

Add `iter_questions` and `render_panels` to `call_plan.py`'s imports if they are not already there
(`focus_questions` and `keep_questions` are in this module / `question_plan`; `render_panels` is in
`forms/prompting.py` — check for an import cycle, and if `prompting` imports `call_plan`, move the
render into `prompting` and call it from here instead).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/forms/test_call_plan.py -v`
Expected: PASS, including the pre-existing `TestPanelsMatchThePrompt` and every other
`TestFocusCallPlan` test. The two that asserted only field-narrowing still hold — narrowing fields
is still part of what this does.

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/call_plan.py tests/unit/forms/test_call_plan.py
git commit -m "fix(retry): narrow the spoken question tree on a focused retry (P7)"
```

---

### Task 2: wire the dispatcher and measure the narrowing

**Files:**
- Modify: `packages/vera_core/src/vera_core/services/queue_dispatcher.py` — the `focus_call_plan`
  call site
- Test: `tests/integration/test_retry_dispatch.py` (extends Plan B's module)

**Interfaces:**
- Consumes: the new `focus_call_plan` signature from Task 1.
- Produces: a staged retry plan whose prompts are narrowed.

- [ ] **Step 1: Write the failing test**

```python
async def test_the_staged_retry_prompt_is_narrowed_not_just_its_fields(...) -> None:
    """The measurement from the spec: 9 tasks -> 4, fields 182 -> 45, but questions 25 -> 25 and
    the prompt byte-identical. Only the last two are what the rep hears."""
    form = await seed_form_with_authoritative_call(...)
    await try_dispatch(session, tenant_id, livekit, kms, audit, plan_service=plans)
    staged = await plans.get(room_name_for_call(tenant_id, (await only_call(form)).id))

    full = compile_and_fuse_the_same_form(...)          # the unfocused comparison
    for task in staged.tasks:
        counterpart = plan_task(full, task.task_key)
        assert len(task.prompt) < len(counterpart.prompt), task.task_key
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/integration/test_retry_dispatch.py -v`
Expected: FAIL — prompts are byte-identical, because the dispatcher still calls the two-argument
form.

- [ ] **Step 3: Update the call site**

`queue_dispatcher.py`, inside the focus block Plan B rewrote:

```python
                if focus:
                    staged_plan = (
                        focus_call_plan(doc, plan, focus, answers=values),
                        plan_prompt_version_id,
                    )
```

`doc` and `values` are both already in scope — `doc` from the schema-version resolution a few lines
above, `values` from the `current_values_by_path` read the block already performs.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_retry_dispatch.py tests/unit/forms/ -v`
Expected: PASS.

- [ ] **Step 5: Measure it against the seeded scenario**

This is the spec's table, and the numbers it must move.

Run: `just seed-retry-form`, then:
```bash
uv run python - <<'PY'
import asyncio, json
from sqlalchemy import select
from vera_core.config import get_settings
from vera_core.db import create_engine, create_sessionmaker
from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.prompting import PromptDocument, numbered_questions
from vera_core.forms.call_plan import compile_call_plan, focus_call_plan, PrefillFuser
from vera_core.forms.review import focus_paths
from vera_core.models import PatientForm, SchemaVersion, PromptVersion
from vera_core.models.enums import VersionStatus
from vera_core.services.field_status import load_field_status, load_authoritative_call_ids
from vera_core.services.field_answers import current_values_by_path

async def main() -> None:
    s_ = get_settings(); eng = create_engine(s_)
    async with create_sessionmaker(eng)() as s:
        form = (await s.execute(select(PatientForm).where(
            PatientForm.chart_number == "TEST-SEED-RETRY"))).scalar_one()
        ver = (await s.execute(select(SchemaVersion).where(
            SchemaVersion.id == form.schema_version_id))).scalar_one()
        doc = FormSchemaDoc.model_validate(ver.schema_json)
        pv = (await s.execute(select(PromptVersion).where(
            PromptVersion.schema_version_id == ver.id,
            PromptVersion.status == VersionStatus.PUBLISHED.value).order_by(
            PromptVersion.created_at.desc()).limit(1))).scalar_one_or_none()
        plan = compile_call_plan(doc, PromptDocument.model_validate(pv.composite_json) if pv else None,
                                schema_version_id=ver.id, prompt_version_id=pv.id if pv else None)
        values = await current_values_by_path(s, form.id)
        fused = PrefillFuser(doc, plan).fuse(values, current_year=2026)
        st = await load_field_status(s, form.id)
        auth = await load_authoritative_call_ids(
            s, form.id, reference_field=doc.rep_call_reference_number_field)
    focus = focus_paths(doc, st, ver.schema_json, floor=s_.post_call_review_floor,
                        values=values, authoritative_calls=auth)
    focused = focus_call_plan(doc, fused, focus, answers=values)
    full = {t.task_key: t for t in fused.tasks}
    print(f"{'task':24s} {'FULL q':>7s} {'FOC q':>6s} {'FULL chars':>11s} {'FOC chars':>10s}")
    for t in focused.tasks:
        f = full[t.task_key]
        print(f"{t.task_key:24s} {numbered_questions(f.panels):7d} "
              f"{numbered_questions(t.panels):6d} {len(f.prompt):11d} {len(t.prompt):10d}")
    await eng.dispose()
asyncio.run(main())
PY
```
Expected: the `FOC q` column is strictly below `FULL q` wherever the task was narrowed —
`diagnostic_coverage` around 4 → 1 and `financial` around 18 → 10 — and `FOC chars` is well below
`FULL chars` on every row. If any row is byte-identical, the tree for that task was not narrowed.

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/services/queue_dispatcher.py \
        tests/integration/test_retry_dispatch.py
git commit -m "fix(dispatch): stage the narrowed retry plan"
```

---

### Task 3: verify the spoken behaviour

**Files:** none — this task is verification, and it is not optional. A change to what the agent says
is not verified by pytest: the assertions above are on strings, and the defect this plan fixes lived
in the audio for two releases behind green tests.

- [ ] **Step 1: Run the full gate**

Run: `just check`
Expected: PASS. Verbatim — `ruff check` and `ruff format --check` are different gates.

- [ ] **Step 2: Run `/simplify`, then the gate again**

Run `/simplify` on the change, then `just check`.
Expected: PASS on the exact tree to be pushed.

- [ ] **Step 3: Run the eval harness**

```bash
VERA_EVALS_FULL=1 VERA_EVALS_ENABLED=1 uv run pytest apps/agent_worker/tests/evals -m evals -s -rs
```
`-m evals` is required — without it you get the 20 LLM-free tests and NO simulations, which looks
like a clean pass. Confirm real scenarios ran by the `===== <scenario>: … =====` banners.

Scenario 2 is the focused one and is what surfaced P7. Expected: VERA asks only the kept questions,
and the false `scope_discipline` failure recorded as H3 in
`2026-07-30-call-flow-eval-findings-remediation.md` clears — that failure was an artifact of
`SCOPE_DISCIPLINE` telling the agent the list was complete while the dropped questions appeared in
neither the list nor the exclusions.

**Do not overclaim from a green run.** There is no STT, no real DTMF, and extraction settles between
turns, so rules fire more reliably than on a real call.

- [ ] **Step 4: Take a live browser-callee retry**

```bash
just seed-retry-form                                  # a prior authoritative call, 3 sections owed
VERA_BROWSER_CALLEE_TRANSPORT=true just api           # terminal 1
VERA_BROWSER_CALLEE_TRANSPORT=true just worker        # terminal 2
cd ../vera-frontend && VITE_BROWSER_CALLEE_TRANSPORT=true npm run dev
just arm-retry-form                                   # then join from Live Monitoring
```

Two constraints that bite: ~60s from enqueue to joining (`_SPEAKER_TIMEOUT_S`), and one tab per call
(the identity is `caller-{user_id}` with no session suffix, so a second tab evicts the first).

**What to listen for:**
- the greeting and the recording/identity disclosure still happen (they survive only because
  `patient_verification.is_insurance_active` is `collected_per="call"`, per Plan A);
- VERA asks the deductible / out-of-pocket / lifetime questions and the one rejected labs panel —
  and **nothing else**;
- she asks for the rep's name and a call reference number at the end;
- she never mentions a previous call.

Then confirm against the trace: `langfuse.session.id` is the room name, and the per-task system
prompts should show numbered question counts far below the 16 and 41 recorded in the spec.

- [ ] **Step 5: Record the result**

If the call is good, note the trace id and the per-task counts in the PR body — that is the evidence
this plan worked, and pytest cannot supply it. If it is not, **stop and diagnose**; do not layer a
second fix on top (see `superpowers:systematic-debugging`).

---

## Verification

Plan C is done when:

- `just check` passes verbatim on the pushed tree.
- `TestPanelsMatchThePrompt` passes on a **focused** plan, not just a compiled one — the reassembly
  invariant is what stops a narrowed task saying something the plan does not carry.
- For every narrowed task, `{f.path for f in task.fields}` is a subset of the kept questions' target
  paths. Fields and panels agreeing is the invariant whose absence caused P7.
- The seeded scenario's measurement (Task 2 Step 5) shows `diagnostic_coverage` around 4 → 1 and
  `financial` around 18 → 10 spoken questions, with no row byte-identical.
- With only a gate parent owed, its dependents appear in the rendered list rather than leaving the
  agent one question and nothing sanctioned to follow it with.
- The eval harness ran real scenarios (banners present) and the focused scenario asks only kept
  questions.
- A live browser-callee retry sounded right, and its trace id is in the PR body.
