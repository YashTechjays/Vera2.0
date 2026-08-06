# Prompt-compiler overhaul — plan index

**Date:** 2026-08-06
**Branch context:** investigated on `fix/prompt-compiler`

The voice agent's per-task system instructions are too long, contradict themselves, and
tell the agent not to ask questions it must ask. This splits the fix into five plans that
can each be executed by an independent session.

## Why (measured, not estimated)

`render_task_prompts` walks `leaf_gates(doc)` and emits **one numbered question per stored
field**, discarding `Group`, `Group.codes`, `ask_groups` and `alternatives` structure. That
structure was then re-explained as prose in each `Task.prompt`. Measured on
`ibv_standard` (infertility), full instruction text per task including the disciplines +
runtime gating block:

| task | fields | task prompt | live gating list | FULL prompt |
| --- | --- | --- | --- | --- |
| `infertility_coverage` | 73 | 23,857 | 3,452 | **31,160** |
| `diagnostic_coverage` | 33 | 11,539 | 1,639 | **17,029** |
| `closing_admin` | 14 | 8,585 | 615 | **13,051** |

Specific defects found:

1. **Flat numbering.** `diagnostic_coverage` renders 33 numbered questions for 8 CPT codes,
   then 4 footnotes (`Ask together on the first pass: … (covers: Covered, Covered, Covered,
   Covered, Covered, Covered, Covered, Covered)`) that contradict them, then 8 identical
   `Either/or — … : Copay ($), Coinsurance (%)` lines with no anchor. `infertility_coverage`
   ends with 15 of those, 14 byte-identical.
2. **Gate prose repeated per question**, with unresolvable breadcrumbs:
   `"Covered" (Diagnostic Testing (Labs, X-ray & Ultrasound) › Labs, Xray/Ultrasound › CPT
   58340) is "Yes"`.
3. **`any_service_requires_prior_auth` renders to 3,007 chars / 27 clauses and is printed
   twice** in `closing_admin` = **70% of that task's prompt**.
4. **The runtime gating block pre-excludes intra-task questions.** 131 of 149 gated
   questions depend on an answer collected *inside the same task*; `_apply_gating` runs only
   in `on_enter`, so those gates are always false at that moment and the block says
   `do NOT ask these, whatever the task list says` about TPA Name, PBM Name/Phone, ISP
   Name/Phone and Enrollment Provider Name/Phone. **This is a live answer-loss bug.** → Plan A.
5. **Instructions the agent cannot obey.** `If skipped, record "$0"` — plan agents are
   dialogue-only (`plan_runtime.py:12-16`), their only tool is `task_complete`;
   `inapplicable_value` is a frontend placeholder concept (`FieldRow.tsx:125`) with no
   backend auto-fill. `Either/or — … record "N/A" for the rest` — `Alternatives` has no
   runtime enforcement anywhere.
6. **`Alternatives.ask` is never rendered.** `prompting.py:418-422` builds the member-title
   list and ignores `alt.ask`, so two authored disambiguation questions never reach the bot
   on any call: the ASC professional-vs-facility question and the egg-cryo
   elective-vs-cancer question. Today the bot asks both ASC panels in full — the same CPT
   58555 twice.
7. **`focus_call_plan` narrows `fields` but not `prompt`** — already filed as **P7** in
   `2026-07-30-call-flow-eval-findings-remediation.md`, still open. → Plan D.

Prototype (structure-preserving renderer over the *unmodified* schema, plus two authoring
macro changes) produced: **180 → 107 spoken questions, 60,882 → 24,134 chars (−60%)**;
`diagnostic_coverage` 33 → 4 questions, 11,539 → 1,623 chars.

## Plans and dependency order

| plan | file | depends on | ships alone? |
| --- | --- | --- | --- |
| **A** — gating entry-decidability | `2026-08-06-a-gating-entry-decidability.md` | — | **yes** |
| **B** — structure-preserving compiler + `PlanQuestion` | `2026-08-06-b-structure-preserving-prompt-compiler.md` | A | yes |
| **C** — gap/refusal question-unit calibration | `2026-08-06-c-question-unit-gap-refusal-calibration.md` | B | **no — must ship with or immediately after B** |
| **D** — focused-retry prompt narrowing (P7) | `2026-08-06-d-focused-retry-prompt-narrowing.md` | B | yes |
| ~~**E** — observer fan-out instruction~~ | `2026-08-06-e-observer-fanout-instruction.md` | — | **NOT NEEDED** — closed by live-call evidence |

**A is independent of everything** and was worth landing on its own — it fixes a live bug.

**E is closed.** A live call on the Plan B compiler showed the Observer already fans one
blanket answer out across all 24 paths it covers, unprompted; the plan's premise was wrong.

**C is not optional once B lands.** B makes the agent ask one question covering N fields;
C's counters still count fields. Left uncalibrated, `_refuse_premature_completion`
(`plan_runtime.py:343`) and `_refuse_premature_gap_complete` (`:519-520`) refuse completion
until `rep_turns >= len(outstanding_fields)`, so a panel answered in one turn gets refused
and re-asked. `_GAP_FRUITLESS_REFUSALS = 2` bounds the loop, so it degrades rather than
hangs — but it costs the rep two spurious re-asks per panel.

## Shared facts every plan needs

- **Nothing here changes the DSL grammar.** No `dsl_version` bump, no migration, no frontend
  guard. `dsl.py` gains validator rules only (Plan B).
- **No stored prompt is invalidated.** `prompt_version.composite_json` holds only the session
  block + per-task text overrides (`PromptDocument`). Rendered prompts are compiled fresh at
  dispatch (`compile_call_plan` → `render_task_prompts`), so no prompt re-seed is needed.
  Plan B touches `catalog/` + `authoring.py`, so it does need
  `just compile-schemas && just seed-schemas`.
- **The field pipeline is untouched by all five plans.** `PlanFieldDescriptor` (path, title,
  type, values, validation, required, gates, inapplicable_value) is built by
  `compile_call_plan` from `leaf_gates(doc)`. The `field_answer.field_path` namespace, gate
  semantics and requiredness are identical before and after. `RuleEngine`, completion maths,
  intake and the review UI are all unaffected.
- **Gate class matters.** A gate conjunct is *decidable at task entry* only if every field
  path it references is collected by an **earlier** task (or by no task at all — a
  context/prefilled leaf). Conjuncts referencing the current or a later task are undecided at
  entry and must be left to live prose. Classification of the current schema:

  ```
  task                        gated Qs  cross-task  intra-task  mixed
  insurance_basics                   4           0           4      0
  infertility_coverage              72           0          72      0
  diagnostic_coverage               32           0          32      0
  general_office_coverage            9           0           9      0
  financial                         14           3           7      4
  male_partner                       9           1           0      8
  closing_admin                      9           2           7      0
  TOTAL                            149           6         131     12
  ```

- **Gate class differs by *when* you ask.** At **task entry** an intra-task gate is undecided.
  At **end of call** (the gap pass) it is decided, because its gate question has been asked by
  then. So `gap_fields` must keep evaluating **full** gate chains — Plan A deliberately does
  not touch it.

## Verification that applies to every plan

- `just check` (ruff check **and** ruff format --check, mypy --strict, pytest) — run verbatim,
  never a hand-picked subset.
- Run `/simplify` on the change, then re-run `just check`, before claiming done.
- **A change to spoken behaviour is not verified by pytest.** After B/C/D, run the eval
  harness and then a live call:
  ```bash
  VERA_EVALS_FULL=1 VERA_EVALS_ENABLED=1 uv run pytest apps/agent_worker/tests/evals -m evals -s -rs
  ```
  `-m evals` is required; confirm real scenarios ran by the `===== <scenario>: … =====` banners.
- Plan A has a specific live-call acceptance test: on a call where the rep says "yes, there is
  a TPA", VERA must ask for the TPA's name. Today she is instructed not to.

## Scratch artifacts from the investigation

The working prototype of Plan B's renderer (runs against the real catalog, simulates the two
authoring changes by patching the built doc) was written to the session scratchpad, not the
repo. It is not required to execute Plan B — Plan B specifies the rules directly — but it
proved the output format is derivable from the current schema with no grammar change.
