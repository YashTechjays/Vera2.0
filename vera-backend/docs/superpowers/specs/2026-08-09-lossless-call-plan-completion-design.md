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
heuristic is deleted rather than tuned, and the end-of-call gap sweep becomes reachable.

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

A single terminal sweep is voided by any early hangup. Everything the per-task guard lets
through depends on the call surviving to the final task.

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

### 1. Artifact changes — make the question tree authoritative

`PromptQuestion` gains the machine-readable data it currently discards. `gate_text` is
retained and demoted from truth to display.

```python
class PromptQuestion(_Model):
    text: str
    options: list[PromptOption]          # already carries target_paths
    gate: Condition | None = None        # NEW — the truth
    gate_text: str | None = None         # retained, now a cached render of `gate`
    required: bool | RequiredWhen = True # NEW
    ...
```

`immediate_confirms` stop being prose. Today a confirm-role field reaches the agent as the
string `If "Coverage Type" is "Family": {{confirm:sections.patient_information.spouse_partner_name}}`
attached to another question — while the *same* condition exists machine-readably on the field
as `required=when=RefCondition(ref='family_coverage')`. They become real question nodes
carrying `target_paths` and a `gate`, so they are countable and gate-evaluable.

The confirm mechanism itself is unchanged: a prefilled value is read back for confirmation, and
the `ask` wording remains the fallback when prefill is absent. Only its **representation** in
the tree changes.

### 2. `task.fields` becomes a derived projection

It stays — flat dot-notation, one entry per collectable leaf — because the Observer needs
exactly that to write `field_answer.field_path`, and that namespace is byte-identical to the
schema path across schema, conditions, intake, extraction and the UI.

It stops being a *parallel* artifact and becomes a *projection of the tree*, asserted total by
the validator. This is what makes the 16-vs-20 divergence unrepresentable rather than merely
fixed once.

### 3. One applicability function, three knowledge levels

Replace the three implementations with one function parameterised by what the caller knows:

```python
def gate_state(cond, answers, shared, *, task_of_path, at_task) -> TRUE | FALSE | UNDECIDED
```

- **dispatch** — no answers; decides by task position. Governs whether gate prose is printed.
- **task entry** — position + answers so far. Governs whether the question survives the list.
- **gap time** — full answers. Governs whether the question is owed.

Same rule, three inputs. This removes the hand-maintained "never less decisive" invariant: it
becomes true by construction, because there is one rule.

### 4. The owed set derives from the tree

```python
def question_is_owed(q, answers, shared) -> bool:
    return (gate_state(q.gate, ...) is TRUE
            and is_required(q, answers, shared)
            and not any(has_value(answers, p) for p in q.target_paths))
```

Three consequences worth stating explicitly:

- **`default` is never consulted.** The `telehealth_covered` class of bug dissolves without a
  schema change and without a second predicate to keep in sync with `is_satisfied`.
  `is_satisfied` is left untouched, so completion percentage, export, auto-completion and
  intake are **unchanged** — this carries no product-visible risk.
- **Either/or is satisfied structurally.** The tree already models an alternatives set as one
  question with several options, so "one answer satisfies the group" needs no special rule.
- **Granularity follows the decision already made in Plan C** (2026-08-07): *ceilings count
  asks, lists name missing fields.* The owed **count** is question-granular; the **re-ask list**
  names missing field paths, because rendering a partially-answered fan-out by its question
  text re-asks the half already on file.

### 5. Delete the completion heuristic

`_refuse_premature_completion`'s turn ceiling, `_completion_refused`, and `PlanTaskAgent._rep_turns`
are removed. Nothing replaces them.

The heuristic exists only to answer *"has the agent asked enough?"* at a moment when extraction
lag makes the answer structurally unknowable. Once recovery is guaranteed elsewhere, that
question is never asked. We ask *"are there gaps?"*, which is a fact.

Note why `GapTaskAgent`'s turn ceiling is sound while `PlanTaskAgent`'s is not, despite the same
expression: the sweep agent's lifetime equals the window it measures. **A guard is sound exactly
when the agent's lifetime matches the set it measures.** The design below makes that true by
construction.

### 6. Lagged per-task sweep

Sweep task N's gaps at the end of task **N+1**, not at task N's own boundary.

Task durations ran 1–5 minutes in the trace against a 23 s p90 extraction lag, so time is the
barrier — no drain await, no sequence barrier, no added latency at any boundary, and no phantom
gaps from stale answer state.

`_maybe_enter_gap_pass` fires at **every** boundary, sweeping any visited task with index ≤ N−1
that has gaps and has not been swept; swept tasks are tracked so nothing is swept twice. The
existing terminal sweep is retained for the final task(s).

This is not a new mechanism — `GapTaskAgent`, `_gap_block`'s mid-call framing ("this is a
mid-call follow-up, NOT the end of the call") and the bounded-refusal budget all exist and are
tested today. Only the firing points change.

Reachability follows: at end of call at most the final task or two can hold unswept gaps. The
hangup in this trace would have cost the last task instead of the whole call.

**Bounded refusal is retained inside the sweep** (per decision, 2026-08-08): progress resets the
budget, a fruitless refusal increments it, cap 2. A callee who cannot answer must never strand
the call.

### 7. Per-turn tool idempotence (P10)

A second `task_complete` / `gap_complete` in the same turn returns a plain string instead of a
second `Agent`, using the same early-return shape as `takeover_engaged`, with the marker cleared
in `on_user_turn_completed`.

P10 did **not** cause this trace — 6 tool calls produced 6 handoffs, no doubling. It is included
because it is real, cheap, filed since 2026-07-30, and retires P6. **This spec records that P8's
attribution to P10 is disproven**: P8's symptom reproduced with no doubled call present.

### 8. Completion observability

The trace recorded 233 answers and exposed zero field paths; reconstructing what was recorded
required scraping the Observer's raw completions out of descendant `llm_request` spans.

- `vera.observer.answer_recorded` — field **path**, confidence, task key. Never the value.
- On `task_complete` — `vera.completion.owed_count`, `vera.completion.refused`, so the guard's
  decision is visible instead of hidden behind the model's `reason` string.

Both with `record_exception=False, set_status_on_exception=False`, per the PHI span rules.

### 9. Validator — losslessness as a build failure

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

1. **Additive artifact fields** — `gate`, `required` on `PromptQuestion`; confirm nodes. Nothing
   reads them yet. Compile output grows; no behaviour change.
2. **Validator** — turn on rules 1–3. Expected to fail first on the confirm-node change; fix
   forward.
3. **Unified `gate_state`** — introduce it, migrate the three call sites one at a time, delete
   `_entry_decided` / `_settled` / `_decided_false` once no caller remains.
4. **Tree-derived owed set** — `gap_fields` computes from panels. `task.fields` becomes a
   projection.
5. **Delete the completion heuristic.**
6. **Lagged sweep firing points.**
7. **P10 idempotence.**
8. **Observability spans.**

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
  - Lagged sweep — gaps open in task N are swept at the end of task N+1.
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
- **Mid-call re-asks become audible.** The lagged sweep can interject between tasks.
  `_gap_block` already frames this, but it changes how a call sounds and needs live judgement.
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

- `focus_call_plan` does not narrow `panels` (P7) — a focused retry renders the full list.
- Misused `default` in both catalogs (see *Out of scope*).
- `_exclusive_notes` still bakes routing-branch guidance to prose at compile time for the same
  "worker is DB-free" reason this spec retires. Not load-bearing for completion; revisit under
  the losslessness rule.
- No TTFT instrumentation exists in-repo; the numbers here came from Langfuse spans.
