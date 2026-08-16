# Plan D — narrow the spoken question list on a focused retry (defect P7)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** A focused retry must ask only the still-missing questions. Today it narrows what the
plan *tracks* but not what VERA *asks*.

**Architecture:** `focus_call_plan` gains the question list as a filter target and re-renders
each task's prompt from the kept questions. The dispatcher already holds the `FormSchemaDoc` two
lines above the call site, so the re-render needs no new plumbing.

**Tech Stack:** Python 3.12, pydantic v2, pytest.

## Global Constraints

- The payer rep must **never** be told a prior call happened. Narrowing is done by removing
  questions, never by adding retry language.
- `focus_call_plan` must stay pure — it returns a copy, never mutates the staged plan.
- Never log a field value.

**Depends on:** Plan B (`PlanTask.questions` + a renderer that can take a path filter).

---

## Why: already filed, still open

This is **P7** in `2026-07-30-call-flow-eval-findings-remediation.md`, found by the eval
harness in run 2 and not yet fixed (no commit references it). Independently reproduced:

```
1 missing copay -> expand_to_groups gives 32 paths
   focused diagnostic task: 32 fields kept
   focused diagnostic task PROMPT length: 11539 chars   <-- unnarrowed
   full  diagnostic task PROMPT length: 11539 chars
```

`focus_call_plan` (`call_plan.py:193-211`) does only
`task.model_copy(update={"fields": kept})`. Its own docstring claims the opposite: *"The agent
then asks ONLY the still-missing data points."*

It is worse than a no-op. `_gating_block` returns `""` when nothing is excluded, and on a
focused retry `inapplicable_fields` is the complement *within the already-narrowed* set — so
the dropped questions appear in **neither** list, while `SCOPE_DISCIPLINE` tells the agent
"that list is the complete set of questions for this call". The narrowing is expressed nowhere
the model can act on.

**The alignment that makes this cheap.** `expand_to_groups` (`review.py:341`) matches *every*
ancestor group, so one missing copay pulls in the whole treatment/labs group — 32 paths. It
already focuses at **panel** granularity. Plan B makes the prompt's unit the panel too, so
after this plan "one copay missing" becomes "re-ask this service's 3 panel questions" instead of
handing the agent 32 flat items.

---

## File Structure

- **Modify** `packages/vera_core/src/vera_core/forms/call_plan.py` — `focus_call_plan` signature
  and body
- **Modify** `packages/vera_core/src/vera_core/services/queue_dispatcher.py:405` — pass the doc
- **Test:** `tests/unit/forms/test_call_plan.py` (or wherever `focus_call_plan` is covered —
  `grep -rn focus_call_plan tests/`)

**Interfaces:**

- Consumes: `PlanTask.questions` (Plan B), `render_task_prompts`' filtered form (Plan B Task 5
  must expose the question-set filter — coordinate if both are in flight).
- Produces:

```python
def focus_call_plan(
    doc: FormSchemaDoc, plan: CallPlan, paths: Collection[str]
) -> CallPlan: ...
```

Note the **signature change** — `doc` is a new first parameter. `queue_dispatcher.py:405` is the
only production caller.

---

### Task 1: filter `questions` alongside `fields`

Keep a question when at least one of its `target_paths` is in the focus set. A question with no
targets (Plan B's routing question) is kept only when any of the panels it routes between
survives. Test that `bookend_paths`' greeting/wrap-up preservation still holds.

### Task 2: re-render each kept task's prompt from its kept questions

Re-render rather than string-edit. Preserve the existing focus behaviour exactly:
`known_information` is kept (context the agent needs), `on_file_values` is cleared (it drives
read-back confirmations, precisely the "re-verify everything" a focused retry must avoid), and a
kept confirm-role field degrades to a plain ask.

Test: after focusing to one diagnostic copay, the rendered task prompt contains that service's
panel questions and **does not** contain a question for a service that was fully answered.

### Task 3: thread the doc through the dispatcher

`queue_dispatcher.py:405`:

```python
staged_plan = (focus_call_plan(doc, plan, focus), plan_prompt_version_id)
```

`doc` is already in scope. Update the docstring at `call_plan.py:193` so it stops describing
behaviour the function did not have.

### Task 4: verify

`just check`; `/simplify`; `just check`. Then the eval harness — scenario 2 is focused and is
what surfaced P7:

```bash
VERA_EVALS_FULL=1 VERA_EVALS_ENABLED=1 uv run pytest apps/agent_worker/tests/evals -m evals -s -rs
```

Expected: the focused scenario shows VERA asking only the kept questions. Per the P7 entry this
also clears the false `scope_discipline` failure described there as H3.

Then a real retry: queue a form, let a first call capture a reference number but miss one
service's copay, let the retry dispatch. VERA must open with a greeting, ask that service's
panel, capture her own rep name + reference number, and ask nothing else.

---

## Out of scope

- Changing `expand_to_groups`' expansion rule. Its panel granularity is what makes this plan
  read well; leave it.
- The FRESH-vs-FOCUSED decision (`has_call_reference`) and the retry floor.
- Telling the agent it is a retry — explicitly forbidden.
