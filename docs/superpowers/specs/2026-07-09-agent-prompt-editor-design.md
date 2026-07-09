# `/agent-prompt` editor rework — session block, task overrides, rendered preview

**Date:** 2026-07-09
**Status:** Approved
**Builds on:** `2026-07-08-prompt-compiler-design.md` (PromptDocument, preview endpoint),
`2026-07-06-task-prompts-dsl-design.md` (task intro/outro/prompt, placeholder namespace).
**Handoff:** `docs/superpowers/handoffs/2026-07-09-agent-prompt-editor-rework.md`.

## 1. Problem

`vera-frontend/src/pages/AgentPrompt.tsx` and `src/lib/api/prompts.ts` were built for
the old model: one big textarea editing a compiled `composite_json.prompt` string.
Under the new architecture `composite_json` is a `PromptDocument` — a literal
`session` block (persona / goal / base_instructions) plus a sparse `task_overrides`
map patching the schema's per-task `intro`/`outro`/`prompt` defaults — and prompts
are rendered at call time. The page must become an editor for that document with a
rendered preview, provenance, version flow, and placeholder-aware validation UX.

Settled UX decisions (brainstorm 2026-07-09, all confirmed):

- **Stateless preview endpoint** added so unsaved edits render without creating
  draft rows.
- **Master-detail 3-pane layout**: rail (session + tasks + versions) → editor for
  the selection → preview of the selection.
- **Collapsible schema default** under an overridden field (no diff library).
- **Insert-placeholder picker** (button + searchable dialog), server as validation
  authority.

## 2. Backend affordances (the only backend changes)

The backend is otherwise done. Three small additions to
`apps/control_plane/src/control_plane/api/v1/prompts.py`, each justified:

### 2.1 Pinned schema version on prompt versions

`PromptVersionSummary` and `PromptVersionDetail` gain:

```
schema_version_id: UUID   # PromptVersion.schema_version_id (already a column)
schema_version: int       # joined SchemaVersion.version
```

*Why:* UX requirement — the preview pane must show "pinned schema v3", and after a
schema republish the published prompt may briefly pin the prior schema version.
Nothing exposes the pin today. List/detail queries join `SchemaVersion`.

### 2.2 `GET /prompts/{prompt_id}/schema` — platform-gated published-schema read

Returns the prompt's schema's currently **published** schema version:

```json
{"id": "…", "schema_id": "…", "version": 3, "status": "published",
 "insurance_type": "…", "name": "…", "document": { …FormSchemaDoc… }}
```

(the `SchemaVersionDetail` shape from `patient_forms.py`). Gated
`platform:prompts:read` + `platform_scoped_session`. 409 when no published schema
version exists (mirrors the draft-save guard). `Cache-Control: no-store` is not
required (global catalog, no PHI) but harmless; follow the sibling routes.

*Why:* the handoff points the editor at the existing
`GET /schema-versions/{id}`, but that route is tenant-gated (`require("forms:read")`
+ `tenant_context`) — a platform operator without an active elevation grant gets
403 (`deps.py` platform path). The editor needs the schema document for task text
defaults and the placeholder namespace. The **published** version is the right one:
it is what a new draft will pin and be validated against.

### 2.3 `POST /prompts/{prompt_id}/preview` — stateless render

Body = `PromptDocument` (same as draft save). Renders against the schema's
published schema version. Persists nothing. Response:

```json
{"errors": ["task_overrides.wrap_up.outro: unknown placeholder {{patietn_name}}", …],
 "rendered": { …RenderedPrompts… }}
```

- HTTP 200 even when `errors` is non-empty: the renderer already tolerates content
  errors (unknown override keys / tokens pass through, defense in depth), and the
  editor calls this debounced while typing — a mid-keystroke 400 would repeatedly
  blank the preview. `errors` comes from the same `validate_prompt_document` used
  by draft save, so the strings are identical to the save-time 400 messages.
- Shape errors (missing session field, empty strings, extra keys) are still 422
  (pydantic body validation), same as draft save.
- 409 when no published schema version.
- Gated `platform:prompts:read`: it is a read-shaped dry run (renders, mutates
  nothing), POST only because the document travels in the body. Not idempotency-
  gated — non-mutating (the known idempotency gap on mutating prompt routes stays
  untouched/deferred).
- Response model `PromptPreview{errors: list[str], rendered: RenderedPrompts}`
  lives in the API layer; `vera_core.forms.prompting` models stay pure.

*Why:* the GET preview renders saved versions only; without this, every
tweak-and-look creates an immutable junk draft row. It is also the validation
authority, so the frontend never re-implements the placeholder-namespace rules.

## 3. Frontend architecture

Stack per repo conventions: local `useState` + typed api wrappers (no Redux beyond
auth, no react-query, no new deps). Route/gating unchanged: `/agent-prompt` under
`RequireAuth`/`AppShell`, page renders only for `selectIsSuperAdmin`.

### 3.1 Files

- **`src/lib/api/prompts.ts` (rewrite)** — types mirroring the pydantic models:
  `SessionBlock`, `TaskTextOverride`, `PromptDocument`, `PromptSummary`,
  `PromptVersionSummary`/`Detail` (with `schema_version_id`, `schema_version`),
  `RenderedTaskPrompt`, `RenderedPrompts`, `PromptSchemaDetail`, `PromptPreview`.
  Functions: `listPrompts`, `listPromptVersions`, `getPromptVersion`,
  `createPromptDraft(promptId, doc: PromptDocument)` (body IS the document),
  `publishPromptVersion`, `getPromptSchema(promptId)`,
  `previewPromptVersion(promptId, versionId?)` (GET),
  `previewPromptDocument(promptId, doc)` (POST). The old
  `CompositeJson`/`CreateDraftRequest` shapes are deleted.
- **`src/lib/prompts/document.ts` (new, pure)** — the unit-tested core:
  - `setOverrideField(doc, taskKey, field, text)` — creates/updates an override;
    an entry whose last field is cleared is dropped from `task_overrides`.
  - `clearOverrideField(doc, taskKey, field)` — the "reset to default" op
    (removal, never blanking — empty string is invalid server-side, min_length=1).
  - `overrideStateOf(doc, defaults, taskKey, field)` →
    `"overridden" | "default" | "no-default"` (schema task `intro`/`outro`/
    `prompt` are all optional).
  - `documentsEqual(a, b)` — dirty check (order-insensitive on `task_overrides`).
  - `parsePromptErrors(joined)` — splits the `"; "`-joined, location-prefixed
    server messages into `Record<location, string[]>` (array: one field can
    carry several errors, e.g. two unknown placeholders; the IBV
    `ValidationErrors` path-keyed idiom, pluralized).
  - `taskDefaultsOf(rawDoc)` → `[{task_key, title, intro?, outro?, prompt?}]` and
    `placeholderGroupsOf(rawDoc)` → `{system: [{token, path}], context:
    [{token, title}]}` (`system_fields` keys; `role:"context"` leaf paths, walking
    nested groups). These read the **raw** schema JSON with their own narrow
    types — the `ibv/types.ts` UI subset stays untouched (its header says tasks
    are intentionally absent).
- **`src/components/agent-prompt/`** — `SessionEditor.tsx`,
  `TaskOverrideEditor.tsx`, `OverrideFieldRow.tsx`, `PreviewPane.tsx`,
  `VersionRail.tsx`, `PlaceholderPicker.tsx`.
- **`src/pages/AgentPrompt.tsx` (rewrite)** — orchestration + layout.
  `agentPrompt.helpers.ts` keeps `pickInitialVersion` (unchanged semantics).

### 3.2 Layout

Header: title, **prompt selector** (`select`; fixes the current first-prompt-only
bug — dev has two prompts), pinned/published badges, **Save draft** (disabled when
pristine or client-invalid), page-level destructive `Alert` for unmapped errors.

Three-pane grid (`lg:` breakpoint; stacks on narrow):

- **Rail (left):** "Session" entry, then the schema's tasks (title + dot when the
  document overrides it), then a Versions list: `v{n}` + status `Badge` +
  `pinned schema v{m}` + created date; row actions **Load** and **Publish**
  (publish per existing idiom, confirm via `dialog`).
- **Editor (middle):** `SessionEditor` (three required textareas labeled
  Persona / Goal / Base instructions with help text taken from the
  `SessionBlock` `Field(description=…)` strings in `prompting.py`) or
  `TaskOverrideEditor` for the selected task (three `OverrideFieldRow`s:
  Intro — "spoken on task entry", Outro — "spoken on task exit",
  Instructions — "leads the compiled prompt; schema-derived questions and rules
  are appended after it").
- **Preview (right):** the selection's rendered text from the preview response —
  session text when "Session" selected; the task's `intro`/`outro`/compiled
  `prompt` when a task is selected. Monospace, scrollable.

### 3.3 Override interaction (per field row)

- **Not overridden, default exists:** muted read-only default text +
  badge `Schema default` + **Override** button — clicking it immediately writes
  the default text into the buffer as an override (document becomes dirty) and
  swaps the row to the editable state.
- **Not overridden, no default:** badge `No default` + **Add** button (empty
  editable textarea).
- **Overridden:** editable textarea + badge `Overridden` + **Reset to default**
  (removes the override field) + `collapsible` muted "Schema default" block
  underneath for comparison (omitted when there is no default).
- An override textarea left empty is a client-side inline error and blocks save
  (server would 422/400); reset is the removal path.

### 3.4 Data flow & preview semantics

Page state: `prompts`, `selectedPromptId`, `versions`, `schemaDetail` (parsed into
task defaults + placeholder groups + published schema version number),
`document` (editing buffer), `baseline` + `loadedVersionId` (dirty tracking),
`selection` (`"session"` | task_key), `preview` + `previewErrors`, busy/error
state — all local state, existing idiom (`cancelled` flag in effects).

- **Pristine buffer** (a loaded, unmodified version): GET
  `preview?version_id=<loaded>` — authoritative historical render. Pane header:
  `v5 · pinned schema v2`.
- **Dirty buffer:** debounced (~500 ms) POST preview. Pane header:
  `unsaved changes · renders against schema v3 (published)` — honest that saving
  pins the published schema even when the loaded version pinned an older one.
  `errors` from the response map inline (§3.6).
- **Load version** (rail click): confirm-discard if dirty, then buffer :=
  that version's `composite_json`. This IS the copy flow — load any version,
  edit, **Save draft** → new immutable draft (POST returns the new version;
  refresh list; it becomes the loaded version).
- **Publish**: per-row; refreshes list (one published per prompt, server
  demotes the prior).
- **Bootstrap (no versions at all):** seed the buffer's session block from GET
  preview's factory session text (`RenderedPrompts.persona/goal/
  base_instructions`), empty `task_overrides`. (Dev DBs always have factory v1;
  this is a degradation path, not a normal flow.)
- **409 on save/preview** (no published schema version): page-level alert
  explaining a schema must be published first.

### 3.5 Placeholder picker

Per editor textarea: an "Insert placeholder" button opening a `dialog` with a
search input and the two grouped lists (System fields: `{{member_id}}` + mapped
path; Context fields: `{{sections.….patient_gender}}` + field title). Selecting
inserts at the cursor (textarea ref + `selectionStart`). A static hint notes
`{{value}}` is only valid inside schema field prompts, not here (the API exempts
the token but it is meaningless in session/override text). No caret-anchored
autocomplete (no editor component in the stack; explicitly descoped).

### 3.6 Validation UX

- Client-side: session fields required non-empty; override textareas non-empty;
  Save disabled with inline messages. No client-side placeholder validation —
  the preview response's `errors` is the authority (identical strings to the
  save 400).
- Server errors (save 400 message and preview `errors[]`) run through
  `parsePromptErrors`: `session.<field>` → that session textarea;
  `task_overrides.<key>.<field>` → that task's row (red border + message text
  under the field, and a dot/indicator on the rail task);
  `task_overrides.<key>: …` (unknown task_key / empty entry — shouldn't happen
  from this UI) and anything unparsed → the page-level alert.

### 3.7 PHI / security posture

Everything on this page is global catalog template text — no PHI. Still: no
browser storage, no PHI-shaped logging, opaque UUIDs in requests only, per
`vera-frontend/CLAUDE.md`.

## 4. Testing

**Backend** (`tests/integration/control_plane/test_prompts.py` extensions):
- versions list/detail expose `schema_version_id` + `schema_version`.
- `GET /prompts/{id}/schema`: returns published document; 409 when schema
  version demoted; 403 for tenant user.
- `POST /prompts/{id}/preview`: renders a valid document (task override visible
  in output); reports content errors in `errors` with HTTP 200 while still
  rendering; 422 on shape errors; 409 without published schema; 403 for tenant
  user; creates no `prompt_version` row.
- `just check` green.

**Frontend** (vitest, house style — no testing-library):
- `src/lib/prompts/document.test.ts`: override set/clear/drop-empty-entry,
  provenance states (incl. no-default), dirty check, `parsePromptErrors`
  (mapped + unmapped), `taskDefaultsOf` / `placeholderGroupsOf` on a fixture
  document with nested groups and context leaves.
- `src/lib/api/prompts.test.ts`: module-mock `apiRequest` (the
  `voiceLab.test.ts` idiom) — paths, methods, bodies (draft body is the raw
  document, not `{composite_json}`).
- Component smoke tests via `renderToStaticMarkup` with props/fixtures
  (`OverrideFieldRow` provenance states; `PreviewPane` headers;
  `VersionRail` badges), following `SchemaForm.test.tsx`.
- `pickInitialVersion` tests kept.
- Gates: `tsc` + `eslint` + vitest + build; code-simplifier pass before done.

## 5. Out of scope

- Voice-lab "run a draft" integration (future; the rail exposes version ids and
  the GET preview honors `version_id`, which is the agreed convention).
- Version-to-version diff view (deferred; provenance + collapsible default only).
- Caret-anchored `{{` autocomplete (descoped in favor of the picker dialog).
- Idempotency-key gate on mutating prompt routes (known, pre-existing, deferred).
- The runtime task builder (separate task on this branch).

## 6. Edge cases

- Published prompt version pinning an older schema than the published one
  (post-republish, pre-carry-forward): rail row shows `pinned schema v2` while
  the header shows published schema v3; dirty-preview header makes the save
  target explicit.
- Task present in `task_overrides` but absent from the (published) schema's
  tasks (stale doc loaded from an old version): rendered preview ignores it;
  the rail lists schema tasks only, plus an "orphaned overrides" warning row so
  the operator can reset them (each shown with a remove action).
- Schema task with `sections: []` (ritual tasks): nothing special — instructions
  only; preview shows whatever the renderer returns.
- Task with no schema `intro`/`outro`/`prompt` and no override: field rows show
  `No default`; preview shows the compiled questions block only.
- Concurrent save/publish 409s ("please retry"): surfaced verbatim in the page
  alert; state refreshed.
