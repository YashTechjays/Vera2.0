# Plan C — calibrate the gap pass and the completion guards to question units

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

> **STATUS (2026-08-07): DONE, scope narrowed.** Tasks 1, 3 and 6 shipped. Tasks 2, 4 and 5
> are **CLOSED — do not implement**; evidence below. The re-ask lists stay **field-granular**
> on purpose. Net shape: question-granular *turn ceilings*, field-granular *re-ask lists*.

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

### Task 1: `gap_questions` on the controller — **DONE**, shipped in two pieces

The tree walk went into `vera_core.forms.question_plan` beside `drop_questions`, whose
predicate it complements, rather than being hand-rolled in the worker:

```python
def owed_questions(panels: list[PromptPanel], paths: Collection[str]) -> list[PromptQuestion]
```

`PlanRunController.owed_question_count(task_index)` is the adapter over it, and returns a
**count** rather than a list — nothing needs the questions themselves once Task 2 is closed.
It adds back gaps no question targets (`len(owed) + len(outstanding - covered)`), so a task
with no compiled panels falls through to the plain field count with no special case, and
partial coverage does not silently lower the ceiling. Routing questions have no
`target_paths` and are never owed.

### Task 2: `_question_lines` replaces `_field_lines` — **CLOSED, do not implement**

**Rejected 2026-08-07** because it re-asks answers already on file. A question's targets are
answered *independently* by the Observer, so a partially-answered question rendered as
`1. <question text>` re-asks the part that landed.

Measured on a PBM question targeting `pbm_exists` + `pbm_name`, with `pbm_exists` extracted
and `pbm_name` missed. Field lines (shipped) give the gap sweep:

```
1 required question is still unanswered from earlier in the call. …
1. PBM Name
```

Question lines (this task) would instead give:

```
1. Is there a separate pharmacy benefit manager for this plan, and if so, what is its name?
```

— re-asking `pbm_exists`. That is a regression against what ships today, and partial
extraction of a multi-target question is the common case, not the edge case.

The over-asking this task was written to fix (eight `Copay ($)` lines for one CPT panel
question) is real but is the *lesser* cost, and `_owning_segment` already keeps those lines
distinguishable. Fixing both at once needs a third thing neither plan specifies: render the
question text **narrowed to its still-missing targets**. That is unbuilt — open it as its own
plan if the CPT batching is worth it, and do not reopen this task as written.

### Task 3: both refusal guards count questions — **DONE**

`_refuse_premature_completion` recomputes the ceiling **live** at refusal time; the guard's
`outstanding` list (what it re-lists to the agent) stays fields. Refuse-at-most-once-per-task
is unchanged.

`_refuse_premature_gap_complete` uses the **entry snapshot** `self._questions_owed`, now set
from `owed_question_count` in `on_enter`. The old `or len(outstanding)` fallback was dropped:
the takeover latch is one-way (`intervention.py` — "never reset") and there is no `await`
between the `gap_fields` and `owed_question_count` calls in `on_enter`, so the attribute can
never be 0 by the time the guard reads it. Both bounds and the `_outstanding_at_last_refusal`
progress check are unchanged.

The refusal log names its units — `%d field(s) open across %d owed ask(s)` — because the two
counts genuinely differ now; the old `%d of %d question(s)` would print "3 of 1".

Regression covered: a 3-target panel question, one rep turn answering all three →
`task_complete` is not refused. Before this plan it was.

### Task 4: `_apply_gap_list` keys on question identity — **CLOSED, do not implement**

Existed only to match Task 2's question-granular list. The list stays field-granular, so
`self._listed_paths` stays paths and the rebuild-not-append rule is untouched.

### Task 5: reconcile the existing tests — **CLOSED, not needed**

Premised on Task 2 changing what the lists render. `_INTAKE_GAPS` keeps its field titles
(`"Covered (cpt_58340)"`, `"Covered (cpt_82670)"`) because the gap block still renders fields;
no existing assertion needed changing.

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
