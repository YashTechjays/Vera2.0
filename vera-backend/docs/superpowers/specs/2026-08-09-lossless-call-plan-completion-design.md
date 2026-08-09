# Lossless CallPlan — one derivation for what is asked and what is owed

**Date:** 2026-08-09
**Branch context:** `fix/prompt-compiler`
**Supersedes the open half of:** `2026-07-30-call-flow-eval-findings-remediation.md` (P8, P10, P1)

## Summary

The voice agent ends a task while required questions remain unasked. The cause is not a
prompt weakness and not a bad heuristic: **the question list the agent is told to ask and the
owed set the completion guard enforces are different quantities, derived from different
artifacts, by different rules.** They cannot be made to agree by tuning either one.

This spec makes the compiled `CallPlan` **lossless with respect to the schema**, then derives
the owed set from the same object the prompt was rendered from. Downstream, the completion
heuristic is repaired at its window rather than tuned at its threshold, and the end-of-call gap
sweep — which keeps its current position — starts sweeping the questions it could never see.

---

## Evidence

### The live call

Langfuse trace `6e1f496cb72d0182af27281c90bdca64` (2026-08-08, `gemini-3.6-flash`,
`thinking_level=minimal`). Task `insurance_basics`, 09:56:58 → 09:59:00.

The agent asked 10 questions and called `task_complete`. Seven required questions were never
asked. `plan_type` resolved to a non-HMO value, so `pcp_referral_required` was correctly gated
out; the other six were not.

```
never asked : plan_effective_date, plan_year_information, telehealth_covered,
              plan_fund_type, employer_support_size, infertility_plan_mandate
correctly gated out : pcp_referral_required
```

All six `task_complete` calls in the trace carry `lk.function_tool.is_error=false` and a
`vera.handoff.*` triple. **The guard never fired.** No `ERROR` or `WARNING` spans anywhere.

### Reproduced deterministically

Replaying that answer state against the real compiled plan:

```
prompt says              : 1 to 16          # what the agent is told to ask
applicable fields        : 18
gap_fields (today)       : 5
owed_question_count      : 5
rep turns in the trace   : 11
GUARD BAILS? 11 >= 5     -> True
```

`_refuse_premature_completion` compares `_rep_turns` — which accumulates across the **whole
task** — against `owed_question_count`, which measures only what is outstanding **right now**.
Two different windows. The gap widens the longer a task runs, so the guard is weakest exactly
where it is needed most.

### The sets do not nest

```
spoken questions in prompt   : 16
fields in task.fields        : 20
paths reachable by a question: 18

QUESTIONS WHOSE TARGETS ARE ALL DEFAULTED  (spoken, but can never be owed)
  - 'What is the group name and group number?'
  - 'What is the policy state or contract state?'
  - 'Does this plan require a PCP referral?'
  - 'Does this plan cover telehealth services?'
```

`telehealth_covered` carries `default="N/A"`, and `is_satisfied` short-circuits on
`default is not None`. It is therefore **structurally invisible** to `gap_fields` — not at
`task_complete`, not in the end-of-call sweep. The same applies to `group_name`,
`group_number`, `policy_situs`, `pcp_referral_required` and `enrollment_required`
(the last gates the entire enrollment block behind it).

### The backstop was unreachable

`_closing_task_index = len(plan.tasks) - 1` = **8 = `wrap_up`**, which is correct — `wrap_up`
really is the last task. The sweep never ran because the call ended in `closing_admin`
(index 7): last agent turn 10:12:33, silence, `on_exit` 10:13:59. The callee hung up.

A single terminal sweep is voided by any early hangup: everything the per-task guard lets through
depends on the call surviving to the final task.

**Decision (2026-08-09): the sweep's position is NOT changed by this spec.** The response is to
make the per-task guard (§4) strong enough that little reaches the sweep at all, and to fix what
the sweep computes (§5) so what does reach it is actually seen. Moving or repeating the sweep is
recorded as debt, not done here.

### Extraction lag is structural

```
rep turn END -> extraction START   median 12.4s   p90 20.3s
extraction call duration           median  2.3s   p90  6.2s
inter-rep-turn spacing             median 12.4s   <- identical to the scheduling lag
```

Extraction for turn N lands around turn N+1. **At the instant a task ends, the last one or two
answers are never on file.** This is why the turn heuristic exists, and it is why a sweep
firing immediately at a task boundary would re-ask questions the rep just answered — already
recorded by the eval harness as `answer_handling FAIL [133]`.

### The worker has the evaluator but not the document

```
$ rg "FormSchemaDoc|load_document|schema_json|leaf_gates|build_question_plan" apps/agent_worker/src/
(no matches)

FormSchemaDoc json : 147,958 bytes
CallPlan json      : 186,152 bytes    <- already 26% larger
```

The worker imports `conditions.evaluate`, `is_applicable`, `is_required`, `is_satisfied` and
`dsl.Condition`, and evaluates conditions against live answers on the field side. It was never
handed a document. The stated reason — *"the worker is DB-free, with no document to render a
`Condition` against"* (`question_plan.py:69`, and again in `call_plan._exclusive_notes`) —
conflates **DB-free** with **document-free**.

We are not fixing this by shipping the document (see *Rejected alternatives*). We are fixing
the artifact that lost the information.

---

## Root cause

`compile_call_plan` emits **two parallel artifacts from one schema**, each discarding half of
what the DSL said:

| | `task.panels` (question tree) | `task.fields` (descriptor list) |
| --- | --- | --- |
| structure (fan-out, either/or, panels) | yes | no — flat |
| gates | **no — prose strings** | yes — `Condition` |
| required | no — prose | yes |
| default | absent | yes |
| **drives** | **the prompt** | **the completion guard** |

They are joined only by `PromptOption.target_paths`, a lossy one-directional bridge that
`owed_question_count` must invert at runtime.

Four duplications follow from that single loss:

1. **Applicability, evaluated three ways in three modules.**

   | when | function | rule | decides |
   | --- | --- | --- | --- |
   | dispatch | `question_plan._entry_decided` | task position only, no answers | is gate **prose printed** |
   | task entry | `PlanRunController._settled` / `_decided_false` | position **+** answered | does the question **survive the list** |
   | gap time | `conditions.is_applicable` | full evaluation, no decidability | is the field **owed** |

   Consistency is maintained by a docstring, not a type: *"the worker is never LESS decisive
   than this — and it must not be, because a gate decided here but not there is a question
   asked with its condition stated nowhere."*

2. **Either/or modeled twice** — as labelled options on one question in the tree, and as an
   `alternative_pairs` group with an `is_satisfied` rule on the field side.

3. **Question identity in three representations** — panels, fields, paths — with
   `owed_questions()` as a third mapping between them.

4. **`default` overloaded.** Its DSL meaning is *the value a field takes when not collected*
   (intake fill, alternatives fill, frontend inapplicable render). `gap_fields` reads it as
   *this question need not be asked*. That is drift from the DSL's own meaning, not a design
   choice — which is why no grammar change is required to fix it.

**The DSL grammar is not the defect.** `applicable_when`, `required`, `alternatives`,
`ask_groups` and `inapplicable_value` are expressive enough to answer every question asked
here. The derivation layer is where truth was lost.

---

## The rule this spec establishes

> **The compiled artifact must be lossless with respect to the schema. Pre-rendered prose is
> a cached render, never the truth. Any consumer decision that the DSL can express must be
> computed from the expression, not from its rendering.**

Enforced by a validator (below), not by convention.

---

## Design

### 1. Artifact changes — make the question tree a complete index

> **Revised 2026-08-09, before implementation.** An earlier draft put
> `gate: Condition` and `required` on `PromptQuestion`. **That design is wrong.** Measured
> against both catalogs, **11 questions in `ibv_standard` have target paths with different
> gate chains** — a fan-out such as *"What is the copay or coinsurance for IUI?"* spans three
> CPT codes, each gated on its **own** `covered` field:
>
> ```
> ibv_standard: multi-target questions -> same gate chain=20  DIFFERENT=11
> disease_only: multi-target questions -> same gate chain=0   DIFFERENT=0
> ```
>
> There is no single "gate of the question". Hoisting one would have to pick a representative
> path and would silently mis-gate the other targets. It would also **duplicate** gates that
> already live on `PlanFieldDescriptor`, recreating the very drift this spec exists to remove.

The gates never needed to **move**. They needed to be **joinable**. `PlanFieldDescriptor`
already carries `gates: tuple[Condition, ...]` and `required: bool | RequiredWhen` per path,
and the worker already evaluates them. What is missing is that the question tree is not a
*complete index* into those descriptors: some collectable paths are unreachable from any
question node, so a computation that walks the tree cannot see them.

So the only artifact change is: **every collectable path must be reachable from exactly one
question node.** Today exactly two are not — the spouse confirms, which reach the agent as
prose (§1 continued below). `gate_text` stays exactly as it is: display prose, never consulted for a
decision.

This is a substantially smaller change than the earlier draft, and strictly more correct: gates
stay in one place, and the per-target granularity that the CPT fan-outs actually require comes
for free.

`immediate_confirms` stop being prose. Today a confirm-role field reaches the agent as the
string `If "Coverage Type" is "Family": {{confirm:sections.patient_information.spouse_partner_name}}`
attached to another question — while the *same* condition exists machine-readably on the field
as `required=when=RefCondition(ref='family_coverage')`. They become real question nodes
carrying `target_paths` and a `gate`, so they are countable and gate-evaluable.

**Representation only — the rendered prompt does not change.** Becoming a node is about what the
runtime can compute; whether a node receives an **ordinal** is a separate renderer decision.
`render_panels` numbers from one shared counter and `numbered_questions` decides eligibility
(`0 if item.routes_between else 1`), so "a real node that is not numbered" is an established
shape here — routing questions already work exactly that way. The spouse confirms keep their
current nested rendering:

```
11. Is this an individual or a family policy? Please answer Individual or Family.
   - Answers: Individual | Family
   - Immediately after this answer:
     * If "Coverage Type" is "Family": ask — Can we also check the spouse on the plan? ...
     * If "Coverage Type" is "Family": ask — And what is the spouse's date of birth?
12. Does this plan require a PCP referral?
```

`TestPanelsMatchThePrompt` (`tests/unit/forms/test_call_plan.py`) must stay green through this
step — that is the proof the change is a true prompt no-op.

**One asymmetry, stated deliberately.** Routing questions are unnumbered *and never owed*;
these are unnumbered *but owed*. That is coherent — numbering is a rendering concern, owed-ness
is a state concern — and it means that on a Family policy the guard's entry count is **18** while
COMPLETENESS still reads "1 to 16". Correct, not a mismatch: the agent genuinely owes 18 asks
(16 numbered + 2 nested follow-ups), and §4 compares rep turns against **asks**, not ordinals.
It composes both ways — 16 numbered asks in 16 turns while skipping the spouse pair gives
`16 < 18` and refuses, which is the bug class; an Individual policy leaves the gate false, so the
pair is not owed and the count is 16 with no false refusal.

The confirm mechanism itself is unchanged: a prefilled value is read back for confirmation, and
the `ask` wording remains the fallback when prefill is absent.

**Flattening these into numbered items 12 and 13 is explicitly NOT part of this spec.** It is a
plausible follow-up — the trace shows the walk ending immediately on exit from this indented
block, and an ordinal would give the model a positional anchor — but it is a *hypothesis* about
the pointer loss, not the demonstrated cause (§4's guard bail is), and it changes spoken
behaviour, which pytest cannot judge. Bundling it here would land a prompt change alongside the
completion fix and destroy attribution. Run it afterwards as a deliberate experiment, gated on
the eval harness plus a live call.

### 2. `task.fields` becomes a derived projection

It stays — flat dot-notation, one entry per collectable leaf — because the Observer needs
exactly that to write `field_answer.field_path`, and that namespace is byte-identical to the
schema path across schema, conditions, intake, extraction and the UI.

It stops being a *parallel* artifact and becomes a *projection of the tree*, asserted total by
the validator. This is what makes the 16-vs-20 divergence unrepresentable rather than merely
fixed once.

### 3. The owed set: walk the tree, join the descriptors

The owed set becomes a **join** — question units from the tree, gates and requiredness from the
descriptors they point at:

```python
def owed_now(task, answers, shared) -> list[PromptQuestion]:
    """Questions still owed: question-granular, evaluated per target path."""
    by_path = {f.path: f for f in task.fields}
    owed = []
    for q in iter_questions(task.panels):
        if q.routes_between:
            continue                      # a routing question collects nothing
        targets = [by_path[p] for p in q.target_paths if p in by_path]
        live = [f for f in targets if is_applicable(f.gates, answers, shared)]
        if any(
            is_required(f, answers, shared) and not has_value(answers, f.path)
            for f in live
        ):
            owed.append(q)
    return owed
```

Per-target evaluation is what the CPT fan-outs require: the IUI copay question is owed while
**any** covered code still lacks a copay, and drops out once every applicable code is answered.

This needs **no new gate data on the artifact** — it uses `PlanFieldDescriptor.gates` and
`.required`, which already ship and are already evaluated by the worker. The tree contributes
question identity; the descriptors contribute truth. Validator rule 1 (§8) is what guarantees
the join is total.

**Unifying the three applicability implementations is no longer required for this fix and moves
to recorded debt.** The earlier draft made it a migration step; with the owed set computed as a
join, `is_applicable` is the only evaluator on this path, and the compile-time / task-entry
variants are untouched. Collapsing them stays worth doing — just not here, and not bundled with
a correctness fix.

Four consequences worth stating explicitly:

- **`default` is never consulted.** The `telehealth_covered` class of bug dissolves without a
  schema change and without a second predicate to keep in sync with `is_satisfied`.
  `is_satisfied` is left untouched, so completion percentage, export, auto-completion and
  intake are **unchanged** — this carries no product-visible risk.
- ~~**Either/or is satisfied structurally.**~~ **WRONG — corrected 2026-08-09 during
  implementation.** The claim was that because the tree models an alternatives set as one question
  with several options, "one answer satisfies the group" falls out for free. It does not.
  `owed_now`'s per-target rule is "owed while ANY applicable target is required and unanswered",
  which is correct for a **fan-out** (eight CPT codes each need their own answer) and wrong for an
  **either/or** (answering coinsurance must close copay). `PromptQuestion` carries no marker
  distinguishing the two — an `AskGroup` fan-out and an `alternatives` pair produce the identical
  shape — and `cost_pair` members are unconditionally `required=True` on both sides, so nothing in
  the tree can tell them apart.

  Left uncorrected this reproduces the exact live-call defect
  `TestAlternativesAwareGapFields` was written to prevent (a gap sweep owing ten phantom fields,
  each with the other side of its pair already answered). **`alternative_index` therefore stays**,
  and `gap_fields`' per-target filter remains alternatives-aware — while still never consulting
  `default`. Alternatives are a genuine second axis, not a duplication to be deleted.
- **Granularity follows the decision already made in Plan C** (2026-08-07): *ceilings count
  asks, lists name missing fields.* The owed **count** is question-granular; the **re-ask list**
  names missing field paths, because rendering a partially-answered fan-out by its question
  text re-asks the half already on file.
- **`gap_fields` keeps its field-granular signature.** It becomes the paths under the owed
  questions rather than a separate walk of `task.fields`, so the re-ask lists and the Observer
  are unaffected while the *set* becomes correct. `owed_question_count` stops reconstructing
  question units via `owed_questions()` and simply returns `len(owed_now(...))`.

### 4. Fix the completion guard's measurement window

The guard stays. Its **window** is the bug, not its existence.

```python
# today — two different windows compared to each other
if self._rep_turns >= self._controller.owed_question_count(self._task_index):
    return None                      # 11 >= 5 -> bail
```

`_rep_turns` accumulates across the whole task; `owed_question_count` measures only what is
outstanding right now. Align both sides to the **whole task**: snapshot the questions owed at
task entry (exactly what `GapTaskAgent._questions_owed` already does) and compare `_rep_turns`
against that.

- 1-question task, 1 rep turn → `1 >= 1` → trust. Unchanged, still correct.
- `insurance_basics`, 11 rep turns, 16 questions at entry → `11 < 16` → **refuses**.

Note *why* `GapTaskAgent`'s ceiling is sound while `PlanTaskAgent`'s is not, despite the same
expression: the sweep agent's lifetime equals the window it measures. **A guard is sound exactly
when the agent's lifetime matches the set it measures.** The entry snapshot makes that true for
the task agent too.

**Bounded refusal replaces one-shot-then-trust** (per decision, 2026-08-08): progress — the
outstanding set shrank — resets the budget; a fruitless refusal increments it; cap 2. This is the
shape `_refuse_premature_gap_complete` already uses. It preserves the no-deadlock property (a
callee who cannot answer must never strand the call) while removing the single-escape-hatch
weakness of refusing at most once per task.

Accepted cost: the bot sometimes merges questions into one turn ("group name and group number"),
so a legitimately complete task can undershoot the turn count and absorb one spurious re-ask
before the budget lets it through.

### 5. The gap sweep keeps its position — only its input changes

**The sweep is not moved.** It continues to fire once, at the boundary into the closing task, so
any re-ask still lands before the closer's reference-number collection and goodbye. `_gap_block`,
its firing point, `_gap_pass_done` and `_next_gap_task` are all unchanged.

What changes is what it computes over: `gap_fields` now derives from the question tree
(§4), so the sweep captures the **real** missing questions instead of the subset `default` left
visible. On this trace that is the difference between sweeping 5 fields and sweeping 6 — and,
across the call, the difference between seeing `telehealth_covered` / `enrollment_required` and
never seeing them at all.

The guard (§4) and the sweep therefore consume the *same* corrected owed set at two different
times, which is what makes them agree.

#### 5a. Settle extraction before the sweep decides — spend the stall that already exists

One exposure remains: the **most recently swept task**'s answers may still be in flight when the
sweep computes its gaps (extraction lag, p90 23 s). Tasks earlier than that are long settled by
the time the terminal sweep runs, so this is not a general problem — it is specific to the last
task before the sweep.

The pause needed to fix it is **already authored and already spoken**. `closing_admin`'s outro:

> *"Perfect, I have all the administrative details I need. Let me take a quick moment to review
> my notes and make sure I haven't missed anything. One moment please."*

and `wrap_up`'s intro, which lands *after* the sweep, already reads as though one occurred:
*"Thanks so much for your patience — that covers everything on my list."* The flow was designed
for this; the pause is simply not being spent.

`_rotate` already **retires rather than closes** the outgoing observer — deliberately, because
"the turn that triggered this rotation may itself be the answer to the outgoing task's final
question" — and `_close_retiring` runs that final drain pass fire-and-forget. The pass exists;
nothing awaits it.

```
closing_admin task_complete
  └─ say(outro)                             not awaited — plays through the swap
  └─ GapTaskAgent.on_enter
       ├─ await manager.drain_pending(timeout)   NEW — runs concurrently with the outro
       └─ gap_fields(...)                        computed against settled state
```

Requires one small addition: `ObserverManager.drain_pending()`, since the drain is currently
reachable only via `_schedule_close`. Bounded by a timeout with graceful fallthrough — extraction
duration is median 2.3 s / p90 6.2 s / max 8.0 s, so ~8–10 s covers it and the spoken outro
absorbs most of the wall-clock.

**Rejected: a lagged gap ledger** (compute task N's gaps at the end of task N+1, ask from the
accumulated list at the terminal sweep). It adds no accuracy for tasks 0…N−2 — minutes have
already passed against a 23 s p90 lag — and a frozen ledger is strictly *worse* than a fresh
check, because a field gapped at n+1 can be answered incidentally several tasks later when the
rep volunteers it; re-asking it is the `answer_handling FAIL [133]` this work exists to remove.
`gap_fields` stays computed fresh, per gap agent, at ask time. The drain barrier gets the whole
benefit with no ledger to keep in sync.

### 6. Per-turn tool idempotence (P10)

A second `task_complete` / `gap_complete` in the same turn returns a plain string instead of a
second `Agent`, using the same early-return shape as `takeover_engaged`, with the marker cleared
in `on_user_turn_completed`.

P10 did **not** cause this trace — 6 tool calls produced 6 handoffs, no doubling. It is included
because it is real, cheap, filed since 2026-07-30, and retires P6. **This spec records that P8's
attribution to P10 is disproven**: P8's symptom reproduced with no doubled call present.

### 7. Completion observability

The trace recorded 233 answers and exposed zero field paths; reconstructing what was recorded
required scraping the Observer's raw completions out of descendant `llm_request` spans.

- `vera.observer.answer_recorded` — field **path**, confidence, task key. Never the value.
- On `task_complete` — `vera.completion.owed_count`, `vera.completion.refused`, so the guard's
  decision is visible instead of hidden behind the model's `reason` string.

Both with `record_exception=False, set_status_on_exception=False`, per the PHI span rules.

### 8. Validator — losslessness as a build failure

Added to the existing document validator, so drift fails CI rather than a live call:

1. Every collectable path in `task.fields` is reachable from **exactly one** question node.
2. Every gate on a field is representable on the question that targets it.
3. Every question with a `gate_text` has a `gate`.

Rule 1 is the 16-vs-20 divergence, converted into a test.

---

## Out of scope

- **Focused retry / `focus_call_plan` / P7 / Plan D.** The goal is a correct first call.
  Recorded: since Plan B, prompts render from `panels`, which `focus_call_plan` does not narrow —
  a focused retry currently renders the full question list. Filed, not fixed here.
- **Schema cleanup of misused `default`** — `pcp_referral_required` → `inapplicable_value`;
  dropping `default` on `group_name`, `group_number`, `policy_situs`, `telehealth_covered`,
  `enrollment_required`. Correct, but it changes export output and completion percentage, so it
  needs its own review. **It is also no longer load-bearing**: the tree-derived owed set never
  consults `default`. Sequencing matters — those defaults are currently the only thing keeping
  forms from sitting incomplete, so they can only be removed once the bot reliably asks.
- **Prompt caching.** `usageDetails` carries no cache fields at all; 493,674 input tokens per
  call. The plugin's explicit-cache option is incompatible with this design (Gemini rejects
  `cached_content` combined with `system_instruction`/`tools`, and we mutate instructions
  mid-task and depend on `task_complete`). Implicit caching needs its own investigation.
- **Date read-back normalisation** — belongs with P9, where a wrong recorded value never
  surfaces as a gap.
- **Raising `thinking_level`.** Rejected as a primary fix: it costs latency on every turn to
  patch one decision point. TTFT is already median 1.30 s / p90 1.59 s.

## Rejected alternatives

**Ship `FormSchemaDoc` to the worker.** Cheap (148 KB vs the 186 KB CallPlan already in Redis,
call-independent so cacheable per `schema_version_id`) but it buys nothing on the live path: the
worker's four runtime jobs need exactly one thing the CallPlan lacks — `Condition` on
`PromptQuestion`. It would add a second representation to compensate for the first being wrong,
introduce a doc/plan version-pinning invariant, and weaken dispatch as the fail-fast gate.
Rendering stays at dispatch.

**Patch `gap_fields` to ignore `default`.** ~20 lines, fixes this trace, leaves all four
duplications and the two-artifact split intact. Buys a release, not a system.

**Rebuild the compiler around a new intermediate language.** Endpoint is nearly the same object,
reached by a route with no safe intermediate landing states.

---

## Migration path

Each step lands independently and is revertible.

1. **Confirm nodes** — `immediate_confirms` become unnumbered question nodes carrying
   `target_paths`. **Rendered prompt is byte-identical** — the step is done when
   `TestPanelsMatchThePrompt` is still green *without being modified*.
2. **Validator** — turn on rules 1–3. Rule 1 fails on exactly the two spouse paths before step 1
   and passes after it, which is the step's proof.
3. **Tree-joined owed set** — `gap_fields` / `owed_question_count` computed by `owed_now()`.
4. **Fix the completion guard's window** — entry snapshot + bounded refusal. The sweep needs no
   change of its own: it consumes the corrected owed set from step 3.
5. **Drain barrier before the sweep decides** — `ObserverManager.drain_pending()`, awaited in
   `GapTaskAgent.on_enter` ahead of `gap_fields`. Independent of steps 1–4; can land first if
   useful, since it fixes phantom gaps under today's logic too.
6. **P10 idempotence.**
7. **Observability spans.**

---

## Verification

- `just check` verbatim (ruff check **and** `ruff format --check`, mypy --strict, pytest) after
  every step. Never a hand-picked subset.
- `/simplify` on the change, then re-run `just check`, before claiming done.
- **Regression tests** (pure, no LLM, in `tests/unit/test_plan_runtime.py`):
  - Run B shape — non-HMO, 16 questions at entry, 11 rep turns → **must not complete**. Note the
    outstanding count under this design is **6**, not the 5 `gap_fields` reports today: the
    tree-derived owed set no longer consults `default`, so `telehealth_covered` is included. The
    test should assert 6 and name it, so a regression to the old predicate fails loudly.
  - Run A shape — HMO → completes, with gated questions unasked because their conditions resolved.
  - Both networks "No" + out-of-network "No" → termination rule fires, early completion
    **permitted**.
  - Small group + self insured → existing consistency rule fires.
  - Doubled `task_complete` in one turn → exactly one handoff.
  - Sweep input — a task whose only gap is a `default`-carrying question (`telehealth_covered`)
    is swept. This fails today and is the sweep's whole behaviour change.
  - Sweep position — the sweep still fires once, at the boundary into the closing task. Pin it,
    so a later change cannot move it silently.
  - Drain barrier — a rep answer finalized in the last turn before the sweep is extracted before
    `gap_fields` runs, so it is not re-asked. Assert the drain is awaited, and that a drain
    exceeding the timeout falls through rather than stalling the call.
  - Losslessness — every collectable path reachable from exactly one question node.
- **Existing tests that pin the deleted behaviour must be rewritten, not deleted silently:**
  `test_a_task_that_walked_its_questions_is_not_refused` (`:1483`) and
  `test_a_task_is_still_refused_when_it_asked_less_than_it_owes` (`:1494`) both assert the turn
  ceiling. `test_a_second_task_complete_advances_even_with_questions_still_open` (`:1454`) pins
  the no-deadlock property and **must keep passing**.
- **A change to spoken behaviour is not verified by pytest.** After the sweep and heuristic
  changes, run the eval harness, then a live call:
  ```bash
  VERA_EVALS_FULL=1 VERA_EVALS_ENABLED=1 uv run pytest apps/agent_worker/tests/evals -m evals -s -rs
  ```
  `-m evals` is required; confirm real scenarios ran by the `===== <scenario>: … =====` banners.
- **Live-call acceptance test:** on a non-HMO call, `insurance_basics` must not hand off until
  `plan_effective_date`, `plan_year_information`, `telehealth_covered`, `plan_fund_type`,
  `employer_support_size` and `infertility_plan_mandate` have been asked. Browser-callee
  transport is sufficient and counts as a live call.

## Risks

- **More questions asked per call.** Six additional required questions in `insurance_basics`
  alone become owed. Calls get longer; that is the intent, but it changes call duration and
  token spend.
- **The terminal sweep is still voided by an early hangup — accepted, not fixed.** With the guard
  repaired, far less should reach the sweep, so the exposure shrinks; it does not disappear. A
  hangup before the closing task still loses whatever the guard's bounded refusal let through.
- **Bounded refusal is deliberately escapable.** Cap 2 means a determined-but-unhelpful callee can
  still advance a task with questions open. That is the no-deadlock property, chosen knowingly.
- **Question-granular counting on partially-answered fan-outs.** Mitigated by keeping re-ask
  lists field-granular (Plan C), but the interaction deserves a live look on a CPT-heavy task.
- ~~**Validator rule 1 may fail on schemas beyond `ibv_standard`.**~~ **Measured — the risk is
  small.** Across both catalogs:

  ```
  ibv_standard: collectable fields=182  reachable-from-0-questions=2  reachable-from->1=0
  disease_only: collectable fields=44   reachable-from-0-questions=0  reachable-from->1=0
  ```

  The only two violations are `spouse_partner_name` / `spouse_partner_dob` — exactly the
  `immediate_confirms` conversion in migration step 1. No path is targeted by more than one
  question in either schema, so the "exactly one" half of rule 1 already holds. **Step 2 should
  pass as soon as step 1 lands.**

## Recorded debt

- **Unify the three applicability implementations** (`question_plan._entry_decided`,
  `PlanRunController._settled` / `_decided_false`, `conditions.is_applicable`) behind one
  function parameterised by what the caller knows. Dropped from this spec's scope on 2026-08-09:
  with the owed set computed as a join, `is_applicable` is the only evaluator on the completion
  path, so unification is no longer required for the fix and should not ride along with a
  correctness change. The "worker is never less decisive than the compiler" invariant stays
  docstring-enforced until then.
- **Gap-sweep reachability.** The sweep fires once, at the boundary into the closing task, so a
  hangup before that point recovers nothing — exactly what happened on trace
  `6e1f496cb72d0182af27281c90bdca64`. Deliberately out of scope here (decision 2026-08-09):
  position unchanged, logic fixed. If a repaired guard still leaves gaps reaching the sweep on
  live calls, revisit — the measured extraction lag (23 s p90) versus task duration (1–5 min)
  means a sweep lagged by one task would be safe without any new synchronisation.
- `focus_call_plan` does not narrow `panels` (P7) — a focused retry renders the full list.
- Misused `default` in both catalogs (see *Out of scope*).
- `_exclusive_notes` still bakes routing-branch guidance to prose at compile time for the same
  "worker is DB-free" reason this spec retires. Not load-bearing for completion; revisit under
  the losslessness rule.
- No TTFT instrumentation exists in-repo; the numbers here came from Langfuse spans.
