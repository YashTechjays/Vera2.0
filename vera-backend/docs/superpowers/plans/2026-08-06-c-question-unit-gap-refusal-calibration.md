# Plan C — calibrate the gap pass and the completion guards to question units

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** The gap sweep and both premature-completion guards must count **questions**, not
fields, so a panel question answered in one turn is not refused and re-asked N times.

**Architecture:** `gap_fields` keeps returning fields (completion maths, the Observer and the
retry path all need paths). A new `gap_questions(task_index)` maps those fields up to the
`PlanQuestion`s that ask them: a question is owed if **any** of its targets is
applicable ∧ required ∧ unanswered. Both turn ceilings and both re-ask lists switch to that.

**Tech Stack:** Python 3.12, pytest + pytest-asyncio.

## Global Constraints

- `PlanTask.fields` and `gap_fields` keep their current semantics — nothing downstream of them
  changes.
- `gap_fields` evaluates **full** gate chains, not Plan A's entry-decidable subset: at end of
  call an intra-task gate is decided, which is exactly what the sweep needs.
- Never log a field value.
- PEP 695 type params. `asyncio` only.

**Depends on:** Plan B (`PlanTask.questions`). **Must ship with it or immediately after** — see
below.

---

## Why this is not optional after Plan B

Measured today, for the diagnostic task with 9 unanswered fields, `_gap_block` renders:

```
9 required questions are still unanswered … Re-ask ONLY the questions on this numbered list …
1. Covered (cpt_58340)      2. Copay ($) (cpt_58340)      3. Coinsurance (%) (cpt_58340)
4. Prior Authorization Required (cpt_58340)               5. Covered (cpt_82670)
6. Copay ($) (cpt_82670)    7. Coinsurance (%) (cpt_82670) …
Keep each question brief, but do not shorten the LIST — every question on it is owed.
```

Eight `Copay ($)` items for what Plan B asks once. Two concrete failures:

1. **Over-asking.** "do not shorten the LIST — every question on it is owed" makes the sweep
   re-ask one panel question up to 8 times.
2. **Inverted turn ceilings.** `_refuse_premature_completion` returns early only when
   `self._rep_turns >= len(outstanding)` (`plan_runtime.py:343`);
   `_refuse_premature_gap_complete` on `self._rep_turns >= owed` where
   `owed = self._questions_owed` (`:455`, `:519-520`). One panel question answered in one rep
   turn leaves 8 paths open, so the guard refuses completion and forces a re-ask — the guard
   fights the merge. `_GAP_FRUITLESS_REFUSALS = 2` bounds the loop, so it degrades rather than
   hangs, at the cost of two spurious re-asks per panel.

This is already latent today: `diagnostic_testing` authors its 4 ask groups, so a rep who
answers the panel in one breath trips the same ceiling. Plan B turns that edge case into the
normal path.

---

## File Structure

- **Modify** `apps/agent_worker/src/agent_worker/plan_runtime.py`
  - `_gap_block(title, fields)` → `_gap_block(title, questions)`
  - `_field_lines(fields, numbered=…)` → `_question_lines(questions, numbered=…)`; the
    duplicate-title qualifier (`_owning_segment`) is no longer needed — a question's text is
    already unique and speakable
  - new `PlanRunController.gap_questions(task_index) -> list[PlanQuestion]`
  - `GapTaskAgent._questions_owed` / `_apply_gap_list` / `_refuse_premature_gap_complete` take
    questions
  - `PlanTaskAgent._refuse_premature_completion` takes questions
  - `_next_gap_task` keeps using `gap_fields` (any owed field means the task needs a sweep)
- **Modify** `apps/agent_worker/tests/unit/test_plan_runtime.py` — `_INTAKE_GAPS` and every
  assertion that matches a bare field title

**Interfaces:**

- Consumes: `PlanQuestion` from Plan B (`text`, `target_paths`, `gates`, `panel_title`, `codes`).
- Produces:

```python
def gap_questions(self, task_index: int) -> list[PlanQuestion]:
    """Questions still owed: those with at least one target that is applicable, required and
    unanswered. Document order, deduped."""
```

---

### Task 1: `gap_questions` on the controller

Map `gap_fields(task_index)` paths onto `plan.tasks[task_index].questions`; a question is owed
if any target path is in that set. Preserve document order; never emit a question twice.

Test: a plan with one 3-target panel question where 2 targets are unanswered yields **one**
owed question, not two. A plan with two independent single-target questions yields two.

### Task 2: `_question_lines` replaces `_field_lines`

Render `1. <question text>` and, where the schema fixes them, the expected values from the
question's first target's descriptor. Drop `_owning_segment` qualification — the question text
names its own subject, which is why the old `Covered (cpt_58340)` shape existed.

Keep the `numbered=True` ordinal behaviour and its reason: a run of near-identical lines
carries no signal that N is how many were owed.

### Task 3: both refusal guards count questions

`_refuse_premature_completion`: `outstanding = self._controller.gap_questions(self._task_index)`;
ceiling `self._rep_turns >= len(outstanding)`. Keep the refuse-at-most-once-per-task design and
its docstring reason (a rep who cannot answer never empties the set, so an unconditional guard
would strand the plan).

`_refuse_premature_gap_complete`: same substitution; `self._questions_owed = len(questions)` in
`on_enter`. Keep both bounds (turn ceiling + `_GAP_FRUITLESS_REFUSALS`) and the
`_outstanding_at_last_refusal` progress check.

Test the specific regression: a 3-target panel question, one rep turn that answers all three →
`task_complete` is **not** refused. Before this plan it is.

### Task 4: `_apply_gap_list` keys on question identity

`self._listed_paths` becomes `self._listed_questions: tuple[str, ...]` of question texts (or a
stable index). The rebuild-not-append rule stays — the reason in `_apply_gating`'s docstring
applies here too.

### Task 5: reconcile the existing tests

`_INTAKE_GAPS` currently lists `"Covered (cpt_58340)"`, `"Covered (cpt_82670)"`. With
`PlanTask.questions` populated on the fixture, those become one question. Either add
`questions=` to the fixture plans or assert on question text. Prefer adding `questions=` —
it exercises the real shape.

### Task 6: verify

`just check`; `/simplify`; `just check`. Then the eval harness — the gap pass is what the eval
scenarios exercise most directly — then a live call where the rep answers a whole CPT panel in
one sentence and then cannot answer one later question. Expected: exactly one re-ask of the
unanswered one, no re-ask of the panel.

---

## Out of scope

- Changing which fields count as owed (`gap_fields`' applicable ∧ required ∧ unanswered rule is
  the same set the form's completion percentage counts — leave it).
- `_next_gap_task`'s visited-tasks restriction and the once-only sweep placement.
