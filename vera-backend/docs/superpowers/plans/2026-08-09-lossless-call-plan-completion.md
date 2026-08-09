# Lossless CallPlan — Completion Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the voice agent ending a task with required questions unasked, by computing the owed set from the same question tree the prompt is rendered from.

**Architecture:** The compiled `CallPlan` carries two artifacts — `task.panels` (the question tree the prompt renders from) and `task.fields` (the descriptor list the completion guard reads). They disagree. This plan makes the tree a **complete index** into the descriptors (one missing link: `immediate_confirms` are prose, not nodes), then computes the owed set as a **join** — question units from the tree, gates and requiredness from the descriptors, evaluated per target path. Gates do not move; they were always on the descriptors and are already evaluated by the worker.

**Tech Stack:** Python 3.12, pydantic v2, pytest + pytest-asyncio, livekit-agents. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-09-lossless-call-plan-completion-design.md`

## Global Constraints

- All paths below are relative to `vera-backend/`.
- `just check` runs `ruff check` **and** `ruff format --check` as **separate** gates, plus `mypy --strict` and pytest. Run it **verbatim** — never a hand-picked subset. A formatting-only failure has reached CI before this way.
- Run `/simplify` on the change, then re-run `just check`, before claiming a task done.
- PEP 695 type params (`def f[T]`, `class Foo[T]`) — ruff rejects `TypeVar` / `Generic[T]`.
- `asyncio` only. Never `import anyio`.
- **Never log a field VALUE** — paths, titles, counts and shapes only. Values are PHI.
- Any span whose body touches PHI takes `record_exception=False, set_status_on_exception=False`.
- No DSL grammar change, no `dsl_version` bump, no migration, no frontend change.
- **Tasks 1–2 must not change the rendered prompt.** `TestPanelsMatchThePrompt` (`tests/unit/forms/test_call_plan.py`) must stay green **without being modified**. If it needs editing, the task is wrong.
- **No task in this plan needs `just compile-schemas` or `just seed-schemas`.** Verified: `data/form_schemas/*.json` serializes `FormSchemaDoc` only — it contains no `panels`, `immediate_confirms`, `gate_text` or `routes_between`. No task here touches a `catalog/` module, so the artifacts cannot drift and the freshness test in `tests/unit/forms/test_schema_dsl.py` cannot fail from this work. Do **not** commit `data/form_schemas/`; if it shows a diff, something unrelated changed and you should stop and report it.
- Commit after every task. Do not squash tasks together.

---

## File Structure

| File | Responsibility | Tasks |
| --- | --- | --- |
| `packages/vera_core/src/vera_core/forms/question_plan.py` | Question tree types + builder. Gains `is_confirm` on `PromptQuestion`; confirm anchors become child nodes. | 1 |
| `packages/vera_core/src/vera_core/forms/prompting.py` | Renders the tree. Must emit byte-identical text for confirm nodes; must not number them. | 1 |
| `packages/vera_core/src/vera_core/forms/dsl.py` | Document validator. Gains the losslessness rules. | 2 |
| `packages/vera_core/src/vera_core/forms/call_plan.py` | Compiled artifact. Gains `owed_now()` — the tree↔descriptor join. | 3 |
| `apps/agent_worker/src/agent_worker/plan_runtime.py` | `gap_fields` / `owed_question_count` delegate to `owed_now`; guard window fix; P10 idempotence. | 3, 4, 7 |
| `apps/agent_worker/src/agent_worker/observer.py` | `drain_pending()` so the sweep can wait for extraction; per-answer span. | 6, 8 |

Task 6 is **independent of 1–5** and may be pulled forward — it fixes phantom gaps under today's logic too.

---

### Task 1: Confirm anchors become question nodes

Two collectable paths (`spouse_partner_name`, `spouse_partner_dob`) reach the agent as pre-rendered prose strings on another question's `immediate_confirms`, so nothing that walks the tree can see them. This makes them real nodes — **without changing a single byte of rendered prompt**.

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/question_plan.py`
- Modify: `packages/vera_core/src/vera_core/forms/prompting.py:390-419` (`_numbered_question`), `:339-351` (`numbered_questions`)
- Test: `tests/unit/forms/test_question_plan.py`, `tests/unit/forms/test_prompting.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `PromptQuestion.is_confirm: bool` — `True` marks a node that renders as a nested sub-bullet under its anchor and receives **no** ordinal. `PromptQuestion.confirm_line: str | None` — the pre-rendered text that used to live in the parent's `immediate_confirms` list. Confirm nodes appear in `panel.items` immediately after their anchor question and carry `options[].target_paths` naming the confirmed leaf. Task 2 asserts every collectable path is reachable from exactly one node; Task 3 joins those paths to descriptors.

- [ ] **Step 1: Write the failing test — confirm targets are reachable from the tree**

Add to `tests/unit/forms/test_question_plan.py`:

```python
def test_confirm_anchors_are_reachable_question_nodes() -> None:
    """A confirm_immediate leaf must be a node with target_paths, not prose on its anchor.

    Anything that walks the tree to decide what is still owed can only see nodes; a
    pre-rendered string is invisible to it (spec §1).
    """
    doc = build_ibv_standard()
    task = next(t for t in doc.tasks if t.task_key == "insurance_basics")
    panels = build_question_plan(doc, task, _immediate_by_anchor(doc))
    reachable = {p for q in iter_questions(panels) for p in q.target_paths}
    assert "sections.patient_information.spouse_partner_name" in reachable
    assert "sections.patient_information.spouse_partner_dob" in reachable


def test_confirm_nodes_are_not_numbered() -> None:
    """They render nested under their anchor, so they must not consume an ordinal —
    the same treatment routing questions already get."""
    doc = build_ibv_standard()
    task = next(t for t in doc.tasks if t.task_key == "insurance_basics")
    panels = build_question_plan(doc, task, _immediate_by_anchor(doc))
    assert numbered_questions(panels) == 16
```

`build_question_plan`'s `immediate_by_anchor` parameter changes type in Step 4 from
`dict[str, list[str]]` to `dict[str, list[tuple[str, str]]]` — `(collected path, rendered line)` —
so a confirm node knows which leaf it collects. Both this test and Task 2's validator must build
it that way.

Put the builder in `prompting.py` as a module-level function (**not** in the test), so the test
and the validator share production's anchor rule instead of reimplementing it:

```python
def immediate_confirms_by_anchor(doc: FormSchemaDoc) -> dict[str, list[tuple[str, str]]]:
    """`{anchor path: [(collected path, rendered confirm line)]}` for every
    `confirm_immediate` leaf — the anchor rule `render_task_prompts` already applies,
    exposed so the question-plan builder and the validator cannot drift from it."""
```

Extract its body from the existing loop at `prompting.py:205-214`, which already computes
`_anchor(...)` and today appends `(path, leaf, gates)`; render the line with the same renderer
`_task_text` uses, and return `(path, line)`. Then have `render_task_prompts` call it rather than
inlining the loop, so there is one implementation.

Import it in the test:

```python
from vera_core.forms.prompting import immediate_confirms_by_anchor as _immediate_by_anchor
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/unit/forms/test_question_plan.py::test_confirm_anchors_are_reachable_question_nodes \
              tests/unit/forms/test_question_plan.py::test_confirm_nodes_are_not_numbered -v
```

Expected: the first FAILS (the two paths are not in `reachable` — they are prose on the anchor). The second PASSES already (16 today) — it is a **regression guard** proving the next step does not renumber. Keep both.

- [ ] **Step 3: Add the node fields to `PromptQuestion`**

In `question_plan.py`, extend the model. Keep `immediate_confirms` for now — Step 4 stops populating it, Step 6 removes it, so the artifact never has two live representations at once:

```python
class PromptQuestion(_Model):
    """One thing the agent says out loud, and every field path it can answer."""

    kind: Literal["question"] = "question"
    text: str
    options: list[PromptOption] = Field(default_factory=list)
    gate_text: str | None = None
    derive_text: str | None = None
    required_text: str | None = None
    immediate_confirms: list[str] = Field(default_factory=list)
    fanned_codes: list[str] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)
    optional: bool = False
    routes_between: list[str] = Field(default_factory=list)
    # A `confirm_immediate` leaf: renders as a nested bullet under the question it
    # follows and takes NO ordinal (like `routes_between`), but unlike a routing
    # question it DOES collect, so it is owed. Numbering is a rendering concern;
    # owed-ness is a state concern.
    is_confirm: bool = False
    confirm_line: str | None = None
```

- [ ] **Step 4: Emit confirm nodes instead of prose**

In `question_plan.py`, `_Builder._scoped` currently flattens confirms into strings (`:440-444`). Stop populating `immediate_confirms` there:

```python
                "immediate_confirms": [],
```

Then, wherever `_Builder` appends a question to a panel's `items`, append its confirm nodes immediately after it. Add this helper to `_Builder`:

```python
    def _confirm_nodes(self, question: PromptQuestion) -> list[PromptQuestion]:
        """The `confirm_immediate` leaves anchored to `question`, as nodes in spoken order.

        Ordered by the anchor's own target order so the rendered bullets keep the sequence
        the previous string list produced — this is what keeps the prompt byte-identical."""
        nodes: list[PromptQuestion] = []
        for target in question.target_paths:
            for path, line in self.immediate_by_anchor.get(target, []):
                nodes.append(
                    PromptQuestion(
                        text=line,
                        options=[PromptOption(target_paths=[path])],
                        is_confirm=True,
                        confirm_line=line,
                    )
                )
        return nodes
```

`self.immediate_by_anchor` must now carry `(path, line)` pairs rather than bare lines, so the node knows which leaf it collects. Update `build_question_plan`'s parameter type to `dict[str, list[tuple[str, str]]]` and update `prompting.py`'s construction of it to pass the path alongside the rendered line.

- [ ] **Step 5: Render confirm nodes byte-identically, and exclude them from numbering**

In `prompting.py`, `numbered_questions` must skip them exactly as it skips routing questions:

```python
    return sum(
        numbered_questions([item])
        if isinstance(item, PromptPanel)
        else (0 if item.routes_between or item.is_confirm else 1)
        for panel in panels
        for item in panel.items
    )
```

In `_panel_lines`, a confirm node must not start a new numbered line. Walk the panel's items and
absorb each run of confirm nodes into the numbered question they follow:

```python
    items = list(panel.items)
    i = 0
    while i < len(items):
        item = items[i]
        if isinstance(item, PromptPanel):
            lines.extend(_panel_lines(item, depth + 1, numbering))
            i += 1
            continue
        run: list[PromptQuestion] = []
        j = i + 1
        while j < len(items) and isinstance(items[j], PromptQuestion) and items[j].is_confirm:
            run.append(items[j])
            j += 1
        lines.extend(_numbered_question(next(numbering), item, run))
        i = j
```

and give `_numbered_question` the run, replacing the `immediate_confirms` branch at `:416-418`
so exactly one code path produces those lines:

```python
def _numbered_question(
    number: int, question: PromptQuestion, confirms: list[PromptQuestion] = []
) -> list[str]:
    ...
    if confirms:
        lines.append("   - Immediately after this answer:")
        lines.extend(f"     * {c.confirm_line}" for c in confirms)
    return lines
```

The header is emitted **once** per run, then one `*` bullet per node — byte-identical to what the
string list produced. A confirm node reaching `_panel_lines` without a preceding numbered
question would be a builder bug; Task 2's validator is what catches it.

- [ ] **Step 6: Delete the dead field**

Remove `immediate_confirms` from `PromptQuestion` and from `hydrate_panels` (`question_plan.py:671`). Nothing populates or reads it now. Leaving it would be a second representation of the same fact — the defect this plan exists to remove.

- [ ] **Step 7: Run the full gate**

```bash
just check
```

Expected: PASS. **`TestPanelsMatchThePrompt` in `tests/unit/forms/test_call_plan.py` must be green without having been modified** — that is this task's real exit criterion. If the rendered text moved, Step 5 is wrong; fix Step 5, do not edit the test.

- [ ] **Step 8: Run `/simplify`, then re-run `just check`**

- [ ] **Step 9: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/question_plan.py \
        packages/vera_core/src/vera_core/forms/prompting.py \
        tests/unit/forms/
git commit -m "refactor(forms): confirm anchors become question nodes, prompt byte-identical

A confirm_immediate leaf reached the agent as a pre-rendered string on its
anchor's immediate_confirms, so nothing walking the question tree could see
it. It is now a node with target_paths, unnumbered like a routing question,
rendering the same bullets it always did."
```

---

### Task 2: Losslessness validator

Turn the 16-vs-20 divergence into a build failure so it cannot come back.

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/dsl.py` (the document validator, near the `confirm_immediate` anchor check at `:728-742`)
- Test: `tests/unit/forms/test_schema_dsl.py`

**Interfaces:**
- Consumes: `PromptQuestion.is_confirm` and confirm nodes from Task 1.
- Produces: a validation error string `"<task_key>: collectable path <path> is not reachable from any spoken question"` when a collectable leaf has no question node, and `"...is reachable from N questions"` when more than one claims it. Task 3 relies on this being total.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/forms/test_schema_dsl.py`:

```python
def test_every_collectable_path_is_reachable_from_exactly_one_question() -> None:
    """The completion guard walks questions; a path no question targets can never be
    asked for, and a path two questions target would be double-counted (spec §8)."""
    for build in (build_ibv_standard, build_disease_only):
        doc = build()
        assert validate_question_coverage(doc) == []


def test_an_unreachable_collectable_path_is_reported() -> None:
    doc = build_ibv_standard()
    # Drop a section's questions but keep its collectable leaves.
    errors = validate_question_coverage(doc, _drop_questions_for="benefit_coverage")
    assert any("not reachable from any spoken question" in e for e in errors)
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/unit/forms/test_schema_dsl.py -k question_coverage -v
```

Expected: FAIL with `NameError: name 'validate_question_coverage' is not defined`.

- [ ] **Step 3: Implement the check**

In `dsl.py`, add the function and call it from the document validator so it runs on every `compile_document` / `load_document`:

```python
def validate_question_coverage(doc: "FormSchemaDoc") -> list[str]:
    """Every collectable leaf must be reachable from exactly one spoken question.

    This is what makes `task.fields` a PROJECTION of the question tree rather than a
    parallel artifact. Without it the prompt can say 16 while the guard counts 20, which
    is exactly how a live call ended a task with six required questions unasked."""
    # Function-scope imports: `question_plan` and `prompting` both import `dsl`, so a
    # module-scope import here would be a cycle.
    from vera_core.forms.prompting import immediate_confirms_by_anchor
    from vera_core.forms.question_plan import build_question_plan, iter_questions

    errors: list[str] = []
    section_to_task = doc.section_to_task()
    anchors = immediate_confirms_by_anchor(doc)
    for task in doc.tasks:
        panels = build_question_plan(doc, task, anchors)
        hits: Counter[str] = Counter(
            path for q in iter_questions(panels) for path in q.target_paths
        )
        for path, leaf, _gates in leaf_gates(doc):
            if leaf.role not in COLLECTED_ROLES:
                continue
            owner = (
                leaf.confirm_in_task.task_key
                if leaf.confirm_in_task is not None
                else section_to_task.get(path.split(".")[1])
            )
            if owner != task.task_key:
                continue
            if hits[path] == 0:
                errors.append(
                    f"{task.task_key}: collectable path {path} is not reachable "
                    "from any spoken question"
                )
            elif hits[path] > 1:
                errors.append(
                    f"{task.task_key}: collectable path {path} is reachable from "
                    f"{hits[path]} questions"
                )
    return errors
```

Import `Counter` from `collections` and reuse the module's existing `COLLECTED_ROLES` and `leaf_gates`. If importing `question_plan` at module scope creates a cycle, keep the local import as written — `dsl.py` is imported by `question_plan.py`, so the cycle is real.

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/unit/forms/test_schema_dsl.py -k question_coverage -v
```

Expected: PASS on both catalogs. Measured before writing this plan: `ibv_standard` has 182 collectable fields with 2 unreachable and 0 duplicated; those 2 are the spouse confirms Task 1 converts. `disease_only` has 44 with 0 and 0. **So this passes only if Task 1 landed correctly** — if it fails naming the spouse paths, Task 1 is incomplete.

- [ ] **Step 5: Run the full gate**

```bash
just check
```

- [ ] **Step 6: Run `/simplify`, then re-run `just check`**

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/dsl.py tests/unit/forms/test_schema_dsl.py
git commit -m "feat(forms): validate every collectable path is reachable from one question

Turns the prompt-says-16 / guard-counts-20 divergence into a build failure."
```

---

### Task 3: The owed set becomes a tree↔descriptor join

**Files:**
- Modify: `packages/vera_core/src/vera_core/forms/call_plan.py` (add `owed_now`)
- Modify: `apps/agent_worker/src/agent_worker/plan_runtime.py:1020-1042` (`gap_fields`, `owed_question_count`)
- Test: `tests/unit/forms/test_call_plan.py`, `apps/agent_worker/tests/unit/test_plan_runtime.py`

**Interfaces:**
- Consumes: Task 2's guarantee that the join is total.
- Produces: `call_plan.owed_now(task: PlanTask, answers: Mapping[str, Any], shared: Mapping[str, Condition]) -> list[PromptQuestion]`. `PlanRunController.gap_fields(task_index) -> list[PlanFieldDescriptor]` keeps its signature and returns the unanswered applicable descriptors **under the owed questions**. `owed_question_count(task_index) -> int` returns `len(owed_now(...))`. Task 4 consumes both.

- [ ] **Step 1: Write the failing test — Run B must be refusable**

Add to `apps/agent_worker/tests/unit/test_plan_runtime.py`:

```python
_S = "sections."
_RUN_B_ANSWERS = {
    _S + "insurance_information.doctor_inside_network": "Yes",
    _S + "insurance_information.facility_inside_network": "Yes",
    _S + "insurance_information.plan_type": "PPO",
    _S + "insurance_information.cob_status": "Secondary",
    _S + "insurance_information.policy_number": "X",
    _S + "insurance_information.group_name": "X",
    _S + "insurance_information.group_number": "X",
    _S + "insurance_information.policy_situs": "X",
    _S + "benefit_coverage.benefit_year_type": "Calendar Year",
    _S + "benefit_coverage.coverage_type": "Family",
    _S + "patient_information.spouse_partner_name": "X",
    _S + "patient_information.spouse_partner_dob": "X",
}


def test_run_b_owes_six_questions_including_the_defaulted_one() -> None:
    """Live trace 6e1f496cb72d0182af27281c90bdca64. `gap_fields` reported 5 because
    `is_satisfied` short-circuits on `default is not None`, hiding telehealth_covered.
    The tree-joined owed set consults no default, so it is 6."""
    controller, _ = _controller(_ibv_plan())
    index = _task_index(controller, "insurance_basics")
    controller.update_answers(dict(_RUN_B_ANSWERS))
    owed = {f.path.split(".")[-1] for f in controller.gap_fields(index)}
    assert owed == {
        "plan_effective_date",
        "plan_year_information",
        "telehealth_covered",
        "plan_fund_type",
        "employer_support_size",
        "infertility_plan_mandate",
    }
    assert "pcp_referral_required" not in owed  # gated out: plan_type is not HMO
```

Add these two helpers to the test module. Do **not** hand-build a fixture plan here — the point of
this test is that the *real* schema produces this set:

```python
def _ibv_plan() -> CallPlan:
    return compile_call_plan(
        build_ibv_standard(), None, schema_version_id=uuid4(), prompt_version_id=None
    )


def _task_index(controller: PlanRunController, task_key: str) -> int:
    return next(i for i, t in enumerate(controller.plan.tasks) if t.task_key == task_key)
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py::test_run_b_owes_six_questions_including_the_defaulted_one -v
```

Expected: FAIL — the set is missing `telehealth_covered` (5 of 6 present).

- [ ] **Step 3: Implement `owed_now` in `call_plan.py`**

```python
def owed_now(
    task: PlanTask, answers: Mapping[str, Any], shared: Mapping[str, Condition]
) -> list[PromptQuestion]:
    """The task's still-owed SPOKEN questions, in spoken order.

    A join, not a second walk: question identity comes from the tree (so one spoken
    question covering N fields counts once), while gates and requiredness come from the
    descriptors those questions point at. Evaluated PER TARGET because a fan-out's targets
    do not share a gate chain — the IUI copay question spans three CPT codes, each gated on
    its own `covered` — so the question is owed while any covered code still lacks a copay.

    `default` is deliberately not consulted: it declares the value a field takes when not
    collected, never that the question need not be asked. `is_satisfied` keeps reading it,
    so completion percentage, export and intake are unaffected."""
    by_path = {field.path: field for field in task.fields}
    owed: list[PromptQuestion] = []
    for question in iter_questions(task.panels):
        if question.routes_between:
            continue  # chooses between panels; collects nothing
        live = [
            field
            for path in question.target_paths
            if (field := by_path.get(path)) is not None
            and is_applicable(field.gates, answers, shared)
        ]
        if any(
            is_required(field, answers, shared) and not has_value(answers, field.path)
            for field in live
        ):
            owed.append(question)
    return owed
```

Import `is_applicable`, `is_required`, `has_value` from `vera_core.forms.conditions` and `iter_questions`, `PromptQuestion` from `vera_core.forms.question_plan`.

- [ ] **Step 4: Point the worker at it**

Replace `PlanRunController.gap_fields` and `owed_question_count` in `plan_runtime.py`:

```python
    def gap_fields(self, task_index: int) -> list[PlanFieldDescriptor]:
        """The unanswered applicable descriptors under this task's still-owed questions.

        Field-granular by design (Plan C, 2026-08-07: ceilings count asks, lists name
        missing fields) — re-asking a partially answered fan-out by its question text would
        re-ask the half already on file."""
        task = self.plan.tasks[task_index]
        shared = self.plan.shared_conditions
        by_path = {field.path: field for field in task.fields}
        return [
            field
            for question in owed_now(task, self._answers, shared)
            for path in question.target_paths
            if (field := by_path.get(path)) is not None
            and is_applicable(field.gates, self._answers, shared)
            and is_required(field, self._answers, shared)
            and not has_value(self._answers, path)
        ]

    def owed_question_count(self, task_index: int) -> int:
        """`gap_fields` measured in SPOKEN questions — the ceiling both guards judge by."""
        return len(owed_now(self.plan.tasks[task_index], self._answers, self.plan.shared_conditions))
```

Delete the now-unused `owed_questions` import and the `self._alternatives` / `is_satisfied` use in this path. `alternative_index` may still be needed elsewhere — remove it only if nothing references it.

- [ ] **Step 5: Run the new test and the whole existing suite**

```bash
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py tests/unit/forms -v
```

Expected: the new test PASSES. Existing `owed_question_count` assertions (`test_plan_runtime.py:1409-1428`, `:2620`) must still pass — they were written against panel-based counting and should be unaffected. **If one fails, read it before changing it**: it may be encoding the alternatives behaviour, which `owed_now` now gets structurally (an either/or is one question, so one answered option satisfies it). Update the test only if you can state why the new number is right.

- [ ] **Step 6: Run the full gate**

```bash
just check
```

- [ ] **Step 7: Run `/simplify`, then re-run `just check`**

- [ ] **Step 8: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/call_plan.py \
        apps/agent_worker/src/agent_worker/plan_runtime.py \
        apps/agent_worker/tests/unit/test_plan_runtime.py
git commit -m "fix(worker): compute the owed set from the question tree, not the field list

The prompt renders from task.panels and the guard counted task.fields, so the
two disagreed. gap_fields now walks the owed questions and joins to the
descriptors they point at, evaluated per target because a CPT fan-out's
targets do not share a gate chain.

Consulting no default recovers telehealth_covered, which is_satisfied hid
from every guard and from the end-of-call sweep."
```

---

### Task 4: Fix the completion guard's measurement window

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/plan_runtime.py:220-232` (`PlanTaskAgent.__init__`), `:260-281` (`on_enter`), `:367-398` (`_refuse_premature_completion`)
- Test: `apps/agent_worker/tests/unit/test_plan_runtime.py`

**Interfaces:**
- Consumes: `owed_question_count` and `gap_fields` from Task 3.
- Produces: no new public surface. `PlanTaskAgent` gains `_questions_at_entry: int`, `_refusals: int`, `_outstanding_at_last_refusal: int | None`; `_completion_refused` is removed.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_run_b_shape_is_refused_after_eleven_turns() -> None:
    """The trace: 11 rep turns into a 16-question task with 6 still owed. The old guard
    compared whole-task turns (11) against the CURRENT outstanding count (5) and bailed.
    Both sides must measure the same window."""
    controller, _ = _controller(_ibv_plan())
    index = _task_index(controller, "insurance_basics")
    agent = controller.agents[index]
    with _session_patch(agent, MagicMock()):
        await agent.on_enter()
        controller.update_answers(dict(_RUN_B_ANSWERS))
        for _ in range(11):
            await _rep_turn(agent)
        result = await _tool(agent, "task_complete")()
    assert isinstance(result, str)
    assert "Telehealth" in result
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py::test_run_b_shape_is_refused_after_eleven_turns -v
```

Expected: FAIL — `result` is an `Agent`, because `11 >= 5` bails.

- [ ] **Step 3: Snapshot the entry count**

In `PlanTaskAgent.__init__`, replace `self._completion_refused = False` with:

```python
        # Questions owed when the task was ENTERED. The ceiling must measure the same
        # window as `_rep_turns` (which accumulates over the whole task); comparing it
        # against the CURRENTLY outstanding count let an 11-turn task clear a 5-question
        # bar and hand off with six questions unasked.
        self._questions_at_entry = 0
        self._refusals = 0
        self._outstanding_at_last_refusal: int | None = None
```

In `on_enter`, after `_apply_gating()` (so gated-out questions are excluded) and before the opening line:

```python
        self._questions_at_entry = self._controller.owed_question_count(self._task_index)
```

- [ ] **Step 4: Rewrite the guard**

Add the budget as a module constant beside the existing `_GAP_FRUITLESS_REFUSALS`, matching that
style:

```python
# Consecutive task_complete refusals that shrink nothing before the guard gives up.
_TASK_FRUITLESS_REFUSALS = 2
```

```python
    def _refuse_premature_completion(self) -> str | None:
        """Send the agent back for this task's still-open required questions.

        Two bounds, because a rep who cannot answer never empties `gap_fields` and an
        unconditional guard would strand the plan on this task:

        * a turn ceiling measured over the SAME window on both sides — rep turns across the
          task against the questions owed when the task was entered. N questions cannot be
          asked in fewer than N exchanges;
        * a refusal budget, since the Observer extracts in a detached pass and the answer to
          the task's last question is never on file here. Progress — the outstanding set
          shrank — resets it, so a task still landing answers keeps its runway.
        """
        outstanding = self._controller.gap_fields(self._task_index)
        if not outstanding:
            return None
        if self._rep_turns >= self._questions_at_entry:
            return None
        if self._refusals >= _TASK_FRUITLESS_REFUSALS:
            logger.info(
                "task %s advancing with %d question(s) still open",
                self._task.task_key,
                len(outstanding),
            )
            return None
        shrank = (
            self._outstanding_at_last_refusal is None
            or len(outstanding) < self._outstanding_at_last_refusal
        )
        self._refusals = 0 if shrank else self._refusals + 1
        self._outstanding_at_last_refusal = len(outstanding)
        logger.info(
            "task %s: completion refused, %d required question(s) still open",
            self._task.task_key,
            len(outstanding),
        )
        return (
            "Not yet — these required questions of the current task have no answer on file. "
            "Ask the representative for them now (one at a time), and call task_complete once "
            "they are answered or the representative says they cannot answer:\n"
            f"{_field_lines(outstanding)}"
        )
```

- [ ] **Step 5: Run the new test and the guard suite**

```bash
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py -k "refus or complete" -v
```

Expected: the new test PASSES. Two existing tests pin the deleted behaviour and **must be rewritten, not deleted**:

- `test_a_task_that_walked_its_questions_is_not_refused` (`:1483`) — still valid in intent. Its plan's task owes 1 question and takes 1 turn, so `1 >= 1` still trusts it. Confirm it passes unchanged; if it does not, the entry snapshot is being taken in the wrong place.
- `test_a_second_task_complete_advances_even_with_questions_still_open` (`:1454`) — the **no-deadlock property**. It now needs three `task_complete` calls rather than two (budget of 2). Update the count and keep the assertion that the call eventually advances. **Do not weaken this test** — a guard that can never be escaped strands live calls.

- [ ] **Step 6: Run the full gate**

```bash
just check
```

- [ ] **Step 7: Run `/simplify`, then re-run `just check`**

- [ ] **Step 8: Commit**

```bash
git add apps/agent_worker/src/agent_worker/plan_runtime.py \
        apps/agent_worker/tests/unit/test_plan_runtime.py
git commit -m "fix(worker): measure the completion ceiling over one window, not two

_rep_turns accumulates across the whole task while owed_question_count
measured only what was outstanding right now, so an 11-turn task cleared a
5-question bar. Both sides now measure the whole task, and a bounded refusal
budget replaces refuse-at-most-once."
```

---

### Task 5: The completion contract — regression suite

Tasks 3 and 4 changed *when* a task may finish. These pin the behaviours that must **not** change, so a later tuning pass cannot quietly break them. No production code — if a test here fails, the fix belongs in Task 3 or 4.

**Files:**
- Test: `apps/agent_worker/tests/unit/test_plan_runtime.py`

**Interfaces:**
- Consumes: `owed_now`, `gap_fields`, `owed_question_count` (Task 3); the repaired guard (Task 4); `_ibv_plan`, `_task_index`, `_RUN_B_ANSWERS` (Task 3's helpers).
- Produces: nothing.

- [ ] **Step 1: Pin that a gated-out question is not owed (the Run A shape)**

```python
def test_an_hmo_plan_owes_the_referral_question_and_a_ppo_plan_does_not() -> None:
    """Completion must be allowed with a gated-out question unasked — the correct
    behaviour the broken guard also produced, and which must survive the fix."""
    controller, _ = _controller(_ibv_plan())
    index = _task_index(controller, "insurance_basics")
    hmo = dict(_RUN_B_ANSWERS) | {_S + "insurance_information.plan_type": "HMO"}
    controller.update_answers(hmo)
    assert "pcp_referral_required" in {f.path.split(".")[-1] for f in controller.gap_fields(index)}
    controller.update_answers(dict(_RUN_B_ANSWERS))  # PPO
    assert "pcp_referral_required" not in {
        f.path.split(".")[-1] for f in controller.gap_fields(index)
    }
```

- [ ] **Step 2: Pin that a fully answered task is never refused**

```python
@pytest.mark.asyncio
async def test_a_task_with_every_question_answered_completes_immediately() -> None:
    """No spurious refusal: a task owing nothing hands off on the first call, whatever
    the turn count."""
    controller, _ = _controller(_ibv_plan())
    index = _task_index(controller, "insurance_basics")
    answers = dict(_RUN_B_ANSWERS) | {
        _S + "benefit_coverage." + key: "X"
        for key in (
            "plan_effective_date",
            "plan_year_information",
            "telehealth_covered",
            "plan_fund_type",
            "employer_support_size",
            "infertility_plan_mandate",
        )
    }
    controller.update_answers(answers)
    agent = controller.agents[index]
    with _session_patch(agent, MagicMock()):
        await agent.on_enter()
        assert isinstance(await _tool(agent, "task_complete")(), Agent)
```

- [ ] **Step 3: Pin the termination rule — early completion IS permitted**

```python
def test_a_termination_rule_still_ends_the_call_early() -> None:
    """Both networks No and out-of-network No fires the flow rule; the completion work
    must not interfere with a deliberate early end."""
    engine = RuleEngine(_ibv_plan())
    directive = engine.evaluate(
        {
            _S + "insurance_information.doctor_inside_network": "No",
            _S + "insurance_information.facility_inside_network": "No",
            _S + "insurance_information.out_of_network_coverage": "No",
        }
    )
    assert isinstance(directive, Terminate)
```

`RuleEngine(plan: CallPlan)` and `evaluate(answers) -> Directive | None` — construct it directly rather than reaching into the controller; the assertion is about the rule firing, not about plumbing.

- [ ] **Step 4: Pin the small-group / self-insured consistency check**

```python
def test_small_group_plus_self_insured_still_fires_the_consistency_rule() -> None:
    engine = RuleEngine(_ibv_plan())
    directive = engine.evaluate(
        {
            _S + "benefit_coverage.employer_support_size": "Small Group",
            _S + "benefit_coverage.plan_fund_type": "Self Insured",
        }
    )
    assert getattr(directive, "rule_key", None) == "small_group_self_insured_conflict"
```

- [ ] **Step 5: Pin the sweep's position**

```python
def test_the_gap_sweep_still_fires_only_before_the_last_task() -> None:
    """The sweep's position is deliberately unchanged (spec §5). A later refactor that
    moves or repeats it must fail here, not on a live call."""
    controller, _ = _controller(_ibv_plan())
    assert controller._closing_task_index == len(controller.plan.tasks) - 1
```

- [ ] **Step 6: Pin the sweep's corrected input**

```python
def test_the_sweep_sees_a_question_whose_only_gap_carries_a_default() -> None:
    """telehealth_covered carries default="N/A", so `is_satisfied` hid it from every
    guard AND from the sweep. This is the sweep's whole behaviour change."""
    controller, _ = _controller(_ibv_plan())
    index = _task_index(controller, "insurance_basics")
    answers = dict(_RUN_B_ANSWERS) | {
        _S + "benefit_coverage." + key: "X"
        for key in (
            "plan_effective_date",
            "plan_year_information",
            "plan_fund_type",
            "employer_support_size",
            "infertility_plan_mandate",
        )
    }
    controller.update_answers(answers)
    assert [f.path.split(".")[-1] for f in controller.gap_fields(index)] == [
        "telehealth_covered"
    ]
```

- [ ] **Step 7: Run them**

```bash
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py -v
```

Expected: all PASS. Any failure here means Task 3 or Task 4 is wrong — **fix the production code, not the assertion.**

- [ ] **Step 8: Run the full gate**

```bash
just check
```

- [ ] **Step 9: Commit**

```bash
git add apps/agent_worker/tests/unit/test_plan_runtime.py
git commit -m "test(worker): pin the completion contract

Gated-out questions stay unowed, a complete task never refuses, the
termination and consistency rules still fire, the sweep keeps its position,
and it now sees a default-carrying gap."
```

---

### Task 6: Drain extraction before the sweep decides

Independent of Tasks 1–5 — it fixes phantom gaps under today's logic too, so it may be pulled forward.

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/observer.py:425-433` (`_close_retiring`, `_schedule_close`)
- Modify: `apps/agent_worker/src/agent_worker/plan_runtime.py:487-499` (`GapTaskAgent.on_enter`)
- Test: `apps/agent_worker/tests/unit/test_observer.py`, `apps/agent_worker/tests/unit/test_plan_runtime.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ObserverManager.drain_pending(timeout: float = 10.0) -> None` — awaits the retiring observer's final extraction pass and any in-flight closes, returning early on timeout. `PlanRunController.drain_observer() -> None` forwards to it, or is a no-op when no manager is attached (Voice Lab, tests).

- [ ] **Step 1: Write the failing test**

Use the module's existing helpers (`_manager`, `_plan`, `_field`, `_rep`, `_settle`, `FakeExtractor`). Add one slow extractor beside `FakeExtractor`:

```python
class SlowExtractor(FakeExtractor):
    """`FakeExtractor` that takes `delay` seconds, so a drain has something to wait for."""

    def __init__(self, answers: list[ExtractedAnswer], *, delay: float) -> None:
        super().__init__(answers)
        self.delay = delay

    async def extract(self, task: Any, transcript: str) -> list[ExtractedAnswer]:
        await asyncio.sleep(self.delay)
        return await super().extract(task, transcript)


class TestDrainPending:
    @pytest.mark.asyncio
    async def test_drain_awaits_the_retiring_observers_final_pass(self) -> None:
        """A rep answer finalized in the last turn before a handoff is extracted by the
        retiring observer's drain. The sweep must wait for it, or it re-asks a question
        the rep has just answered."""
        extractor = SlowExtractor([ExtractedAnswer("sections.a.b", "Yes", 90)], delay=0.05)
        manager, run_state, _bus, _controller = _manager(_plan([_field("sections.a.b")]), extractor)
        manager.ingest(_rep("yes it is"))
        manager._rotate(None)  # task ended; the outgoing observer retires
        await manager.drain_pending(timeout=5.0)
        assert run_state.records, "the final extraction pass had not completed when drain returned"

    @pytest.mark.asyncio
    async def test_drain_returns_on_timeout_instead_of_stalling(self) -> None:
        """The barrier must never become a hang: a slow extractor falls through."""
        extractor = SlowExtractor([ExtractedAnswer("sections.a.b", "Yes", 90)], delay=5.0)
        manager, _run_state, _bus, _controller = _manager(
            _plan([_field("sections.a.b")]), extractor
        )
        manager.ingest(_rep("yes it is"))
        manager._rotate(None)
        await asyncio.wait_for(manager.drain_pending(timeout=0.05), timeout=1.0)
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest apps/agent_worker/tests/unit/test_observer.py -k drain_pending -v
```

Expected: FAIL with `AttributeError: 'ObserverManager' object has no attribute 'drain_pending'`.

- [ ] **Step 3: Implement `drain_pending`**

```python
    async def drain_pending(self, timeout: float = 10.0) -> None:
        """Await the retiring observer's final pass and any in-flight closes.

        The gap sweep decides what is still owed; extraction lands ~15s after the turn that
        produced it (p90 23s), so without this the sweep re-asks the last question the rep
        just answered. Bounded and best-effort: on timeout the sweep proceeds on whatever
        landed, because a barrier that can hang is worse than a stale answer set."""
        self._close_retiring()
        if not self._closing:
            return
        try:
            async with asyncio.timeout(timeout):
                while self._closing:
                    await asyncio.gather(*list(self._closing), return_exceptions=True)
        except TimeoutError:
            logger.warning("observer manager %s: drain timed out", self._room)
```

- [ ] **Step 4: Await it before the sweep computes gaps**

In `plan_runtime.py`, `GapTaskAgent.on_enter`, before `gap_fields`:

```python
    async def on_enter(self) -> None:
        self._controller.note_task_entered(self._task_index)
        if takeover_engaged(self.session):
            return
        # The preceding task's outro is playing ("let me take a quick moment to review my
        # notes... one moment please"), so this barrier costs the caller no extra silence.
        await self._controller.drain_observer()
        fields = self._controller.gap_fields(self._task_index)
        ...
```

Add to `PlanRunController`:

```python
    async def drain_observer(self) -> None:
        """Let extraction settle before a caller reads `gap_fields`. No-op without a manager."""
        if self._observer_manager is not None:
            await self._observer_manager.drain_pending()
```

Wire `_observer_manager` in the same place `attach_session` is wired from `main.py`; default it to `None` in `__init__` so every existing test and the Voice Lab path keep working untouched.

- [ ] **Step 5: Run the tests**

```bash
uv run pytest apps/agent_worker/tests/unit/test_observer.py apps/agent_worker/tests/unit/test_plan_runtime.py -v
```

Expected: PASS.

- [ ] **Step 6: Run the full gate**

```bash
just check
```

- [ ] **Step 7: Boot the worker and watch a task handoff**

Per the repo rule that a change to a long-lived async path is not verified by pytest alone:

```bash
just up && just worker
```

Confirm no `drain timed out` warnings on an idle boot and that the worker reaches its ready state.

- [ ] **Step 8: Run `/simplify`, then re-run `just check`**

- [ ] **Step 9: Commit**

```bash
git add apps/agent_worker/src/agent_worker/observer.py \
        apps/agent_worker/src/agent_worker/plan_runtime.py \
        apps/agent_worker/tests/unit/
git commit -m "fix(worker): settle extraction before the gap sweep decides what is owed

Extraction lands ~15s after the turn that produced it, so the sweep could see
a phantom gap for the last answer and re-ask it. The sweep now awaits the
retiring observer's final pass, bounded and best-effort, while the preceding
task's outro is still playing."
```

---

### Task 7: Per-turn tool idempotence (P10)

Filed 2026-07-30, never implemented. Did **not** cause the trace under investigation (6 tool calls produced 6 handoffs), but it is real and cheap.

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/plan_runtime.py:332-336` (`PlanTaskAgent.on_user_turn_completed`), `:348-365` (`_task_complete`), `:511-519` (`GapTaskAgent.on_user_turn_completed`), `:530-537` (`_gap_complete`)
- Test: `apps/agent_worker/tests/unit/test_plan_runtime.py`

**Interfaces:**
- Consumes: nothing.
- Produces: no public surface. Both agents gain `_advanced_this_turn: bool`, cleared in `on_user_turn_completed`.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_two_task_completes_in_one_turn_produce_one_handoff() -> None:
    """A doubled tool call walked straight past a task without entering its question
    loop (P10, 2026-07-30). The second call must be inert."""
    controller, _ = _controller(_gap_plan())
    controller.update_answers({"sections.intro.rep_name": "Pat"})
    agent = controller.agents[0]
    with _session_patch(agent, MagicMock()):
        first = await _tool(agent, "task_complete")()
        second = await _tool(agent, "task_complete")()
    assert isinstance(first, Agent)
    assert isinstance(second, str)
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py::test_two_task_completes_in_one_turn_produce_one_handoff -v
```

Expected: FAIL — `second` is an `Agent`, so the chain advanced twice.

- [ ] **Step 3: Add the guard to both agents**

In each `__init__`: `self._advanced_this_turn = False`.

In each `on_user_turn_completed`, alongside the existing `self._rep_turns += 1`:

```python
        self._advanced_this_turn = False
```

At the top of `_task_complete` and `_gap_complete`, immediately after the `takeover_engaged` early return (the same shape, deliberately):

```python
        if self._advanced_this_turn:
            # A second chain-advancing call in one turn traverses a task without ever
            # entering its question loop. Inert, not a second Agent.
            return "Already moving on — continue with the next question."
```

and set `self._advanced_this_turn = True` immediately before returning the successor.

- [ ] **Step 4: Run the tests**

```bash
uv run pytest apps/agent_worker/tests/unit/test_plan_runtime.py -k "handoff or complete" -v
```

Expected: PASS. `test_a_second_task_complete_advances_even_with_questions_still_open` exercises repeated calls **without** intervening turns — it must now interleave `await _rep_turn(agent)` between calls, since that is what a real retry looks like. Update it accordingly and keep its assertion.

- [ ] **Step 5: Run the full gate**

```bash
just check
```

- [ ] **Step 6: Run `/simplify`, then re-run `just check`**

- [ ] **Step 7: Commit**

```bash
git add apps/agent_worker/src/agent_worker/plan_runtime.py \
        apps/agent_worker/tests/unit/test_plan_runtime.py
git commit -m "fix(worker): a repeated chain-advancing tool call in one turn is inert (P10)

Two task_complete calls in a turn produced two handoffs, traversing a task
without entering its question loop."
```

---

### Task 8: Completion observability

The trace recorded 233 answers and exposed zero field paths; what had been recorded had to be reverse-engineered from raw LLM completions on descendant spans.

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/observer.py:439-497` (`_record_locked`)
- Modify: `apps/agent_worker/src/agent_worker/plan_runtime.py:348-365` (`_task_complete`)
- Test: `apps/agent_worker/tests/unit/test_observer.py`

**Interfaces:**
- Consumes: `gap_fields` / `owed_question_count` from Task 3.
- Produces: span `vera.observer.answer_recorded` with attributes `vera.field.path`, `vera.field.confidence`, `vera.task.key`; attributes `vera.completion.owed_count` and `vera.completion.refused` on the existing `task_complete` span.

- [ ] **Step 1: Write the failing test**

Use the module's existing helpers plus the in-memory OTel helper in
`vera_core.observability.otel_testing` (read it first for the exact collector API — other tests in
`tests/unit/observability/` already use it):

```python
@pytest.mark.asyncio
async def test_a_recorded_answer_emits_a_span_naming_the_path_but_not_the_value() -> None:
    extractor = FakeExtractor([ExtractedAnswer("sections.a.b", "Yes", 90)])
    manager, _run_state, _bus, _controller = _manager(_plan([_field("sections.a.b")]), extractor)
    with collected_spans() as spans:
        await _feed(manager, _rep("yes it is"))
    span = next(s for s in spans if s.name == "vera.observer.answer_recorded")
    assert span.attributes["vera.field.path"] == "sections.a.b"
    assert span.attributes["vera.field.confidence"] == 90
    assert "Yes" not in str(span.attributes)  # PHI must never reach a span
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest apps/agent_worker/tests/unit/test_observer.py -k answer_recorded -v
```

Expected: FAIL with `StopIteration` — no such span exists.

- [ ] **Step 3: Emit the span**

In `_record_locked`, after the write and emit land and `self._answers` is updated:

```python
        # Path, confidence and task only — never the value (PHI). Both knobs off because
        # this span's body sits beside raw extracted values.
        with tracer.start_as_current_span(
            "vera.observer.answer_recorded",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            attrs: dict[str, str | int] = {"vera.field.path": answer.field_path}
            if self._active_index is not None:
                attrs["vera.task.key"] = self._plan.tasks[self._active_index].task_key
            if answer.confidence is not None:
                attrs["vera.field.confidence"] = answer.confidence
            span.set_attributes(attrs)
```

In `_task_complete`, before returning, tag the decision onto the current span alongside the existing `_tag_task_complete_handoff` attributes:

```python
                    "vera.completion.owed_count": len(
                        self._controller.gap_fields(self._task_index)
                    ),
                    "vera.completion.refused": refusal is not None,
```

Restructure `_task_complete` so `refusal` is in scope at the tagging point; keep the early return behaviour identical.

- [ ] **Step 4: Run the tests**

```bash
uv run pytest apps/agent_worker/tests/unit/test_observer.py apps/agent_worker/tests/unit/test_plan_runtime.py -v
```

- [ ] **Step 5: Run the full gate**

```bash
just check
```

The PHI guardrail hook (`.claude/hooks/phi_guardrails.py`) will inspect these edits. If it flags a line, the fix is to remove the value — never to bypass the hook.

- [ ] **Step 6: Run `/simplify`, then re-run `just check`**

- [ ] **Step 7: Commit**

```bash
git add apps/agent_worker/src/agent_worker/observer.py \
        apps/agent_worker/src/agent_worker/plan_runtime.py \
        apps/agent_worker/tests/unit/test_observer.py
git commit -m "feat(observability): trace which answers were recorded and what completion owed

A call recorded 233 answers and exposed zero field paths, so a missed question
was invisible behind the model's confident reason string. Paths, confidence and
counts only — never values."
```

---

### Task 9: Verify on a real call

`pytest` asserts on strings; the defect lived in a live conversation. This task has no code.

**Files:** none.

**Interfaces:**
- Consumes: Tasks 1–8.
- Produces: a go/no-go on the branch.

- [ ] **Step 1: Run the eval harness**

```bash
VERA_EVALS_FULL=1 VERA_EVALS_ENABLED=1 uv run pytest apps/agent_worker/tests/evals -m evals -s -rs
```

`-m evals` is **required** — without it you get the LLM-free tests and no simulations, which looks like a clean pass. Confirm real scenarios ran by the `===== <scenario>: … =====` banners. Diff the scorecard against the pre-change run; a scenario reporting `0 answers extracted` proves nothing.

- [ ] **Step 2: Run a live call on browser-callee transport**

Set `VERA_BROWSER_CALLEE_TRANSPORT=true` on both `just api` and `just worker` (plus `VITE_BROWSER_CALLEE_TRANSPORT=true` on the frontend), then join from Live Monitoring as the payer rep. Runbook and the two constraints that bite (~60s join window, one tab per call) are in `README.md` → "Browser callee".

- [ ] **Step 3: Drive the Run B scenario deliberately**

Answer the plan-type question with a **non-HMO** value. Then assert, from the call and the trace:

1. `insurance_basics` does **not** hand off until `plan_effective_date`, `plan_year_information`, `telehealth_covered`, `plan_fund_type`, `employer_support_size` and `infertility_plan_mandate` have all been asked.
2. `pcp_referral_required` is **not** asked (correctly gated out).
3. No question is asked twice — in particular the gap sweep does not re-ask the last answer of the preceding task (Task 5's barrier).
4. `vera.observer.answer_recorded` spans are present and carry paths, not values.

- [ ] **Step 4: Record the outcome in the spec**

Append a short "Live verification" section to `docs/superpowers/specs/2026-08-09-lossless-call-plan-completion-design.md` with the trace ID and what it showed — pass or fail. A failure here is information, not a reason to hide the result.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-08-09-lossless-call-plan-completion-design.md
git commit -m "docs(specs): record live verification of the completion fix"
```

---

## Out of scope — do not implement

Recorded in the spec with reasons; a worker who "helpfully" adds these is expanding scope:

- Flattening the spouse confirms into numbered items 12/13 (a prompt change needing its own live validation).
- Unifying the three applicability implementations.
- Moving or repeating the gap sweep.
- Schema cleanup of misused `default` (`pcp_referral_required` → `inapplicable_value`; dropping `default` on `group_name`, `group_number`, `policy_situs`, `telehealth_covered`, `enrollment_required`).
- `focus_call_plan` / focused retry / P7.
- Prompt caching, date read-back normalisation, raising `thinking_level`.
