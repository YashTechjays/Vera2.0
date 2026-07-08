# Prompt compiler — runtime per-task rendering with prompt_version overrides

**Date:** 2026-07-08
**Status:** Approved
**Builds on:** `2026-07-02-form-schema-dsl-v2-design.md` (§5 prompt-compiler contract),
`2026-07-06-task-prompts-dsl-design.md` (task intro/outro/prompt + placeholders)

## 1. Problem and the pivotal decision

The voice runtime needs one instruction prompt per task — the text a LiveKit
AgentTask runs on — generated from the form-schema DSL and folding in the schema's
conditions, flow rules, contradictions, and task-level instructions.

The first instinct (and the current code) was to **materialize** that document at
seed time into `prompt_version.composite_json`, pinned to a `schema_version`. Design
review killed that: the rendered prompt is a *pure function* of the schema document,
so storing its output is caching, not modeling — it recreates the freshness/drift
problem the compiled-schema artifact needs a CI test to police, adds a seeder
ordering dependency, and silently discards hand edits every time a schema
republishes.

**Decision: render at call time; store only human overrides.**

- The task builder already loads the form's pinned `schema_version` at call
  initiation (conditions, extraction targets, `stt_key_terms`). Rendering the
  per-task text from that same in-memory document is trivial and deterministic:
  same schema version + same renderer code = byte-identical prompt.
- `prompt_version` survives with an honest job: the **operator tuning lever**. It
  stores sparse per-task text overrides, editable from `/agent-prompt` without a
  schema bump or a deploy.
- Task `intro`/`outro`/`prompt` **stay in the schema** as code-reviewed, validated
  defaults (placeholder validation against `system_fields` lives there); the
  effective text is `override ?? schema default`. Moving the text wholly into the
  prompt layer was considered and rejected: it creates a second authoring surface
  that must track the schema's task keys by hand, and it moves placeholder
  validation away from the document that defines the namespace.

## 2. Goals / non-goals

**Goals**

- A pure, deterministic renderer: `FormSchemaDoc` (+ optional overrides) → per-task
  prompt strings in the composite shape
  `{tasks: [{task_key, title, intro?, outro?, prompt}], …meta}`.
- Conditions, flow rules, and contradictions rendered into the affected task's
  prompt as natural-language instructions.
- `prompt_version.composite_json` re-purposed as a sparse overrides document, with
  save-time validation against the pinned schema version.
- Prompts API reworked: overrides CRUD (existing draft/publish flow) + a preview
  endpoint returning the effective rendered prompts.
- Seeder reworked: no text materialization; carry-forward of published overrides
  across schema republishes.

**Non-goals**

- Agent-worker / task-builder consumption of the renderer (the next task on this
  branch). This task delivers up to and including the API.
- The `/agent-prompt` frontend rework (follow-up against the new API and preview
  endpoint).
- Placeholder hydration (call-initiation concern; placeholders pass through
  un-hydrated).
- Per-question override granularity — question phrasing is tuned in the schema so
  it stays in sync with extraction; overrides are task-level only (YAGNI).
- No DB migration: `prompt` / `prompt_version` tables keep their shape; only the
  meaning of `composite_json` changes (it was seed-generated data with no runtime
  consumer, so no compatibility shim is needed).

## 3. Renderer (`vera_core.forms.prompting`, pure & DB-free)

```python
def render_task_prompts(
    doc: FormSchemaDoc, overrides: PromptOverridesDoc | None = None
) -> RenderedPrompts
```

```python
class RenderedTaskPrompt(BaseModel):
    task_key: str
    title: str
    intro: str | None   # AgentTask entry speech — verbatim, never folded into prompt
    outro: str | None   # AgentTask exit speech — verbatim
    prompt: str         # the compiled instruction text

class RenderedPrompts(BaseModel):
    name: str
    insurance_type: str
    dsl_version: str
    tasks: list[RenderedTaskPrompt]
```

Implementation reuses the existing structured builder (`compile_prompt_document`'s
leaf-gate walk, per-section question grouping, `confirm_in_task` routing) as a
private intermediate representation; a new text formatter turns each IR task entry
into the `prompt` string. The IR is no longer stored anywhere.

### 3.1 Per-task prompt assembly (in order)

1. **Task applicability** — `applicable_when`, when present, renders as a "This
   task runs only when …" preamble (condition-to-text, §3.2).
2. **Task instructions** — the task's `prompt` (override-merged) leads the text.
3. **Questions** — per section in document order: section title, optional section
   `prompt.intro`, section codes (`speak_cpt: true` → "read these codes aloud",
   else "provide if asked"); then numbered questions from `ask`/`confirm` leaves
   carrying: the ask/confirm text (`{{value}}` left un-hydrated), expected enum
   vocabulary, `special_values`, `hints`, validation notes (date format, numeric
   range), `derive` notes ("when <condition>, record <value> without asking"),
   `inapplicable_value` skip-fill notes, and requiredness. Leaf gate chains render
   as "Ask only if …" clauses. `ask_groups` render as one combined question
   replacing its members on the first pass; `alternatives` render as either/or
   ("once one is answered, record N/A for the others"). `confirm_in_task` leaves
   render in a "confirm at the end of this task" block.
4. **Termination rules** — each flow rule attaches to the task whose sections
   contain its trigger fields (where it can actually fire): condition in words,
   the rule's `note`, and the skip instruction ("stop the remaining questions and
   move to <skip_to_task title>"). For the IBV schema: `patient_not_on_plan` →
   introduction; `no_out_of_network_coverage` → insurance_basics.
5. **Consistency checks** — each contradiction attaches to the task containing the
   *last* referenced field in document order (the earliest point both sides are
   known; both IBV contradictions land in `coverage`): condition in words, the
   `reason`, the `clarify` script, and the fields to re-confirm (by title).

Formatting: plain-text sections with headers and numbered questions — readable in
the future editor preview. The formatter is one module; switching to XML-tag style
later is a localized change. Rendering is fully deterministic (no timestamps, no
randomness).

### 3.2 Condition-to-text

One deterministic function renders any `Condition` to English: `Comparison` →
"<field title> is <value>" (title looked up from the doc; path appended in parens
only when titles collide), `all`/`any`/`not_` → "and"/"or"/"not" grouping,
`RefCondition` → the shared condition expanded by name. Used for gates, task
applicability, flow rules, and contradictions, so wording is uniform everywhere.

### 3.3 Attachment edge cases

- A flow rule or contradiction whose trigger fields span **no collect task**
  (context-only fields) cannot happen — conditions must resolve to leaves, and the
  validator's task rules keep collect leaves task-assigned; `confirm_in_task`
  leaves belong to their named task.
- If a rule's fields span multiple tasks, it attaches to the latest task in
  document order (where the last answer arrives).
- `skip_to_task` referencing a task that renders later is expected; the renderer
  only names it.

## 4. Overrides document (`prompt_version.composite_json`)

```json
{
  "kind": "task_prompt_overrides",
  "tasks": {
    "introduction": { "prompt": "…" },
    "wrap_up": { "outro": "…" }
  }
}
```

`PromptOverridesDoc` (pydantic, in `vera_core.forms.prompting`): sparse map of
`task_key` → subset of `{intro, outro, prompt}`. Merge rule: field-level — an
override field wins over the schema default; absent fields fall through.

**Save-time validation** (API layer, against the version's pinned schema document):

- unknown `task_key` → 400;
- any `{{token}}` in override text not a `system_fields` key of that schema → 400
  (reuse `PLACEHOLDER_RE` from `dsl.py`);
- empty override entries (no fields set) rejected.

The renderer itself ignores override keys not present in the doc (defense in depth
— carry-forward may race a schema change).

## 5. Prompt store semantics

`prompt` / `prompt_version` keep their tables and the draft→publish flow (immutable
version rows, one published per prompt, `schema_version_id` NOT NULL + RESTRICT for
audit). What changes:

- `composite_json` holds the overrides doc, never rendered text.
- Resolution at runtime/preview: the published `prompt_version` whose
  `schema_version_id` matches the form's pinned schema version, else `{}` (pure
  schema defaults).
- No version rows exist until someone edits — absence of overrides is the normal
  state, not an error.

## 6. Seeder rework (`scripts/seed.py::_seed_prompts`)

No text compilation. The step:

1. Ensures the `Prompt` row per `FormSchema` exists (name `"<schema name> Prompt"`).
2. **Carry-forward**: if the schema was just republished (new published
   `schema_version`) and a published overrides doc exists against the *prior*
   schema version, republish those overrides bound to the new `schema_version_id`,
   dropping entries whose `task_key` no longer exists in the new document — dropped
   keys are named in the seed summary. Idempotent: an overrides doc already
   published against the current schema version is left alone.
3. Otherwise: no-op (no empty versions are created).

## 7. API rework (`apps/control_plane/.../api/v1/prompts.py`)

- List/detail endpoints unchanged in shape; `composite_json` now carries the
  overrides doc.
- Draft save: body is the overrides doc; the new row pins the schema's currently
  **published** `schema_version` and is validated per §4 against that document
  before creation (no published schema version → 409, mirroring the existing
  draft-creation guard).
- Publish: unchanged (promote/demote, partial unique index enforces one published).
- **New:** `GET /prompts/{prompt_id}/preview?version_id=<optional>` → the effective
  `RenderedPrompts`: the named version's overrides merged over the schema document
  that version pins; with no `version_id`, the published version (none → `{}`
  overrides rendered against the schema's published version). Platform-gated like
  the other prompt routes (`platform_require`). This is the future editor's read
  path.

## 8. Consumption contract (for the next task, not built here)

At call initiation the task builder loads the form's pinned `schema_version`,
resolves the published overrides for it (§5), calls `render_task_prompts`, and maps
each `RenderedTaskPrompt` onto a LiveKit AgentTask: `intro`/`outro` spoken verbatim,
`prompt` as the agent instructions, placeholders hydrated per patient form at that
point (per the 2026-07-06 design §3.2 posture: raw values in the live pipeline;
tokenization only at persistence seams).

## 9. Testing

- **Condition-to-text**: each op, nested `all`/`any`/`not_`, shared-condition refs.
- **Placement**: flow rules and contradictions attach to the right IBV tasks
  (`patient_not_on_plan` → introduction; both contradictions → coverage).
- **Merge**: override field wins, absent falls through, unknown key ignored by the
  renderer but rejected by the API.
- **Golden snapshot**: the rendered IBV `introduction` and `insurance_basics`
  prompts as committed fixture files — locks wording regressions the same way the
  compiled-schema freshness test locks the artifact.
- **API integration**: draft validation (unknown task_key, bad placeholder, empty
  entry), preview merge with and without a published version.
- **Seeder**: carry-forward on republish, task_key drop reporting, idempotency.
- Determinism: rendering the same doc twice yields identical output (no clock, no
  randomness — consistent with workflow/seed reproducibility rules).

## 10. Edge cases

- Schema with no overrides ever → preview and runtime render pure defaults; no
  prompt_version rows exist.
- Override saved, then schema republished with a renamed task_key → carry-forward
  drops the orphaned entry and says so; the old version row remains for audit.
- A task with `sections: []` (ritual tasks) renders instructions-only (no
  questions block).
- `{{value}}` in confirm prompts is a field-level namespace, not a system field —
  the renderer passes it through and the API's placeholder check exempts it
  (exact token `value`), matching field-prompt semantics.
- Duplicate titles in condition rendering → disambiguated with the field path in
  parentheses.
