# Plan A — stop the gating block pre-excluding intra-task questions

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** The runtime gating block must only forbid a question whose gate is actually decided
at task entry, so the agent stops being told not to ask follow-ups it must ask.

**Architecture:** A gate conjunct is *decidable at task entry* only if every field path it
references is collected by an earlier task, or by no task at all. Classify each conjunct with
a path→task-index map built once at controller construction; evaluate only the decidable
conjuncts when building the gating block and when deciding whether a whole task can be
skipped. Leave `gap_fields` on full gate chains — at end of call the intra-task gates are
decided, and that is exactly when the gap pass needs them.

**Tech Stack:** Python 3.12, pydantic v2, pytest + pytest-asyncio. No new dependencies.

## Global Constraints

- No DSL grammar change, no migration, no schema recompile. This plan touches
  `apps/agent_worker` only.
- PEP 695 type params (`def f[T]`) — ruff rejects `TypeVar`/`Generic[T]`.
- `asyncio` only; never import `anyio`.
- Never log a field **value** — paths, titles, counts and shapes only (PHI).
- `just check` runs ruff check **and** `ruff format --check` as separate gates; both must pass.

---

## The bug, reproduced

`PlanTaskAgent.on_enter` calls `_apply_gating()` (`plan_runtime.py:233`) exactly once.
`PlanTaskAgent.on_user_turn_completed` (`:290-294`) does **not** re-gate — only
`GapTaskAgent` re-narrows its list. So the verdict is frozen at task entry, when every
intra-task gate is necessarily unanswered and therefore false.

Reproduced on `closing_admin` with a realistic entry snapshot (coverage tasks done, one
prior auth = Yes, nothing in `closing_admin` answered yet):

```
# Excluded by the plan's gates — do NOT ask these, whatever the task list says
- Enrollment Provider Name        gate refs in THIS task: ['enrollment_required']
- Enrollment Provider Phone       gate refs in THIS task: ['enrollment_required']
- TPA Name                        gate refs in THIS task: ['tpa_exists']
- PBM Name                        gate refs in THIS task: ['pbm_exists']
- PBM Phone                       gate refs in THIS task: ['pbm_exists']
- Infertility Specialty Pharmacy Name    gate refs in THIS task: ['isp_exists']
- Infertility Specialty Pharmacy Phone   gate refs in THIS task: ['isp_exists']
```

All 7 are follow-ups to an existence gate VERA asks **seconds later in the same task**, and
the instruction is never withdrawn when the rep says "yes, there is a TPA".

**Why "is the path answered?" is not a sufficient test.** An unanswered path means either
"not asked yet" or "gated out upstream and will never be answered". Only task position
distinguishes them. `any_service_requires_prior_auth` on a call where infertility was not
covered has 27 permanently-unanswered refs and *should* exclude the auth-department
questions; `enrollment_required` is unanswered and *must not* exclude anything yet.

**Why `_skip_when_nothing_applies` needs the same fix.** It reads full chains
(`plan_runtime.py:256`) and only avoids wrongly skipping tasks today because every gated task
happens to open with an ungated gate question (`infertility_tx_covered`,
`diagnostic_testing_covered`, `enrollment_required` all have no gates). That is luck, not
design. After this change it is correct by construction.

---

## File Structure

- **Modify** `apps/agent_worker/src/agent_worker/plan_runtime.py`
  - `PlanRunController.__init__` — build `self._task_of_path`
  - new `PlanRunController._decided_before(path, task_index)` + `_decidable_gates(field, task_index)`
  - new `PlanRunController.entry_gate_split(task_index)` — one pass, returns `(askable, excluded)`
  - `PlanRunController.applicable_fields` — unchanged signature, still full chains (used by
    `gap_fields`); its docstring now says it is invalid at task entry
  - `PlanRunController.inapplicable_fields` — **deleted** (no production caller left)
  - `PlanTaskAgent._apply_gating` — use the entry-time split
  - `PlanTaskAgent._skip_when_nothing_applies` — use the entry-time split
  - `_gating_block` — takes `(askable, excluded)`, unchanged rendering contract
- **Modify** `apps/agent_worker/tests/unit/test_plan_runtime.py` — `class TestGating` gains
  the intra-task cases; two existing assertions change (see Task 4)

**Interfaces:**

- Consumes: `PlanFieldDescriptor.gates: tuple[Condition, ...]`,
  `CallPlan.shared_conditions`, `vera_core.forms.dsl.condition_field_paths`,
  `vera_core.forms.conditions.is_applicable`.
- Produces, for Plan B and Plan C:
  - `PlanRunController._task_of_path: dict[str, int]` — collectable path → owning task index
  - `PlanRunController.entry_gate_split(task_index: int) -> tuple[list[PlanFieldDescriptor], list[PlanFieldDescriptor]]`
    returning `(askable, excluded)` in one pass
  - `PlanRunController.applicable_fields` / `gap_fields` keep their current signatures and
    full-chain semantics. `inapplicable_fields` was **deleted** — it had no production caller
    left once `_apply_gating` moved to the entry-time split.

---

### Task 1: path → owning-task map on the controller

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/plan_runtime.py` (`PlanRunController.__init__`, near `:593`)
- Test: `apps/agent_worker/tests/unit/test_plan_runtime.py`

- [ ] **Step 1: Write the failing test**

Add to `test_plan_runtime.py`, at the end of `class TestConstruction`:

```python
    def test_task_of_path_maps_every_collectable_field_to_its_task(self) -> None:
        controller, _ = _controller(_gap_plan())
        assert controller._task_of_path["sections.intro.rep_name"] == 0
        assert controller._task_of_path["sections.cov.deductible"] == 2
        assert controller._task_of_path["sections.close.ref_number"] == 3
        # a path no task collects (a context/prefilled leaf) is simply absent
        assert "sections.a.in_network" not in controller._task_of_path
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py::TestConstruction::test_task_of_path_maps_every_collectable_field_to_its_task -v
```

Expected: FAIL with `AttributeError: 'PlanRunController' object has no attribute '_task_of_path'`.

- [ ] **Step 3: Write minimal implementation**

In `PlanRunController.__init__`, immediately after `self._answers = dict(plan.prefilled)`:

```python
        # Collectable path -> the task that asks it. A gate referencing a path in THIS task
        # (or a later one) is undecided at entry; one referencing only earlier tasks is final,
        # answered or gated-out-upstream alike. Paths absent here are context/prefilled.
        self._task_of_path: dict[str, int] = {
            field.path: index
            for index, task in enumerate(plan.tasks)
            for field in task.fields
        }
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py::TestConstruction -v
```

Expected: PASS (all tests in the class).

- [ ] **Step 5: Commit**

```bash
git add apps/agent_worker/src/agent_worker/plan_runtime.py apps/agent_worker/tests/unit/test_plan_runtime.py
git commit -m "refactor(worker): map each collectable field path to its owning task"
```

---

### Task 2: entry-time gate decidability

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/plan_runtime.py` (new methods next to `applicable_fields`, ~`:875`)
- Test: `apps/agent_worker/tests/unit/test_plan_runtime.py`

**Interfaces:**
- Produces: `entry_gate_split(task_index) -> (askable, excluded)`, partitioning
  `plan.tasks[task_index].fields` in a single pass.

- [ ] **Step 1: Write the failing test**

Add a new plan builder next to `_gap_plan()`, then a test class. The plan mirrors the real
`closing_admin` shape: an ungated gate question, a follow-up gated on it (intra-task), and a
question gated on an earlier task's field (cross-task).

```python
def _intra_task_gate_plan() -> CallPlan:
    """closing_admin's shape: `tpa_name` is gated on `tpa_exists`, asked in the SAME task, so
    at entry that gate is undecided — not excluded. `auth_dept` is gated on a field the
    previous task collected, so at entry it IS decided."""
    return CallPlan(
        schema_name="Test",
        insurance_type="ibv_standard",
        dsl_version="2.1",
        schema_version_id=uuid.uuid4(),
        session=PlanSession(persona="P.", goal="G.", base_instructions="B."),
        tasks=[
            PlanTask(
                task_key="coverage_task",
                title="Coverage",
                prompt="Coverage.",
                fields=[
                    _field("sections.cov.prior_auth", "Prior auth", values=["Yes", "No"]),
                ],
            ),
            PlanTask(
                task_key="admin_task",
                title="Administrative Details",
                prompt="Admin.",
                fields=[
                    _field("sections.admin.tpa_exists", "TPA Exists", values=["Yes", "No"]),
                    _field(
                        "sections.admin.tpa_name",
                        "TPA Name",
                        gates=(
                            Comparison(field="sections.admin.tpa_exists", op="eq", value="Yes"),
                        ),
                    ),
                    _field(
                        "sections.admin.auth_dept",
                        "Authorization Department Name",
                        gates=(
                            Comparison(field="sections.cov.prior_auth", op="eq", value="Yes"),
                        ),
                    ),
                ],
            ),
            PlanTask(
                task_key="closing_task",
                title="Wrap Up",
                prompt="Reference number then end.",
                fields=[_field("sections.close.ref_number", "Reference number")],
            ),
        ],
    )


class TestEntryDecidability:
    def test_a_gate_on_a_field_this_task_asks_is_undecided_so_never_excluded(self) -> None:
        controller, _ = _controller(_intra_task_gate_plan())
        controller.update_answers({"sections.cov.prior_auth": "Yes"})
        askable, excluded = controller.entry_gate_split(1)
        assert "TPA Name" not in [f.title for f in excluded]
        assert "TPA Name" in [f.title for f in askable]

    def test_an_earlier_task_left_unanswered_upstream_still_decides(self) -> None:
        # An explicit "No" and a never-answered "" reach the same single comparison, so one
        # test covers both; prefer the harder input.
        controller, _ = _controller(_intra_task_gate_plan())
        _askable, excluded = controller.entry_gate_split(1)
        assert "Authorization Department Name" in [f.title for f in excluded]

    def test_gap_fields_still_uses_full_chains(self) -> None:
        # At end of call the intra-task gate IS decided, which is what the sweep needs.
        controller, _ = _controller(_intra_task_gate_plan())
        controller.update_answers(
            {"sections.cov.prior_auth": "No", "sections.admin.tpa_exists": "No"}
        )
        assert "TPA Name" not in [f.title for f in controller.gap_fields(1)]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py::TestEntryDecidability -v
```

Expected: FAIL with `AttributeError: … has no attribute 'entry_gate_split'`.

- [ ] **Step 3: Write minimal implementation**

Add the import at the top of `plan_runtime.py` (alongside the existing `vera_core.forms`
imports):

```python
from vera_core.forms.dsl import Condition, condition_field_paths
```

Add these three methods to `PlanRunController`, directly above `applicable_fields`:

```python
    def _decided_before(self, path: str, task_index: int) -> bool:
        owner = self._task_of_path.get(path)  # absent: context/prefilled, so always final
        return owner is None or owner < task_index

    def _decidable_gates(
        self, field: PlanFieldDescriptor, task_index: int
    ) -> tuple[Condition, ...]:
        """The conjuncts of `field`'s gate chain whose answer is already final at task entry.

        A conjunct referencing a path THIS task (or a later one) collects is undecided —
        evaluating it reads false and would forbid a follow-up the agent is about to need. A
        mixed conjunct (`any(earlier, this_task)`) counts as undecided whole: descending into an
        OR to salvage its decidable half would be unsound."""
        shared = self.plan.shared_conditions
        return tuple(
            gate
            for gate in field.gates
            if all(
                self._decided_before(ref, task_index)
                for ref in condition_field_paths(gate, shared)
            )
        )

    def entry_gate_split(
        self, task_index: int
    ) -> tuple[list[PlanFieldDescriptor], list[PlanFieldDescriptor]]:
        """(askable, excluded) for this task, judged only on gates already decided at entry.

        One pass so the two halves cannot drift, and so the gate walk happens once."""
        askable: list[PlanFieldDescriptor] = []
        excluded: list[PlanFieldDescriptor] = []
        shared = self.plan.shared_conditions
        for field in self.plan.tasks[task_index].fields:
            gates = self._decidable_gates(field, task_index)
            bucket = askable if is_applicable(gates, self._answers, shared) else excluded
            bucket.append(field)
        return askable, excluded
```

Then add a section header above `applicable_fields` so the two gate policies are signposted,
and requalify its docstring:

```python
    # -- gap-pass API: FULL gate chains, valid only once a task's own questions are asked ----

    def applicable_fields(self, task_index: int) -> list[PlanFieldDescriptor]:
        """A task's questions whose FULL gate chain holds. Valid for the gap pass, never at task
        entry — an intra-task gate is undecided there (see `entry_gate_split`)."""
```

Delete `inapplicable_fields` — `_apply_gating` was its only production caller. Migrate the two
assertions in `TestFullyGatedTaskSkip` that used it onto `entry_gate_split`, which is what
production now reads.

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py::TestEntryDecidability -v
```

Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add apps/agent_worker/src/agent_worker/plan_runtime.py apps/agent_worker/tests/unit/test_plan_runtime.py
git commit -m "feat(worker): classify gate conjuncts by whether they are decided at task entry"
```

---

### Task 3: the gating block and the whole-task skip use entry decidability

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/plan_runtime.py` (`_apply_gating` ~`:270`, `_skip_when_nothing_applies` ~`:249`)
- Test: `apps/agent_worker/tests/unit/test_plan_runtime.py`

- [ ] **Step 1: Write the failing test**

Append to `class TestEntryDecidability`:

```python
    @pytest.mark.asyncio
    async def test_the_instructions_never_forbid_a_same_task_follow_up(self) -> None:
        # The bug: at entry `tpa_exists` is unanswered, so the old block listed TPA Name under
        # "do NOT ask these" and never withdrew it when the rep said yes.
        controller, _ = _controller(_intra_task_gate_plan())
        controller.update_answers({"sections.cov.prior_auth": "No"})
        agent = controller.agents[1]
        with _session_patch(agent, MagicMock()):
            await agent.on_enter()
            await controller.drain_cursor_writes()
        excluded_block = agent.instructions.split("# Excluded by the plan's gates")[1]
        assert "TPA Name" not in excluded_block
        assert "Authorization Department Name" in excluded_block

    @pytest.mark.asyncio
    async def test_a_task_whose_only_failing_gates_are_undecided_gets_no_block(self) -> None:
        controller, _ = _controller(_intra_task_gate_plan())
        controller.update_answers({"sections.cov.prior_auth": "Yes"})
        agent = controller.agents[1]
        before = agent.instructions
        with _session_patch(agent, MagicMock()):
            await agent.on_enter()
            await controller.drain_cursor_writes()
        assert agent.instructions == before

    @pytest.mark.asyncio
    async def test_a_task_is_not_skipped_because_its_own_gate_question_is_unanswered(
        self,
    ) -> None:
        # `_skip_when_nothing_applies` on full chains would see tpa_name + auth_dept excluded
        # and tpa_exists applicable, so it never fired here — but a task whose EVERY question
        # is intra-task-gated would have been skipped outright. Entry decidability makes that
        # impossible by construction.
        controller, _ = _controller(_intra_task_gate_plan())
        controller.update_answers({"sections.cov.prior_auth": "No"})
        agent = controller.agents[1]
        session = MagicMock()
        with _session_patch(agent, session):
            await agent.on_enter()
            await controller.drain_cursor_writes()
        session.update_agent.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py::TestEntryDecidability -v -k follow_up
```

Expected: FAIL — `assert "TPA Name" not in excluded_block` fails, because the block still
lists it.

- [ ] **Step 3: Write minimal implementation**

In `_apply_gating`, replace the two calls:

```python
        gating = _gating_block(*self._controller.entry_gate_split(self._task_index))
```

Extend that method's docstring with the reason:

```python
        Judged on ENTRY-DECIDED gates only (`_decidable_gates`). A question gated on a field
        this task itself asks is undecided here, and listing it as excluded told the agent not
        to ask the follow-up it needed one turn later — 131 of this schema's 149 gated
        questions are that shape. Those are governed by the prose gate in the task prompt,
        which the agent re-evaluates every turn; this block only ever names questions a
        settled answer has ruled out.
```

In `_skip_when_nothing_applies`, replace the applicability probe:

```python
        askable, _ = self._controller.entry_gate_split(self._task_index)
        if not self._task.fields or askable:
            return False
```

and append to its docstring:

```python
        Entry-decided gates only: on full chains a task whose every question is gated on its
        own first question would skip itself before asking anything.
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py -v
```

Expected: `TestEntryDecidability` passes; two tests in `TestGating` now FAIL — that is Task 4.

- [ ] **Step 5: Commit**

```bash
git add apps/agent_worker/src/agent_worker/plan_runtime.py apps/agent_worker/tests/unit/test_plan_runtime.py
git commit -m "fix(worker): gating block no longer forbids questions gated on the same task"
```

---

### Task 4: reconcile the existing gating tests

**Files:**
- Modify: `apps/agent_worker/tests/unit/test_plan_runtime.py` (`class TestGating`, ~`:893-945`)

`_gap_plan()`'s `coverage_task` gates `oon_note` on `sections.a.in_network`, a path no task
collects, so it stays decidable and those tests keep their meaning. Confirm rather than
assume — if any assertion fails, the cause is a real behaviour change, not test rot.

- [ ] **Step 1: Run the existing class and read the failures**

```bash
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py::TestGating -v
```

- [ ] **Step 2: For each failure, decide and fix**

- If the failure is on `"apply on THIS call" in agent.instructions` or on the
  applicable-before-excluded ordering: the block's rendering is unchanged by this plan, so a
  failure means the field is now in the other bucket. Re-read which bucket is correct and
  update the assertion, adding a one-line comment naming the gate class.
- Add this regression test to `TestGating` so the decidable case stays covered:

```python
    @pytest.mark.asyncio
    async def test_a_gate_on_a_field_no_task_collects_still_excludes(self) -> None:
        # `in_network` is prefilled context, not a question, so its value is final at entry.
        controller, _ = _controller(_gap_plan())
        controller.update_answers({"sections.a.in_network": "Yes"})
        agent = await self._enter(controller, 2)
        assert "OON note" in agent.instructions.split("# Excluded by the plan's gates")[1]
```

- [ ] **Step 3: Run the whole worker unit suite**

```bash
uv run pytest apps/agent_worker/tests/unit -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add apps/agent_worker/tests/unit/test_plan_runtime.py
git commit -m "test(worker): cover both gate classes in the gating block"
```

---

### Task 5: verify against the real schema, then gate and simplify

**Files:** none modified — this task is verification.

- [ ] **Step 1: Confirm the reported defect is gone on the real schema**

```bash
uv run python - <<'EOF'
from uuid import uuid4
from vera_core.forms.catalog import build_ibv_standard
from vera_core.forms.call_plan import compile_call_plan
from agent_worker.plan_runtime import PlanRunController, _gating_block

doc = build_ibv_standard()
plan = compile_call_plan(doc, None, schema_version_id=uuid4(), prompt_version_id=None)


class _S:
    async def set_active_task(self, *a, **k): ...


c = PlanRunController(plan, room_name="r", run_state=_S())
i = next(i for i, t in enumerate(plan.tasks) if t.task_key == "closing_admin")
c.update_answers(
    {"sections.infertility_treatment.intrauterine_insemination.cpt_58323.prior_auth": "Yes"}
)
print(_gating_block(*c.entry_gate_split(i)) or "(no block)")
EOF
```

Expected: **no `# Excluded` section naming** TPA Name, PBM Name, PBM Phone, Infertility
Specialty Pharmacy Name/Phone, Enrollment Provider Name/Phone. With one prior auth = Yes the
auth-department questions are askable, so the whole block should be absent.

- [ ] **Step 2: Confirm the cross-task exclusion still works**

Re-run the snippet with `c.update_answers({})`. Expected: an `# Excluded` section naming
**only** `Authorization Department Name` and `Authorization Department Phone`.

- [ ] **Step 3: Run `/simplify` on the change, then the full gate**

```bash
just check
```

- [ ] **Step 4: Commit any simplifier refinements**

```bash
git add -A && git commit -m "refactor(worker): simplify entry-decidability helpers"
```

- [ ] **Step 5: Live-call acceptance**

Dispatch one real call and drive it to the administrative task. Say **"yes, there is a third
party administrator"**. VERA must then ask for the TPA's name. Before this change she is
explicitly instructed not to. Do the same for the PBM and the infertility specialty pharmacy.

`pytest` cannot substitute for this step — the assertions are on strings, the defect is in
what the agent says.

---

## Out of scope for this plan

- Deleting the gating block entirely, and resolving cross-task gates by dropping the question
  from the rendered list instead of appending a correction. That needs a re-renderable
  question tree in the plan → **Plan B**.
- Removing `any_service_requires_prior_auth`'s 3,007-char prose rendering from the task
  prompt → **Plan B**.
- Re-gating during a task as answers land. Not needed: after this change the intra-task gates
  live only in the prose the agent re-reads every turn.
