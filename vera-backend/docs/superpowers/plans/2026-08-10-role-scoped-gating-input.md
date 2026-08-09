# Role-Scoped Gating Input Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop an intake value from answering a question the call owes, so a schema `default` on an `ask`-role leaf can no longer delete every question gated behind it from the compiled prompt.

**Architecture:** `CallPlan.prefilled` is a role-blind `{path: value}` dump used as an answer snapshot by two consumers. Split the concept in two: a **pre-call baseline** that is role-scoped (`gating_seed` — an `ask` leaf is collected on the call, so a pre-call value for one is not an answer), and **what the call has collected**, which the Observer owns. The controller's working set becomes `baseline | call answers`, merged rather than replaced. The Observer keeps the raw map for its own dedup, `remaining` derivation and rule engine, renamed `_on_file` to say so.

**Tech Stack:** Python 3.12, pydantic v2, pytest, uv workspace (`vera_core` → `agent_worker`), ruff + mypy --strict.

**Spec:** `docs/superpowers/specs/2026-08-10-role-scoped-gating-input-design.md`

## Global Constraints

- Comments only where they explain what the code cannot — a constraint, a race, a deliberate trade-off. One line. No narration. Docstrings one sentence where practical (repo `CLAUDE.md`).
- **No `CallPlan` schema change.** `PlanFieldDescriptor.role` already exists. The plan is `extra="forbid"` and persists in Redis; a new field is a rolling-deploy hazard.
- Never log or trace a raw answer value — paths, counts and shapes only (repo `CLAUDE.md`, PHI bright lines).
- PEP 695 type params only. `asyncio` only — never `import anyio`.
- Run `just check` verbatim before claiming done — never a hand-picked subset. `ruff check` and `ruff format --check` are different gates.
- After implementation and before the final commit, run the `/simplify` skill on the change, then re-run `just check`.
- `just check` is currently red on two pre-existing `test_auth_audit_chain` failures unrelated to this work. Prove pre-existence against the merge-base before attributing anything to this branch.

## The trap this plan exists to avoid

Filtering the controller's **constructor seed alone does not work**, and the obvious unit tests do not catch it:

```python
observer.py:531      self._controller.update_answers(self._answers)   # the WHOLE map, intake included
plan_runtime.py:938  self._answers = dict(answers)                    # wholesale REPLACE
```

On a real call the Observer records answers from the first turn, so by the time `closing_admin` is entered the controller's filtered seed has been overwritten many times with an unfiltered map — `enrollment_required: "N/A"` back in place, question deleted again. A test that builds a controller and reads `excluded_fields` without ever calling `update_answers` passes anyway. **Task 2 Step 5 exists specifically to fail if this is reintroduced.**

---

### Task 1: Split the concept — pre-call baseline vs. what the call collected

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/call_plan.py` (add `gating_seed` after `owed_now`, which ends at line 190)
- Modify: `apps/agent_worker/src/agent_worker/plan_runtime.py` — import line 47, seed lines 711-714, `update_answers` lines 936-938
- Modify: `apps/agent_worker/src/agent_worker/observer.py` — line 360 and the 10 `_answers` references; `_record_locked` around line 530
- Test: `tests/unit/forms/test_call_plan.py`

**Interfaces:**
- Consumes: `CallPlan.prefilled: dict[str, Any]`, `PlanFieldDescriptor.role: Literal["ask", "confirm"]`, `PlanTask.fields: list[PlanFieldDescriptor]` — all already in `call_plan.py`.
- Produces:
  - `vera_core.forms.call_plan.gating_seed(plan: CallPlan) -> dict[str, Any]`
  - `PlanRunController.update_answers(answers: Mapping[str, Any]) -> None` — contract changes from *replace* to *merge onto the baseline*. Same name and arity, so no call site moves.
  - `ObserverManager._recorded: dict[str, Any]` — call-collected answers only, what gets pushed to the controller.

- [ ] **Step 1: Write the failing tests for `gating_seed`**

Append to `tests/unit/forms/test_call_plan.py`. `IBV` and `PLAN` already exist at module scope (lines 35-49); add `gating_seed` to the existing `from vera_core.forms.call_plan import (...)` block, which already imports `fuse_prefill`.

```python
class TestGatingSeed:
    """`ask` is collected ON the call, so a pre-call value for one must not settle a gate."""

    def _fused(self, values: dict[str, object]) -> CallPlan:
        return fuse_prefill(IBV, PLAN, values, current_year=2026)

    def test_ask_role_prefill_is_dropped(self) -> None:
        path = "sections.enrollment.enrollment_required"
        plan = self._fused({path: "N/A"})
        assert plan.prefilled[path] == "N/A"
        assert path not in gating_seed(plan)

    def test_confirm_role_prefill_survives(self) -> None:
        # On file to be read back — the member-ID pattern.
        path = "sections.insurance_information.policy_number"
        plan = self._fused({path: "ABC123"})
        assert gating_seed(plan)[path] == "ABC123"

    def test_context_role_prefill_survives(self) -> None:
        # No task collects it; it is what the clinic supplied.
        path = "sections.patient_information.spouse_gender"
        plan = self._fused({path: "Male"})
        assert gating_seed(plan)[path] == "Male"

    def test_prefilled_itself_is_not_mutated(self) -> None:
        path = "sections.enrollment.enrollment_required"
        plan = self._fused({path: "N/A"})
        gating_seed(plan)
        assert plan.prefilled == {path: "N/A"}

    def test_a_human_typed_ask_value_is_dropped_too(self) -> None:
        """Provenance is not consulted, only role: `field_answer.source` never reaches the
        worker, and the payer's representative is the authority on an ask leaf either way."""
        path = "sections.benefit_coverage.coverage_type"
        plan = self._fused({path: "Family"})
        assert path not in gating_seed(plan)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/forms/test_call_plan.py::TestGatingSeed -v`
Expected: FAIL at collection — `ImportError: cannot import name 'gating_seed'`.

- [ ] **Step 3: Add `gating_seed`**

In `packages/vera_core/src/vera_core/forms/call_plan.py`, directly after `owed_now` (before `_exclusive_notes`):

```python
def gating_seed(plan: CallPlan) -> dict[str, Any]:
    """The answers a gate may be judged against before the call has collected anything.

    An `ask`-role leaf is collected ON the call, so a value on file for one is a pre-call
    baseline and never an answer: letting it settle a gate deletes every question behind it
    from the compiled list, which is how the intake UI's `default` for `enrollment_required`
    removed the enrollment provider question from `closing_admin`. `confirm` stays — it is on
    file precisely to be read back — and a path no task collects is clinic-supplied context.

    Provenance is deliberately not consulted, only role: `field_answer.source` does not reach
    the worker, and an ask leaf's authority is the payer's representative either way."""
    asked = {field.path for task in plan.tasks for field in task.fields if field.role == "ask"}
    return {path: value for path, value in plan.prefilled.items() if path not in asked}
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/unit/forms/test_call_plan.py::TestGatingSeed -v`
Expected: 5 passed.

- [ ] **Step 5: Make the controller hold a baseline and MERGE onto it**

In `apps/agent_worker/src/agent_worker/plan_runtime.py`, extend the import at line 47:

```python
from vera_core.forms.call_plan import CallPlan, PlanFieldDescriptor, gating_seed, owed_now
```

Replace lines 711-714:

```python
        # Pre-call baseline for gate evaluation: the intake values a gate may legitimately be
        # judged against before the call collects anything. Role-scoped (`gating_seed`), so an
        # ask leaf's prefill can never settle a gate. Immutable for the run.
        self._baseline = gating_seed(plan)
        # Baseline + what the call has collected. Redis stays the cross-process truth.
        self._answers: dict[str, Any] = dict(self._baseline)
```

Replace `update_answers` (lines 936-938):

```python
    def update_answers(self, answers: Mapping[str, Any]) -> None:
        """The answers the CALL has collected, laid over the pre-call baseline.

        MERGED, never replaced. The Observer's own map is not role-scoped, so a wholesale
        replace would put every ask-role intake value back and re-arm the question deletion
        `gating_seed` exists to prevent — and it would do so invisibly, since the controller
        cannot tell a pre-call value from one the rep just gave."""
        self._answers = {**self._baseline, **answers}
```

`Mapping` is already imported at line 28.

- [ ] **Step 6: Make the Observer push only what the call collected**

In `apps/agent_worker/src/agent_worker/observer.py`, replace the comment and assignment at lines 358-360:

```python
        # Everything on file for this form — intake included, all roles. NOT the controller's
        # gate-evaluation set: three behaviours here need the intake values, namely the dedup in
        # `_record_locked`, `_derive_remaining_locked`'s "a prefilled remaining wins", and the
        # rule engine, whose terminate rules read ask-role paths a clinic may fill.
        self._on_file: dict[str, Any] = dict(plan.prefilled)
        # What THIS CALL collected, which is all the controller may gate on — see
        # `PlanRunController.update_answers`.
        self._recorded: dict[str, Any] = {}
```

Rename the remaining 9 `self._answers` references in the file to `self._on_file`, then change the two lines at 530-531 to:

```python
        self._on_file[answer.field_path] = answer.value
        self._recorded[answer.field_path] = answer.value
        self._controller.update_answers(self._recorded)
```

Update the 3 `_answers` references in `apps/agent_worker/tests/unit/test_observer.py` to `_on_file`.

- [ ] **Step 7: Run the worker and forms suites**

Run: `uv run pytest apps/agent_worker/tests/unit tests/unit/forms -q`
Expected: `777 passed, 1 xfailed`.

Every existing `update_answers` call site passes a call-answer map against a plan whose `prefilled` is empty (the fixtures build `CallPlan(...)` directly), so merge and replace are identical for them. If any test does fail here, it is asserting that `update_answers` CLEARS a prefilled value — read it before changing it, because that assertion is now wrong by design.

- [ ] **Step 8: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/call_plan.py \
        apps/agent_worker/src/agent_worker/plan_runtime.py \
        apps/agent_worker/src/agent_worker/observer.py \
        tests/unit/forms/test_call_plan.py \
        apps/agent_worker/tests/unit/test_observer.py
git commit -m "fix(worker): an ask-role intake value no longer settles a gate

CallPlan.prefilled is role-blind and both consumers used it as an answer
snapshot. The intake UI seeds every leaf default, so enrollment_required
arrived as \"N/A\", its gate was decided false at task entry, and the
enrollment provider question was deleted from the compiled list.

Split the concept: gating_seed() is the pre-call baseline, role-scoped so an
ask leaf's prefill can never settle a gate, and update_answers now MERGES the
call's own answers onto it instead of replacing the map. Seeding the
constructor alone would not have held — the Observer pushed its whole
unfiltered map on every recorded answer.

The Observer keeps that raw map, renamed _on_file: its dedup, its remaining
derivation and its terminate rules all require the intake values."
```

---

### Task 2: Pin both live bugs, the bounds, and the Observer path

**Files:**
- Test: `apps/agent_worker/tests/unit/test_plan_runtime.py`

**Interfaces:**
- Consumes: `gating_seed` (Task 1), the merge contract of `update_answers` (Task 1), `PlanRunController`, `controller.excluded_fields(i)` / `applicable_fields(i)` / `conditional_fields(i)` / `gap_fields(i)`, `compile_call_plan(doc, prompt_doc, *, schema_version_id, prompt_version_id)`, `fuse_prefill(doc, plan, values, *, current_year)`, the module's existing `_controller(plan) -> tuple[PlanRunController, FakeRunState]` helper (line 108).
- Produces: nothing consumed later.

Nothing currently pins any of this. Without these tests the fix regresses silently.

- [ ] **Step 1: Add the imports**

At the top of `apps/agent_worker/tests/unit/test_plan_runtime.py`, extend the existing `from vera_core.forms.call_plan import (...)` block with `fuse_prefill`, and add:

```python
from vera_core.forms.catalog.disease_only import build_disease_only
```

- [ ] **Step 2: Write the regression tests**

Append to the same file:

```python
def _fused_plan(build: Any, values: dict[str, Any]) -> CallPlan:
    doc = build()
    template = compile_call_plan(doc, None, schema_version_id=uuid4(), prompt_version_id=None)
    return fuse_prefill(doc, template, values, current_year=2026)


def _task_index(plan: CallPlan, task_key: str) -> int:
    return next(i for i, task in enumerate(plan.tasks) if task.task_key == task_key)


class TestAnIntakeDefaultNeverDeletesAQuestion:
    """The intake UI seeds every leaf `default`, so these arrive as real field_answer rows.
    Before `gating_seed` they settled their gate at task entry and the dependent question was
    dropped from the compiled list — recovered, if at all, by the end-of-call sweep."""

    def test_enrollment_provider_survives_the_defaulted_gate(self) -> None:
        plan = _fused_plan(
            build_ibv_standard, {"sections.enrollment.enrollment_required": "N/A"}
        )
        controller, _ = _controller(plan)
        excluded = {f.path for f in controller.excluded_fields(_task_index(plan, "closing_admin"))}
        assert "sections.enrollment.enrollment_provider_name" not in excluded
        assert "sections.enrollment.enrollment_provider_phone" not in excluded

    def test_renewal_date_survives_the_defaulted_gate(self) -> None:
        plan = _fused_plan(
            build_disease_only, {"sections.coverage_summary.benefit_year_type": "Calendar Year"}
        )
        controller, _ = _controller(plan)
        excluded = {f.path for f in controller.excluded_fields(_task_index(plan, "policy_basics"))}
        assert "sections.coverage_summary.renewal_date" not in excluded

    def test_the_gate_field_itself_becomes_owed(self) -> None:
        """`owed_now` requires `not has_value`, so the intake row also hid the gate question
        from the completion guard — the second half of the same failure."""
        plan = _fused_plan(
            build_ibv_standard, {"sections.enrollment.enrollment_required": "N/A"}
        )
        controller, _ = _controller(plan)
        owed = {f.path for f in controller.gap_fields(_task_index(plan, "closing_admin"))}
        assert "sections.enrollment.enrollment_required" in owed
```

- [ ] **Step 3: Write the bounds tests — what must NOT move**

```python
class TestTheBoundsOfTheGatingChange:
    """Both pass before the fix as well. They are the fence around it."""

    def test_an_earlier_tasks_answer_still_decides_a_later_gate(self) -> None:
        """The position half of `_settled` is untouched. `coverage_type` is collected in
        `insurance_basics` and gates 17 questions in `financial` / `male_partner`, so those
        stay excluded whether or not it was seeded — which is also what keeps the worker no
        LESS decisive than the compiler (`question_plan._entry_decided`)."""
        plan = _fused_plan(
            build_ibv_standard, {"sections.benefit_coverage.coverage_type": "Individual"}
        )
        controller, _ = _controller(plan)
        index = _task_index(plan, "male_partner")
        assert controller.applicable_fields(index) == []
        assert len(controller.excluded_fields(index)) == 9
        assert controller.conditional_fields(index) == []

    def test_a_context_prefill_still_decides_its_gate(self) -> None:
        """`context` is clinic-supplied background and stays authoritative — an absent spouse
        gender must still exclude the male-partner questions rather than ask about them."""
        plan = _fused_plan(
            build_ibv_standard,
            {
                "sections.benefit_coverage.coverage_type": "Family",
                "sections.patient_information.spouse_gender": "N/A",
            },
        )
        controller, _ = _controller(plan)
        assert len(controller.excluded_fields(_task_index(plan, "male_partner"))) == 9

    def test_a_confirm_prefill_still_settles_its_own_answered_check(self) -> None:
        """`confirm` values stay in the baseline, so a prefilled member ID is not re-owed."""
        path = "sections.insurance_information.policy_number"
        plan = _fused_plan(build_ibv_standard, {path: "ABC123"})
        controller, _ = _controller(plan)
        owed = {f.path for f in controller.gap_fields(_task_index(plan, "insurance_basics"))}
        assert path not in owed
```

- [ ] **Step 4: Verify the first three fail on the pre-fix code**

```bash
git stash push packages/vera_core/src/vera_core/forms/call_plan.py \
               apps/agent_worker/src/agent_worker/plan_runtime.py \
               apps/agent_worker/src/agent_worker/observer.py
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py \
  -k "TestAnIntakeDefaultNeverDeletesAQuestion or TestTheBoundsOfTheGatingChange" -v
git stash pop
```
Expected: the three in `TestAnIntakeDefaultNeverDeletesAQuestion` FAIL; all three in `TestTheBoundsOfTheGatingChange` PASS. A bounds test that fails here is a bug in the test, not evidence of a regression — it is meant to hold both ways.

> If `git stash pop` conflicts, recover with `git checkout stash@{0} -- <paths>` and confirm `git status --porcelain` matches the Task-1 state before continuing.

- [ ] **Step 5: Write the test that catches the trap this plan warns about**

This is the one that would have caught my first draft. It drives the real Observer→controller path instead of reading the constructor's state.

```python
class TestTheObserverCannotReArmTheDeletion:
    """`ObserverManager` pushes through `update_answers` on every recorded answer. If that
    replaced the controller's map with an unfiltered one — or if the Observer pushed its own
    `_on_file` rather than `_recorded` — the intake default would be back in place by the time
    `closing_admin` is entered, and the question would be deleted again on a real call while
    every constructor-level test stayed green."""

    def test_a_recorded_answer_does_not_restore_the_intake_default(self) -> None:
        plan = _fused_plan(
            build_ibv_standard, {"sections.enrollment.enrollment_required": "N/A"}
        )
        controller, _ = _controller(plan)
        # What the Observer sends after extracting one unrelated answer earlier in the call.
        controller.update_answers({"sections.insurance_representative.rep_name": "Pat"})
        excluded = {f.path for f in controller.excluded_fields(_task_index(plan, "closing_admin"))}
        assert "sections.enrollment.enrollment_provider_name" not in excluded

    def test_the_call_can_still_settle_the_gate_it_owns(self) -> None:
        """The merge must not swallow a real answer: once the rep says "No", the provider
        question is genuinely excluded."""
        plan = _fused_plan(
            build_ibv_standard, {"sections.enrollment.enrollment_required": "N/A"}
        )
        controller, _ = _controller(plan)
        controller.update_answers({"sections.enrollment.enrollment_required": "No"})
        excluded = {f.path for f in controller.excluded_fields(_task_index(plan, "closing_admin"))}
        assert "sections.enrollment.enrollment_provider_name" in excluded
```

- [ ] **Step 6: Prove Step 5's first test fails against a constructor-only fix**

Temporarily revert `update_answers` to the wholesale replace, keeping everything else:

```bash
# in plan_runtime.py, change update_answers' body back to: self._answers = dict(answers)
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py::TestTheObserverCannotReArmTheDeletion -v
```
Expected: `test_a_recorded_answer_does_not_restore_the_intake_default` FAILS. Restore the merge and re-run — both pass. Do not commit the temporary revert.

- [ ] **Step 7: Run the whole file**

Run: `uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add apps/agent_worker/tests/unit/test_plan_runtime.py
git commit -m "test(worker): pin the defaulted-gate deletions and the Observer path

The enrollment provider question and disease_only's renewal date were both
deleted from their compiled list by an intake default, and nothing pinned it.

TestTheObserverCannotReArmTheDeletion is the important one: a constructor-only
fix passes every other test here and still fails on a real call, because the
Observer pushes on every recorded answer. The bounds class passes before the
fix too — coverage_type still decides its later-task gates by position, and a
context prefill still excludes the male-partner questions."
```

---

### Task 3: Close the residual trap — a `confirm` leaf's `default` must not gate

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/dsl.py` (new function after `validate_question_coverage`, which ends at line 981; wire at line 745)
- Test: `tests/unit/forms/test_schema_dsl.py`

**Interfaces:**
- Consumes: `FormSchemaDoc`, `doc.leaf_items()`, `doc.shared_conditions`, `condition_field_paths` (defined in `dsl.py`), `vera_core.forms.conditions.leaf_gates` (function-scope import — `conditions` imports `dsl`, so module scope is a cycle).
- Produces: `vera_core.forms.dsl.validate_confirm_defaults(doc: FormSchemaDoc) -> list[str]`.

`gating_seed` removes `ask` from the gating input, so a `default` on an `ask` leaf is now inert for gating. `confirm` values stay authoritative by design — so a `default` on a **confirm** leaf would still silently delete every question gated on it, by exactly the mechanism this change removes for `ask`. Zero leaves in either catalog do this today: a trap-closer, needing no catalog edit.

**Correction to the spec:** the spec says this rule "belongs in `compile_document` rather than `_validate_document`". That is wrong — `compile_document` (dsl.py:984) is `model_dump` + `json.dumps` and runs no validation at all. It goes in `_validate_document` beside `validate_question_coverage`. The cost concern behind the spec's note does not apply: this is one pass over `leaf_gates` with no `build_question_plan` call. Fix that paragraph of the spec as part of this task.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/forms/test_schema_dsl.py`; add `validate_confirm_defaults` to the existing `from vera_core.forms.dsl import (...)` block.

```python
def test_no_catalog_gates_on_a_defaulted_confirm_leaf() -> None:
    """`gating_seed` keeps confirm-role prefills authoritative (the member-ID read-back), so a
    `default` on one would still settle its gate and delete the questions behind it — the
    exact failure removed for ask-role leaves."""
    for build in (build_ibv_standard, build_disease_only):
        assert validate_confirm_defaults(build()) == []


def test_the_document_validator_rejects_a_defaulted_confirm_gate() -> None:
    """A build failure, not an advisory list."""
    doc = minimal_doc()
    doc["sections"]["basics"]["fields"]["member_id"] = {
        "type": "text",
        "title": "Member ID",
        "role": "confirm",
        "default": "N/A",
        "prompt": {"confirm": "I have {{value}} on file — is that right?"},
    }
    doc["sections"]["basics"]["fields"]["notes"]["applicable_when"] = {
        "field": "sections.basics.member_id",
        "op": "eq",
        "value": "Yes",
    }
    with pytest.raises(ValidationError, match="declares a default and is referenced by"):
        FormSchemaDoc.model_validate(doc)


def test_a_defaulted_confirm_leaf_that_gates_nothing_is_fine() -> None:
    """The rule is about deleting other questions, not about defaults — `spouse_partner_name`
    carries one legitimately."""
    doc = minimal_doc()
    doc["sections"]["basics"]["fields"]["member_id"] = {
        "type": "text",
        "title": "Member ID",
        "role": "confirm",
        "default": "N/A",
        "prompt": {"confirm": "I have {{value}} on file — is that right?"},
    }
    assert validate_confirm_defaults(FormSchemaDoc.model_validate(doc)) == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py -k "confirm" -v`
Expected: FAIL at collection — `ImportError: cannot import name 'validate_confirm_defaults'`.

- [ ] **Step 3: Implement the rule**

In `packages/vera_core/src/vera_core/forms/dsl.py`, after `validate_question_coverage`:

```python
def validate_confirm_defaults(doc: FormSchemaDoc) -> list[str]:
    """A `confirm` leaf that gates another question may not declare a `default`.

    `call_plan.gating_seed` drops ask-role prefills from the worker's gate-evaluation set but
    keeps confirm ones — a confirm value is on file precisely to be read back. So a `default`
    on a gating confirm leaf still arrives as an answer and still deletes every question behind
    it, which is the failure `gating_seed` removes for ask-role leaves."""
    from vera_core.forms.conditions import leaf_gates

    leaves = dict(doc.leaf_items())
    shared = doc.shared_conditions or {}
    referenced = {
        ref
        for _path, _leaf, chain in leaf_gates(doc)
        for gate in chain
        for ref in condition_field_paths(gate, shared)
    }
    return [
        f"{path}: a confirm-role leaf that declares a default and is referenced by another "
        "field's applicable_when would settle that gate from intake, deleting the questions "
        "behind it"
        for path in sorted(referenced)
        if (leaf := leaves.get(path)) is not None
        and leaf.role == "confirm"
        and leaf.default is not None
    ]
```

Wire it at line 745, beside its sibling:

```python
        errors.extend(validate_question_coverage(self))
        errors.extend(validate_confirm_defaults(self))
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py -k "confirm" -v`
Expected: 3 passed.

- [ ] **Step 5: Run the full forms suite**

Run: `uv run pytest tests/unit/forms -q`
Expected: all pass — both real catalogs must still validate.

- [ ] **Step 6: Fix the spec's placement claim**

In `docs/superpowers/specs/2026-08-10-role-scoped-gating-input-design.md`, replace the final paragraph of "The residual trap, and the rule that closes it":

```markdown
It is wired into `_validate_document` beside `validate_question_coverage`, so it is a build
failure rather than an advisory list. Unlike its sibling it costs nothing measurable — one pass
over `leaf_gates`, no `build_question_plan` call — so it adds no meaningful load to the
dispatcher's per-call document load.
```

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/dsl.py \
        tests/unit/forms/test_schema_dsl.py \
        docs/superpowers/specs/2026-08-10-role-scoped-gating-input-design.md
git commit -m "feat(forms): reject a defaulted confirm leaf that gates another question

gating_seed keeps confirm-role prefills authoritative, so a default on one
would still settle its gate from intake and delete the questions behind it —
the failure just removed for ask-role leaves. No catalog declares one; the
rule closes the trap before a future schema arms it.

Also corrects the spec: compile_document runs no validation, so the rule
belongs in _validate_document, where it costs one leaf_gates pass."
```

---

### Task 4: Correct the stale docstring `9e17401b` left behind

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/conditions.py:159-161`

**Interfaces:** none — documentation only, no behaviour change.

`is_satisfied` claims it is *"shared by `gap_fields` (and so both `task_complete` guards through it)"*. Since `9e17401b` that is false: `gap_fields` goes through `owed_now` + `is_required` + a local `answered()`. Its only production caller is `completion_pct_v2`. The stale claim makes the `default` clause read as if it governed the completion guards, which is how this area was mis-scoped twice.

- [ ] **Step 1: Confirm the claim is false before rewriting it**

Run: `grep -rn "is_satisfied" --include="*.py" apps/ packages/ | grep -v "/tests/"`
Expected: exactly one production call site — `packages/vera_core/src/vera_core/forms/review.py:163`, inside `completion_pct_v2`.

- [ ] **Step 2: Rewrite the opening of the docstring**

Replace lines 159-161 of `packages/vera_core/src/vera_core/forms/conditions.py`:

```python
    """Whether a required, applicable field owes nothing, for `completion_pct_v2` — its only
    caller. NOT the call's owed set: `gap_fields` and both `task_complete` guards go through
    `call_plan.owed_now`, which consults no `default` (9e17401b). `review`'s path lists apply
    the same rule through `is_field_satisfied` instead, since they may hold a sentinel map.
```

Leave the rest of the docstring unchanged.

- [ ] **Step 3: Verify nothing moved**

Run: `uv run pytest tests/unit/forms/test_conditions.py tests/unit/forms/test_review.py -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/conditions.py
git commit -m "docs(forms): is_satisfied no longer feeds the completion guards

Stale since 9e17401b moved gap_fields onto owed_now. The claim made the
default clause read as if it governed task_complete, which is how this area
got mis-scoped twice."
```

---

### Task 5: Record the contract where the next schema author will read it

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/CLAUDE.md` ("Semantics worth remembering")

**Interfaces:** none — documentation.

That file already says *"Leaf `role` drives everything downstream"* and then lists only the UI and extraction consequences. The gate-time consequence — the one that cost two questions — is missing.

- [ ] **Step 1: Add two bullets after the existing `role` bullet**

```markdown
- **Role decides whether an intake value may settle a GATE.** `call_plan.gating_seed` drops
  `ask`-role paths from the worker's pre-call baseline: an `ask` leaf is collected on the
  call, so a value on file for one is a baseline, never an answer. `confirm` stays (on file
  to be read back), `context`/`input` stay (clinic-supplied). `PlanRunController.update_answers`
  MERGES the call's answers onto that baseline — a wholesale replace puts the intake values
  back, which is why the Observer pushes `_recorded` and not its full `_on_file` map.
- **`default` is an export/completion fallback, never an answer.** The export writes it when
  nothing was collected (`export_form_sheet`) and `completion_pct_v2` counts it filled; the
  call's owed set (`owed_now`) ignores it. The intake UI materializes it into `field_answer`
  at create, so a `default` on a leaf that GATES another question used to delete that question
  from the compiled prompt — `validate_confirm_defaults` rejects the confirm case, and
  `gating_seed` makes the ask case inert.
```

- [ ] **Step 2: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/CLAUDE.md
git commit -m "docs(forms): record that role decides gate-time authority"
```

---

### Task 6: Gates, simplification, and the live call

**Files:** none new — verification only.

**Interfaces:** none.

A change to spoken output is not verified by `pytest` (repo `CLAUDE.md`): the assertions are on strings and the deliverable is what the representative hears.

- [ ] **Step 1: Run the simplifier**

Invoke the `code-simplifier` agent on the change with the trigger phrase **"simplify code"** (repo `CLAUDE.md` — mandatory, same session as the implementation).

- [ ] **Step 2: Run the full gate**

Run: `just check`
Expected: green except the two pre-existing `test_auth_audit_chain` failures.

- [ ] **Step 3: Prove those two failures predate the branch**

```bash
uv run pytest tests/ -k auth_audit_chain -q                       # on HEAD
git stash push -u && git checkout $(git merge-base HEAD origin/dev)
uv run pytest tests/ -k auth_audit_chain -q                       # on the merge-base
git checkout - && git stash pop
```
Record both outputs for the PR body. Never wave a red gate through on say-so.

- [ ] **Step 4: Confirm the compiled artifacts did not drift**

Run: `just compile-schemas && git status --porcelain data/form_schemas/`
Expected: no diff. Nothing here touches a catalog module, so a diff means something unintended moved.

- [ ] **Step 5: Live call on browser-callee transport**

Per README "Browser callee": set `VERA_BROWSER_CALLEE_TRANSPORT=true` on both `just api` and `just worker`, plus `VITE_BROWSER_CALLEE_TRANSPORT=true` on the frontend, then join from Live Monitoring as the payer rep.

**Create a fresh form** — an existing one carries the intake rows either way, but a fresh one exercises the whole path from intake through dispatch.

Verify:
1. `closing_admin`'s spoken list contains "What is the provider name and phone number for enrollment?" **in position 2**, directly after the enrollment-required question.
2. Answer "Yes" to enrollment required → the bot asks the provider question next, in context.
3. On a second call, answer "No" → the bot skips it, moves to the centre-of-excellence question, and the sweep does not re-ask it.
4. Neither call ends with a gap-sweep re-ask of `enrollment_provider_*`.

- [ ] **Step 6: Confirm against the database**

```bash
docker exec vera-backend-postgres-1 psql -U vera -d vera_prompt_compiler_fix -tAc "
select to_char(created_at,'HH24:MI:SS'), field_path, source
from field_answer
where field_path like 'sections.enrollment.%' and source='ai_call'
order by created_at desc limit 6;"
```
Expected on the "Yes" call: `enrollment_provider_name` / `_phone` land within about a minute of `enrollment_required`, not three minutes later after an unrelated task's paths — the sweep signature recorded in the spec's Evidence section.

- [ ] **Step 7: Commit any simplifier changes and re-run the gate**

```bash
just check   # the last run must be on the exact tree that gets pushed
git add -A && git commit -m "refactor: simplifier pass on the gating-seed change"
```

---

## Notes for the PR body

Two items required by repo `CLAUDE.md`:

1. **Rolling deploy.** This change adds no `CallPlan` field, so it is not itself a deploy hazard — but the branch's existing plan-shape changes are. `PromptQuestion` is `extra="forbid"` and the plan persists in Redis; a mismatched blob falls back to the legacy monolithic agent with no guards and no sweep. Ship control plane and worker together.
2. **`just check` red on two pre-existing `test_auth_audit_chain` failures** — include the merge-base proof from Task 6 Step 3.

Also worth stating: the frontend still writes a `field_answer` row for every leaf `default` at create. Those rows are now inert for gating but remain misleading data. Removing the seeding is a follow-up, and it needs a decision about the rows already in the database.
