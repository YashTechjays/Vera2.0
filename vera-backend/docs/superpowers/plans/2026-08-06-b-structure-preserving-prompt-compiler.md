# Plan B — structure-preserving prompt compiler + `PlanQuestion`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compile one numbered item per **spoken question** instead of one per stored field,
preserving the service/code structure the schema already carries, and publish that question
list in the `CallPlan` so the worker can narrow it.

**Architecture:** Two stages. Stage 1 builds a question tree from DSL constructs — `ask_groups`
are a fan-out axis (one text, N target paths), `alternatives` over leaves are an option axis
(one text, N labelled options), `alternatives` over groups are a routing question, and `Group`
+ `Group.codes` supply panel headings. Stage 2 renders that tree to markdown. The tree is also
emitted as `PlanTask.questions` so Plans C and D can filter it. `PlanTask.fields` stays exactly
as it is — the Observer, rule engine and completion maths keep their current input.

**Tech Stack:** Python 3.12, pydantic v2, pytest.

## Global Constraints

- **No DSL grammar change.** `dsl.py` gains validator rules only. No `dsl_version` bump, no
  migration, no frontend guard, no change to `prompt_version.composite_json`.
- Requires `just compile-schemas && just seed-schemas` (this plan edits `catalog/` and
  `authoring.py`). Never hand-edit `data/form_schemas/*.json`.
- `render_task_prompts` must stay **deterministic and pure**: same doc + same prompt_doc ⇒
  byte-identical output. It is DB-free and consumed by the seeder and the dispatch path.
- Document key order **is** field order — do not sort anything.
- PEP 695 type params. `asyncio` only. Never log a field value.

**Depends on:** Plan A (its `_task_of_path` map and entry-decidability split are reused here).

---

## Why: the four things the current renderer throws away

`prompting._task_text` walks `leaf_gates(doc)` into a flat numbered list.

| construct | today |
| --- | --- |
| `Group` (service, CPT code) | invisible, flattened |
| `Group.codes` / `Group.prompt` | never read — `authoring.py:206` documents this |
| `Section.ask_groups` | trailing footnote, after the questions it contradicts |
| `Section.alternatives` | trailing footnote; `alt.ask` **never rendered at all** (`prompting.py:418-422`) |

Because the structure is gone it was re-explained as prose in `Task.prompt` ("Ask per service
panel, fan answers out to the CPT codes…"). **The verbosity is compensation for a lossy
renderer**, so the prose goes away with the same change.

Prototype result over the unmodified schema: **180 → 107 questions, 60,882 → 24,134 chars
(−60%)**. `diagnostic_coverage` 33 → 4 questions.

---

## File Structure

- **Modify** `packages/vera_core/src/vera_core/forms/prompting.py` — replace `_task_text` /
  `_question_lines` (~120 lines) with the two-stage compiler (~250 lines). Keep
  `render_task_prompts`'s signature and `RenderedPrompts` / `RenderedTaskPrompt` shapes.
- **Create** `packages/vera_core/src/vera_core/forms/question_plan.py` — Stage 1 only: the
  `PromptQuestion` / `PromptPanel` model and `build_question_plan(doc, task)`. Split out
  because `prompting.py` at ~500 lines already carries the session/override merge, and
  `call_plan.py` needs Stage 1 without the markdown.
- **Modify** `packages/vera_core/src/vera_core/forms/prompt_text.py` — `build_condition_renderer`
  gains `scope: str | None` for panel-relative labels.
- **Modify** `packages/vera_core/src/vera_core/forms/call_plan.py` — add `PlanQuestion` and
  `PlanTask.questions`.
- **Modify** `packages/vera_core/src/vera_core/forms/authoring.py` — panel `AskGroup`s from
  `treatment_group`/`cpt_groups`; `cost_pair` passes an `ask`; delete `_COVERED_ASKS`,
  `_COPAY_ASKS`, `_COINSURANCE_ASKS`, `_PRIOR_AUTH_ASKS`, `_shape` and the `variant` plumbing.
- **Modify** `packages/vera_core/src/vera_core/forms/dsl.py` — validator only (Task 6).
- **Modify** `packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py`,
  `catalog/disease_only.py` — trim the six verbose `Task.prompt`s.
- **Modify** `apps/agent_worker/src/agent_worker/plan_runtime.py` — `_gating_block` deleted;
  `_apply_gating` re-renders the question list minus entry-excluded questions.
- **Tests:** `tests/unit/forms/test_prompting.py` (203 lines, largely rewritten),
  `tests/unit/forms/test_prompt_text.py`, new `tests/unit/forms/test_question_plan.py`.

**Interfaces:**

- Consumes from Plan A: `PlanRunController._task_of_path`,
  `entry_gate_split`, `_decidable_gates`.
- Produces for Plans C and D:

```python
class PlanQuestion(_Model):
    text: str                              # the spoken question, verbatim
    target_paths: list[str]                # 1..N collectable paths this question answers
    gates: tuple[Condition, ...] = ()      # residual gate chain, as rendered
    panel_title: str | None = None         # the heading it sits under, for list rendering
    codes: list[str] = Field(default_factory=list)   # CPT codes it fans out to

class PlanTask(_Model):
    ...
    questions: list[PlanQuestion] = Field(default_factory=list)
```

---

## Stage 1 rules (`build_question_plan`)

Union-find over each section's paths, joining on both relations, then one question per class:

1. **`AskGroup` → fan-out.** Members collapse to one question; text = `ag.ask`; targets = all
   members. Emit at the first member in document order.
2. **`Alternatives` over leaves → options.** Members collapse to one question; text =
   `alt.ask`; each member contributes a labelled sub-bullet (`Copay ($)`, `Coinsurance (%)`).
3. **Both at once** (diagnostic copay/coinsurance × 8 codes) → one question, 2 options × 8
   targets. A `cost_pair` sitting on two `AskGroup`s joins both fan-outs.
4. **`Alternatives` over groups → routing question.** Text = `alt.ask`; **no targets** (the
   choice surfaces as which panel's `covered` gets `Yes`), rendered unnumbered above the member
   panels. This restores two questions the bot never asks today.
5. **Plain leaf** → one question, one option, one target.
6. Question text precedence: `alt.ask` → `ag.ask` → `leaf.prompt.ask` / `leaf.prompt.confirm`.

Panel/heading rules:

7. **Panel heading** from a `Group` that carries codes or holds child groups. Its codes line
   merges its own codes with every suppressed descendant's. Numbering restarts per panel.
8. **Heading suppressed** when the group's codes are already on the enclosing panel's codes
   line, or when every question it hosts fans out beyond it — it is a storage node, not a
   conversational subject. Its questions join the parent's numbering.
9. **Wrapper collapse.** A panel with exactly one emitting child panel and no questions of its
   own keeps the *service* name and absorbs the child's codes.

Gate rules:

10. **Gate elision.** A panel prints its `applicable_when` once in its header; each question
    prints only the residual conjuncts.
11. **Panel-relative labels.** `"Covered" (Diag › Labs › CPT 58340) is "Yes"` →
    `this service is covered`. A residual gate whose refs are all `.covered` paths inside the
    current panel renders as `this service is covered` (one ref) or `the codes above are
    covered` (several).
12. **Never render a gate the runtime decides.** A conjunct that is entry-decidable (Plan A's
    `_decidable_gates`) is resolved by the worker and the question is either rendered with no
    gate text or omitted. This is what removes `any_service_requires_prior_auth` — 3,007 chars,
    27 clauses, printed twice = 70% of `closing_admin`'s prompt. Rendering it as prose is not
    an option to improve: panel-relative labels collapse it to 27 identical
    `"Prior Authorization Required" is "Yes"` clauses, which is worse.

Dropped outright:

13. `If skipped, record "$0"` — the agent has no answer tool; `inapplicable_value` is a
    frontend placeholder (`FieldRow.tsx:125`) and already reaches the runtime via
    `PlanFieldDescriptor.inapplicable_value`.
14. `Either/or — … record "N/A" for the rest` — `Alternatives` has no runtime enforcement.
15. `Ask together on the first pass: … (covers: Covered, Covered, …)` — now the primary
    question.
16. Per-leaf `Codes:` where the panel header carries them.

---

### Task 1: `PromptQuestion` model + `build_question_plan` for a flat section

Create `question_plan.py`; handle plain leaves and `AskGroup` fan-out only. Test against a
hand-built two-leaf section and against `_diagnostic_testing()`'s 4 ask groups: assert 8
`covered` paths collapse into one question with 8 `target_paths`.

### Task 2: alternatives — leaf options, then group routing

Add rules 2–4. Test: `cost_pair` over `copay`/`coinsurance` yields one question with two
options; the ASC alternative yields a routing question with `target_paths == []` and
`text` starting `"Can you provide coverage and benefit details for ambulatory surgical
center services"`. **Regression-test that `alt.ask` is present in the output at all** — it is
absent today.

### Task 3: panels, codes merging, heading suppression, numbering

Add rules 7–9. Test on `_infertility_treatment()`: `Intrauterine Insemination (IUI)` is one
panel with `CPT: 58323, 58322, 89261`, five questions numbered 1–5, and **no** `CPT 58323`
sub-heading. Test on `_general_coverage()`: three panels titled `Office Visits`,
`ASC Professional Services`, `ASC Facility` — not `CPT 58555` twice.

### Task 4: scoped condition rendering

`build_condition_renderer(doc, scope=...)` in `prompt_text.py`; add rules 10–11. Keep the
unscoped behaviour byte-identical when `scope is None` so `flow_rules` and `contradictions`
rendering does not move. Test the collapse of the breadcrumb form.

### Task 5: markdown renderer + wire into `render_task_prompts`

Replace `_task_text`/`_question_lines`. Rules 13–16 are deletions here. Keep `flow_rules` /
`contradictions` / end-of-task confirm blocks exactly as they render today — they are not part
of the question list. Snapshot-test `diagnostic_coverage` and one infertility panel.

### Task 6: DSL validator — merging must be sound

`dsl.py`: an `AskGroup`'s members must share `type`, `values`, `special_values` and
`validation`; merging into one question is unsound otherwise. Also require that a group-member
`Alternatives` carries an `ask` (rule 4 has nothing to speak without it). Both are new errors
in `_validate_document`. Add cases to `tests/unit/forms/test_schema_dsl.py`.

### Task 7: authoring macros

`treatment_group`/`cpt_groups` emit one `AskGroup` per (service, sub-question) over that
service's codes — skip services with a single code, an `AskGroup` needs ≥2 members.
`cost_pair(base, referent)` passes `ask="What is the copay or coinsurance for {referent}?"`.
Delete `_COVERED_ASKS`/`_COPAY_ASKS`/`_COINSURANCE_ASKS`/`_PRIOR_AUTH_ASKS`/`_shape` and every
`variant` parameter — with one question per sub-field per service the rotation has nothing to
de-duplicate, and it made 8 mechanically identical questions look like 8 different ones.
`service_asks` loses its `variant` arg.

**Careful:** `_diagnostic_testing()` already authors these 4 ask groups by hand. The macro must
not double-add — a member may sit in only one ask group per section
(`dsl.py:718`, `member … in more than one ask group`).

### Task 8: trim the task prompts

`ibv_standard.py`: `infertility_coverage`, `diagnostic_coverage`, `general_office_coverage`,
`male_partner`, `financial`, `closing_admin`. Delete every sentence that re-explains structure
or asks for phrasing variety ("Ask per service panel, fan answers out to…", "vary how you open
each question…", "That is wording only…"). Keep only what the list cannot express, e.g. for
`financial`: *"Read money values back for confirmation when they sound ambiguous."* Same pass
over `disease_only.py`. Then:

```bash
just compile-schemas && just seed-schemas
```

`tests/unit/forms/test_schema_dsl.py`'s freshness test fails until `compile-schemas` runs.

### Task 9: publish `PlanQuestion` in the CallPlan

`call_plan.py`: add the model, populate `PlanTask.questions` from `build_question_plan`.
`compile_call_plan` stays deterministic and memoizable per schema version. Assert
`{p for q in task.questions for p in q.target_paths} <= {f.path for f in task.fields}` — the
question list may never invent a path.

### Task 10: replace the gating block with list narrowing

`plan_runtime.py`: delete `_gating_block` and `_field_lines`' gating use. `_apply_gating`
re-renders the numbered list from `task.questions`, dropping any question whose every target is
in `entry_gate_split`'s excluded half (Plan A). No `# Questions that apply` block and no `# Excluded`
block — the rendered list **is** the applicable list. Update `TestGating` accordingly.

### Task 11: verify

`just check`; `/simplify`; `just check` again. Then the eval harness, then a live call. Confirm
on a real call that VERA asks the ASC professional-vs-facility question — she never has.

---

## Out of scope

- Gap-pass and refusal counting → **Plan C** (must ship with or immediately after this).
- Focused-retry prompt narrowing → **Plan D**.
- Observer instructions → **Plan E**.
