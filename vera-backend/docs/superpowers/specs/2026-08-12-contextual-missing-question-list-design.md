# Contextual missing-question lists — service, codes and gate, from the compiled tree

**Date:** 2026-08-12
**Branch context:** `fix/task-complete-and-gap-pass-question-preparatino`
**Builds on:** `2026-08-09-lossless-call-plan-completion-design.md` (the owed set and the
question tree it derives from)

## Summary

Two places tell the voice agent what it still owes: the `task_complete` refusal and the
gap-pass system instruction. Both render `PlanFieldDescriptor.title` — the **storage field's**
title — so the agent is handed a list of bare nouns with no subject:

```
Cycle Limit
Cycle Limit
Covered (cpt_89337) (expected one of: Yes, No, N/A)
Covered (cpt_89337) (expected one of: Yes, No, N/A)
Covered (cpt_58974) (expected one of: Yes, No, N/A)
Covered (cpt_89290) (expected one of: Yes, No, N/A)
Covered (cpt_89291) (expected one of: Yes, No, N/A)
```

Nothing there says *which service*, and the two ambiguities are structural rather than
cosmetic: the two `Cycle Limit` lines are OI/TI and IUI, and `cpt_89337` appears twice because
elective and cancer egg cryopreservation bill the same code. The agent has to invent a subject,
and near the end of a call it does.

The context it needs already exists. The compiled `PromptPanel` tree carries the service
heading, the merged CPT/ICD code line, the pre-rendered `Ask only if …` gate prose, the answer
vocabulary and the fanned-code note — `render_panels` prints all of it for the task agent's own
instruction list. The refusal path simply never looked at the tree.

This spec replaces field-title rendering with a **narrowed copy of that same tree**, adds an
`explode` mode that pre-loads the follow-up questions a missing answer will open, and deletes
the field-line renderers.

---

## Evidence

The bare-title list above is `_field_lines` (`plan_runtime.py:177`) applied to
`gap_fields`' output. Three distinct defects:

1. **No ancestry.** `_owning_segment` appends the owning path segment (`cpt_89337`) only when a
   title repeats. A leaf whose parent is the service group (`cycle_limit`, `additional_notes`)
   has no code segment to fall back on, so two services' cycle limits render identically.
2. **The disambiguator is not unique.** `_TREATMENTS` gives both `egg_cryopreservation_elective`
   and `egg_cryopreservation_cancer` the code `89337`, so `(cpt_89337)` distinguishes nothing.
3. **Field granularity overstates the ask.** `gap_fields` is field-granular by design (Plan C,
   2026-08-07). `cpt_89290.covered` and `cpt_89291.covered` are two fields of **one** spoken
   `AskGroup` question, so the list shows two entries for one ask, reading as a run of
   near-duplicates.

Rendered from the tree instead, against the real `ibv_standard` schema and the same seven
paths:

```
1. Ovulation Induction/Timed Intercourse (OI/TI) > What is the cycle limit for ovulation
   induction? (only if this service is covered)
2. Intrauterine Insemination (IUI) [CPT 58323, 58322, 89261] > What is the cycle limit
   for IUI? (only if any of the codes above is covered)
   * First settle which applies: ... elective, or cancer-related fertility preservation?
     (Egg Cryopreservation Elective or Egg Cryopreservation Cancer -- only one applies)
3. Egg Cryopreservation Elective [CPT 89337] > Is CPT code 89337 for elective egg
   cryopreservation covered under this plan?
4. Egg Cryopreservation Cancer [CPT 89337] > Is CPT code 89337 for egg cryopreservation
   related to cancer treatment covered under this plan?
5. Frozen Embryo Transfer (FET) [CPT 58974] > Is CPT code 58974 for frozen embryo
   transfer covered under this plan?
6. Embryo Biopsy [CPT 89290, 89291] > Are Embryo Biopsy codes 89290, 89291 covered
   under this plan?
```

Seven ambiguous field titles become six unambiguous asks.

### The gap-pass blind spot

`GapTaskAgent`'s instruction block lists only what is owed **right now**. The Observer extracts
in a detached pass, so on the turn immediately after the representative says *"yes, 89337 is
covered"*, the copay, prior-auth, cycle-limit and notes follow-ups are not yet owed and
therefore not yet listed. The agent has an answer in hand and no sanctioned next question —
the same hallucination window, reached from the other side. `_apply_gap_list` closes it one
turn late, which is one turn too late.

---

## Design

### One narrowing primitive, two renderers

| Layer | Function | File |
|---|---|---|
| Tree narrowing | `keep_questions(panels, wanted)` — complement of `drop_questions` | `forms/question_plan.py` |
| Tree × descriptors | `owed_question_tree(task, paths, answers, shared, *, explode=False)` | `forms/call_plan.py` |
| Compact render | `render_digest(panels)` | `forms/prompting.py` |
| Full render | `render_panels(panels)` — **unchanged** | `forms/prompting.py` |

`owed_question_tree` lives in `call_plan.py` because the explode closure needs
`PlanFieldDescriptor.gates` — the same tree × descriptor join `owed_now` already performs
there. All four functions stay pure, DB-free and deterministic, so the worker keeps rendering
without a `FormSchemaDoc`.

`owed_questions` (`question_plan.py:681`) has no production caller — its own docstring records
that the guards stopped counting in it — and `keep_questions` subsumes it. It is deleted in
this change rather than left to drift beside its replacement.

### `keep_questions`

Mirrors `drop_questions` structurally, including its two non-obvious rules:

- **A confirm run travels with its anchor.** A `confirm_immediate` node's anchor is positional,
  not modeled, so a surviving question carries the confirm nodes that follow it and a dropped
  one takes them with it.
- **A routing question survives only when ≥ 2 of the panels it routes between survive.** A
  routing question (`Alternatives` over groups: ASC professional/facility, egg cryo
  elective/cancer) has no target paths and collects nothing, so it can never itself be missing.
  With both branches owed it is the line that tells the agent they are mutually exclusive; with
  one branch left there is nothing to route between, and its rendered text — *"take only the
  matching panel below"* — would name a panel that is not below. The surviving branch keeps its
  own gate, and the Observer still has `exclusive_note` for the record-N/A-not-No hazard.

### Question granularity, and the partial fan-out

The narrowed tree is question-granular: an `AskGroup` is one `PromptQuestion` whose
`target_paths` are all its members, so one owed member and all owed members both render one
line. That is correct for the ask — there is no per-code sentence in the tree — but it loses
the per-code targeting today's field-granular list has. On `diagnostic_testing`, two owed codes
out of eight would re-ask a sentence naming all eight.

`PromptQuestion` gains `still_needed: list[str] = []`, stamped by `owed_question_tree` on the
surviving copy when the owed targets are a strict subset of the question's targets and each
maps to a distinct `cpt_` segment. `_numbered_question` renders it only when non-empty, so the
compiler — which never sets it — produces byte-identical output and `TestPanelsMatchThePrompt`
still holds.

```
1. Are diagnostic labs, X-ray and ultrasound services CPT codes: 58340, 82670, ... covered
   under this plan?
   - Answers: Yes | No | N/A
   - One question for all of these codes; apply the answer to every code the representative
     confirms: 58340, 82670, ...
   - Still needed for: 58340, 82670.
```

Only the `AskGroup` (fan-out) axis needs this. On the `Alternatives` (either/or) axis
`panel_cost_pairs` puts all copay **and** coinsurance paths of a service in one group, so
`gap_fields`' `_alternatives` lookup means a single copay answer clears the whole cost
question — a partial cost fan-out is unreachable, and naming one side would wrongly narrow a
question either side satisfies.

### `explode` — transitive gate closure

Seed with the owed paths. Any question whose targets' gate chain
(`condition_field_paths(field.gates, shared)`) references a path already in the set joins,
contributing its own target paths; repeat to fixpoint. Scoped to the one task being rendered.

A dependent joins **only if at least one of its targets has no value**, so a copay already on
file is never re-listed. For `covered` the closure pulls in copay-or-coinsurance, prior auth,
cycle limit and notes — each keeping its own `Ask only if …` line, which is what makes a single
tree sufficient to express two tiers.

### Call sites

A **system instruction gets the full render** — its reader has no other list. A **refusal gets
the digest** — its reader already has one, and only needs pointing at entries.

| Site | Render | Explode |
|---|---|---|
| `PlanTaskAgent._refuse_premature_completion` | `render_digest` | no |
| `_gap_block` (gap system instruction) | `render_panels` | **yes** |
| `GapTaskAgent._refuse_premature_gap_complete` | `render_digest` | no |

`_field_line`, `_field_lines` and `_owning_segment` are deleted.

### `render_digest`

Groups by panel chain so a service crumb is printed once, with its owed questions numbered
beneath. Numbering is continuous across panels — the same rule `render_panels` and
`numbered_questions` share, so the last ordinal is still the total owed, and a routing line
takes no ordinal.

```
Ovulation Induction/Timed Intercourse (OI/TI):
  1. What is the cycle limit for ovulation induction? (only if this service is covered)

Intrauterine Insemination (IUI) [CPT 58323, 58322, 89261]:
  2. What is the copay or coinsurance for IUI?
     [either: Copay ($) / Coinsurance (%)] (only if this service is covered)
  3. Is prior authorization required for Intrauterine Insemination (IUI)?
     (only if this service is covered)
  4. What is the cycle limit for IUI? (only if any of the codes above is covered)
```

It lives beside `_panel_lines` and `numbered_questions` because it renders the same tree and
must move whenever the numbering rule does. The sole root section panel is omitted from the
crumb — it names the task, so repeating it on every line says nothing.

### The gap block's two tiers

One tree; the gate prose marks the tier. A question with no `Ask only if` is owed now; one with
a gate is a follow-up. This is the shape the main task agent already reads, so it needs no new
convention — only honest framing:

```
N required questions from this task are still unanswered. Ask every question below whose
condition holds, one at a time. A question marked "Ask only if ..." is a follow-up: ask it
only once its condition is true — typically right after the representative confirms coverage.
```

`N` is `numbered_questions` of the **unexploded** narrowing — the required count, which is what
`_refuse_premature_gap_complete`'s ceiling enforces. The exploded total is never claimed as
owed. The existing *"the list is the complete set"* / *"do not shorten the LIST"* wording is
replaced, because with follow-ups pre-loaded it would push the agent to ask conditional
questions unconditionally.

`GapTaskAgent.__init__` builds its instructions before any answer snapshot exists, so the
empty-tree branch — *"when the list arrives, re-ask ONLY those specific questions"* — stays.

### Two seams this forces open

- **`_apply_gating` must keep its narrowed tree.** It computes `kept` at entry and discards it,
  so a refusal narrowing from `self._task.panels` could name a question the agent's own
  instructions do not contain. `on_enter` stores it (`self._panels`, defaulting to
  `task.panels`).
- **The gap tree is pre-pruned by `drop_questions(panels, excluded_paths)`** before narrowing.
  `gap_fields` already filters by `is_applicable`, but the explode closure could otherwise
  surface a question some *other* gate has decidably ruled out.

### Invariants deliberately untouched

This is a rendering change. `_questions_at_entry`, `owed_question_count`, both refusal budgets
and both turn ceilings keep their current arithmetic, and `gap_fields` stays field-granular.

`_apply_gap_list`'s dedupe key moves from the owed-path tuple to the rendered block: the text
is now a function of paths *and* answers (the `still_needed` clause and the explode filter both
read answers), so a path-keyed cache would serve a stale list.

### PHI

The rendered block carries `{{token}}` values hydrated by `fuse_prefill`, so it is
unloggable. Logging at all three sites stays as it is today — counts and task keys only, never
the rendered text and never a field value.

---

## Testing

| What | Where |
|---|---|
| `keep_questions`: routing kept at ≥2 / dropped at 1, confirm run travels with anchor, empty panels pruned, fan-out counted once | `tests/unit/forms/test_question_plan.py` |
| `owed_question_tree`: closure reaches fixpoint, answered dependents excluded, `still_needed` only on a strict code subset | `tests/unit/forms/test_call_plan.py` |
| `render_digest`: crumb printed once per panel, continuous numbering, routing line unnumbered, either/or labels present | `tests/unit/forms/test_prompting.py` |
| `render_panels` byte-identity with `still_needed` unset | existing `TestPanelsMatchThePrompt` |
| Real-schema regression: the two `Cycle Limit` lines and the two `cpt_89337` lines are distinguishable | `apps/agent_worker/tests/unit/test_plan_runtime.py` |

`TestFieldLines` is replaced by tests on the two refusal messages and `_gap_block`;
`_INTAKE_GAPS` is updated to the question-granular list.

Gate: `just check`, then `/simplify`, then `just check` again. A change to what the agent is
told to say is not verified by pytest — the eval harness
(`apps/agent_worker/tests/evals`, `-m evals`) exercises the gap pass and the completion guards,
and a live browser-callee call is required before shipping.

---

## Out of scope

- `gap_fields` staying field-granular. The narrowing consumes its output; changing its
  granularity is a separate decision with guard-arithmetic consequences.
- The end-of-task confirm limitation `owed_now` documents (`confirm_in_task` with
  `confirm_immediate=False` can never be owed). No catalog authors one.
- Any change to `render_panels`' output for the compiled prompt.
