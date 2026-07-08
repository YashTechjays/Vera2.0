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

**Decision: render at call time; store the prompt document (session text +
task overrides), never rendered output.**

- The task builder already loads the form's pinned `schema_version` at call
  initiation (conditions, extraction targets, `stt_key_terms`). Rendering the
  per-task text from that same in-memory document is trivial and deterministic:
  same schema version + same prompt document + same renderer code = byte-identical
  prompt.
- `prompt_version` survives with an honest, two-part job:
  - **Session-level agent text** — persona, goal, base instructions — applicable
    to every task. This is **literal, authoritative content consumed as-is**, not
    an override of anything: `prompt_version` is its home. It makes base agent
    behavior operator-tunable at runtime: copy a version, edit, test the draft in
    the voice lab, publish.
  - **Task-level overrides** — sparse patches over the schema's task
    `intro`/`outro`/`prompt` defaults; effective text is `override ?? schema
    default`.
- Task `intro`/`outro`/`prompt` **stay in the schema** as code-reviewed, validated
  defaults (placeholder validation against `system_fields` lives there). Moving
  the text wholly into the prompt layer was considered and rejected: it creates a
  second authoring surface that must track the schema's task keys by hand, and it
  moves placeholder validation away from the document that defines the namespace.
- Session text lives in `composite_json` as a JSON block, not dedicated columns:
  one atomic versioned document that copy/publish/carry-forward move as a unit,
  and future session-level knobs (pronunciation guide, turn-taking notes) are doc
  changes, not migrations. The API's pydantic model enforces the shape.

## 2. Goals / non-goals

**Goals**

- A pure, deterministic renderer: `FormSchemaDoc` + prompt document → session text
  plus per-task prompt strings in the composite shape
  `{persona, goal, base_instructions, tasks: [{task_key, title, intro?, outro?,
  prompt}], …meta}`.
- Conditions, flow rules, and contradictions rendered into the affected task's
  prompt as natural-language instructions.
- `prompt_version.composite_json` re-purposed as the prompt document: a literal
  `session` block (persona/goal/base_instructions) + a sparse `task_overrides`
  block, with save-time validation against the pinned schema version.
- Prompts API reworked: prompt-document CRUD (existing draft/publish flow) + a
  preview endpoint returning the effective rendered prompts.
- Seeder reworked: no rendered-text materialization; publishes a factory v1 prompt
  document per schema and carries the published document forward across schema
  republishes.

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
    doc: FormSchemaDoc, prompt_doc: PromptDocument | None = None
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
    persona: str             # literal from the prompt document's session block
    goal: str                # literal
    base_instructions: str   # literal
    tasks: list[RenderedTaskPrompt]
```

`prompt_doc=None` (bootstrap gap: schema published but prompts never seeded) falls
back to the factory session constants (§6) with a logged warning — a call must not
fail for want of a prompt row. Session text passes through verbatim; it is not
folded into per-task `prompt` strings — the task builder composes session text +
task instructions when it constructs AgentTasks (§8).

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

## 4. Prompt document (`prompt_version.composite_json`)

```json
{
  "kind": "prompt_document",
  "session": {
    "persona": "…",
    "goal": "…",
    "base_instructions": "…"
  },
  "task_overrides": {
    "introduction": { "prompt": "…" },
    "wrap_up": { "outro": "…" }
  }
}
```

`PromptDocument` (pydantic, in `vera_core.forms.prompting`) with two blocks of
**deliberately different semantics**:

- `session` — **literal content, consumed as-is**: persona, goal, and base
  instructions applicable to every task in the session. All three required
  non-empty strings. Nothing underneath is overridden; this block IS the source.
  Field intents:
  - `persona` — who the agent is: name, temperament, speech pacing, how it refers
    to itself (Vera 1.0 `AGENT_PERSONA` territory);
  - `goal` — what the call is for: the objective the LLM falls back on when the
    conversation drifts;
  - `base_instructions` — global behavior rules for every task: turn-taking,
    value-recording discipline, hold/background-noise handling, role enforcement,
    anti-repetition (Vera 1.0 conversation/value-recording rule blocks).

  The block is intentionally growable: future session-level knobs (pronunciation
  guide, medical-speech pacing, escalation instructions) arrive as **optional**
  fields on the pydantic model — old documents keep validating, no migration.
- `task_overrides` — **patches**: sparse map of `task_key` → subset of
  `{intro, outro, prompt}`. Merge rule is field-level: an override field wins over
  the schema default; absent fields fall through.

**Save-time validation** (API layer, against the version's pinned schema document):

- unknown `task_key` in `task_overrides` → 400;
- any `{{token}}` in session or override text not a `system_fields` key of that
  schema → 400 (reuse `PLACEHOLDER_RE` from `dsl.py`; exact token `value` exempt);
- empty override entries (no fields set) rejected;
- `session` block required and complete.

The renderer itself ignores override keys not present in the doc (defense in depth
— carry-forward may race a schema change).

## 5. Prompt store semantics

`prompt` / `prompt_version` keep their tables and the draft→publish flow (immutable
version rows, one published per prompt, `schema_version_id` NOT NULL + RESTRICT for
audit). What changes:

- `composite_json` holds the prompt document (§4), never rendered text.
- Resolution at runtime/preview: the published `prompt_version` whose
  `schema_version_id` matches the form's pinned schema version. The voice lab may
  instead name a specific (draft) version id to test it before publishing.
- A published version normally always exists — the seeder publishes a factory v1
  per schema (§6). The no-row bootstrap gap degrades to factory session constants
  with a warning (§3).
- The copy → edit → test → publish loop is the existing version machinery: every
  save is an immutable draft (copying = saving an existing version's document as a
  new draft), the voice lab runs a named draft, publish promotes it and demotes
  the prior published version.

## 6. Seeder rework (`scripts/seed.py::_seed_prompts`)

### 6.1 Bootstrap chain (no chicken-and-egg)

The first prompt document's content originates in **code**, not the DB:
`FACTORY_SESSION` (persona / goal / base_instructions constants in
`vera_core.forms.prompting`, adapted from Vera 1.0's agent persona, placeholder-
free). The seed order is linear — `_seed_form_schemas` publishes the
`schema_version` first, then `_seed_prompts` pins the factory v1 against it — so
the `schema_version_id` FK is always satisfiable. After bootstrap the DB is
authoritative: editing `FACTORY_SESSION` in code never retrofits an existing
schema's prompts (those change only through the editor); the constants matter
again only for never-bootstrapped schemas and the §3 degradation path.

### 6.2 The step

No rendered-text compilation. The step:

1. Ensures the `Prompt` row per `FormSchema` exists (name `"<schema name> Prompt"`).
2. **Factory bootstrap**: a schema with no prompt versions gets a published v1
   whose document is the factory session content (code-authored constants in
   `vera_core.forms.prompting` — placeholder-free so they are valid for every
   schema; consulted only at creation time, plus the §3 degradation path) and an
   empty `task_overrides`.
3. **Carry-forward**: if the schema was just republished (new published
   `schema_version`) and a published prompt document exists against the *prior*
   schema version, republish that document bound to the new `schema_version_id` —
   session block verbatim, `task_overrides` pruned of entries whose `task_key` no
   longer exists in the new schema (dropped keys are named in the seed summary).
   Idempotent: a document already published against the current schema version is
   left alone.

## 7. API rework (`apps/control_plane/.../api/v1/prompts.py`)

- List/detail endpoints unchanged in shape; `composite_json` now carries the
  prompt document.
- Draft save: body is the prompt document; the new row pins the schema's currently
  **published** `schema_version` and is validated per §4 against that document
  before creation (no published schema version → 409, mirroring the existing
  draft-creation guard).
- Publish: unchanged (promote/demote, partial unique index enforces one published).
- **New:** `GET /prompts/{prompt_id}/preview?version_id=<optional>` → the effective
  `RenderedPrompts`: the named version's prompt document rendered against the
  schema document that version pins; with no `version_id`, the published version
  (none → factory session + no overrides, rendered against the schema's published
  version). Platform-gated like the other prompt routes (`platform_require`). This
  is the future editor's read path, and the voice lab uses the same
  `version_id`-naming convention to test drafts.

## 8. Consumption contract (for the next task, not built here)

At call initiation the task builder loads the form's pinned `schema_version`,
resolves the prompt document for it (§5 — the published version, or a voice-lab
draft named explicitly), calls `render_task_prompts`, and maps the result onto
LiveKit AgentTasks: the session block (`persona` + `goal` + `base_instructions`)
composes the session-level system prompt shared by every task; per task,
`intro`/`outro` are spoken verbatim and `prompt` becomes the agent instructions.
Placeholders hydrate per patient form at that point (per the 2026-07-06 design
§3.2 posture: raw values in the live pipeline; tokenization only at persistence
seams).

## 9. Testing

- **Condition-to-text**: each op, nested `all`/`any`/`not_`, shared-condition refs.
- **Placement**: flow rules and contradictions attach to the right IBV tasks
  (`patient_not_on_plan` → introduction; both contradictions → coverage).
- **Merge**: override field wins, absent falls through, unknown key ignored by the
  renderer but rejected by the API; session text passes through verbatim and is
  never folded into task `prompt` strings.
- **Bootstrap**: `prompt_doc=None` renders with factory session constants and logs
  the warning; factory constants contain no `{{placeholders}}`.
- **Golden snapshot**: the rendered IBV `introduction` and `insurance_basics`
  prompts as committed fixture files — locks wording regressions the same way the
  compiled-schema freshness test locks the artifact.
- **API integration**: draft validation (unknown task_key, bad placeholder, empty
  entry), preview merge with and without a published version.
- **Seeder**: carry-forward on republish, task_key drop reporting, idempotency.
- Determinism: rendering the same doc twice yields identical output (no clock, no
  randomness — consistent with workflow/seed reproducibility rules).

## 10. Edge cases

- Schema seeded normally → factory v1 published; preview and runtime read its
  session block with no task overrides. Schema published but prompt seed never ran
  → renderer degrades to factory constants with a logged warning (§3).
- Override saved, then schema republished with a renamed task_key → carry-forward
  drops the orphaned entry and says so; the old version row remains for audit.
- A task with `sections: []` (ritual tasks) renders instructions-only (no
  questions block).
- `{{value}}` in confirm prompts is a field-level namespace, not a system field —
  the renderer passes it through and the API's placeholder check exempts it
  (exact token `value`), matching field-prompt semantics.
- Duplicate titles in condition rendering → disambiguated with the field path in
  parentheses.
