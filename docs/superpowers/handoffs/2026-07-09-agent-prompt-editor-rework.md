# Handoff: `/agent-prompt` frontend rework for the new prompt structure

**Audience:** a fresh Claude session (or engineer) starting this task cold.
**Task:** rebuild the platform-admin `/agent-prompt` page in `vera-frontend/` against
the new prompt architecture. The backend is DONE — including the preview endpoint;
this task is frontend-only (plus any small, genuinely-missing backend affordance you
justify explicitly).

## 1. What changed underneath (read these first)

- `docs/superpowers/specs/2026-07-08-prompt-compiler-design.md` — the authoritative
  design. Key shift: prompts are **rendered at call time** from the schema;
  `prompt_version.composite_json` no longer stores compiled text. It stores a
  `PromptDocument` with two blocks of different semantics:
  - `session` — **literal content consumed as-is** (persona, goal,
    base_instructions). `prompt_version` is its home; a factory v1 is seeded per
    schema.
  - `task_overrides` — **sparse patches** over the schema's per-task
    `intro`/`outro`/`prompt` defaults. Effective text = `override ?? schema default`.
- `docs/superpowers/specs/2026-07-06-task-prompts-dsl-design.md` — where the schema's
  task text defaults come from (intro = spoken on task entry, outro = spoken on exit,
  prompt = agent instructions; `{{placeholder}}` namespace).
- Everything below is implemented and merged on branch
  `feat/schema-to-prompt-generation`; `just check` green (unit + integration on the
  dedicated `vera_test` DB).

## 2. Backend surface (all implemented — do not rebuild)

File: `vera-backend/apps/control_plane/src/control_plane/api/v1/prompts.py`.
All routes platform-gated: `platform:prompts:read` / `platform:prompts:write`.
URL prefix `/api/v1/prompts`. Response envelope: `ResponseModel[T]` (`{"data": …}`).

- `GET /prompts` → `PromptSummary[]` (`id, name, insurance_type, published_version`).
- `GET /prompts/{id}/versions` → `PromptVersionSummary[]` (`id, version, status,
  created_at`); `GET /prompts/{id}/versions/{vid}` → adds `composite_json`.
- `POST /prompts/{id}/versions` — body IS a `PromptDocument`:
  ```json
  {"kind": "prompt_document",
   "session": {"persona": "…", "goal": "…", "base_instructions": "…"},
   "task_overrides": {"wrap_up": {"outro": "…"}}}
  ```
  Every save creates an immutable DRAFT pinned to the schema's currently published
  `schema_version` (409 if none). Content validation → **400** with joined messages:
  `task_overrides.<key>: unknown task_key`, `task_overrides.<key>: empty override
  entry`, `<where>: unknown placeholder {{token}}`. Shape errors (missing session
  field, empty strings — min_length=1, extra keys) → **422**. Malformed body → 422.
- `POST /prompts/{id}/versions/{vid}/publish` — promotes, demotes prior published.
- `GET /prompts/{id}/preview?version_id=<optional uuid>` → `RenderedPrompts`:
  ```json
  {"name": "...", "insurance_type": "...", "dsl_version": "2.1",
   "persona": "...", "goal": "...", "base_instructions": "...",
   "tasks": [{"task_key": "...", "title": "...", "intro": "...?",
              "outro": "...?", "prompt": "<compiled instruction text>"}]}
  ```
  No `version_id` → published version; named `version_id` (any status, incl. drafts)
  → that version, rendered against the schema version **it pins**. No versions at
  all → factory session + pure schema defaults.
  This endpoint is the editor's read path for the preview pane AND the future
  voice-lab "test this draft" convention.
- Placeholder namespace (for editor affordances): a `{{token}}` is valid iff it is a
  `system_fields` key of the pinned schema OR the root-anchored path of a
  `role:"context"` leaf (e.g. `{{sections.patient_information.patient_gender}}`).
  `{{value}}` is exempt (field-level namespace). The schema document is fetchable
  via the existing `GET /schema-versions/{schema_version_id}` the form UI already
  uses.

Reference for exact behaviors: `vera-backend/tests/integration/control_plane/test_prompts.py`.
Renderer source of truth: `vera-backend/packages/vera_core/src/vera_core/forms/prompting.py`
(`render_task_prompts`, `PromptDocument`, `SessionBlock` — its `Field(description=…)`
texts define what each session field MEANS; surface those as help text).

## 3. Current frontend state (what you're replacing)

- `vera-frontend/src/pages/AgentPrompt.tsx` + `vera-frontend/src/lib/api/prompts.ts`
  were built for the OLD model (editing the whole compiled composite JSON). The API
  client shapes there are stale (`CreateDraftRequest{composite_json}` no longer
  exists — the draft body is the `PromptDocument` itself).
- Stack & conventions: React + Vite + TS + Redux Toolkit + shadcn; ES modules;
  `function` keyword for top-level functions; explicit return types; no nested
  ternaries; selectors as arrow-consts (see `authSlice`). `vera-frontend/CLAUDE.md`
  loads when you touch that tree — obey it (PHI rules: no PHI in browser storage).
- Gates: `tsc` + `eslint` + vitest + build. Repo rule: run the **code-simplifier**
  agent after implementation, then re-run gates, before claiming done.

## 4. UX requirements (settled during design — not open questions)

1. **Session editor** — three required text areas (persona / goal / base
   instructions), labeled with intent help text from `SessionBlock` descriptions.
   Literal content: no "default" concept here; what you see is what ships.
2. **Per-task override editor** — list the schema's tasks (from preview or the
   schema doc): for each, show effective intro/outro/prompt with **provenance**
   (schema default vs overridden). Editing a field creates/updates the override;
   an explicit "reset to default" removes that override field. Empty string is
   invalid (min_length=1) — reset is removal, not blanking.
3. **Rendered preview pane** — per task, from the preview endpoint; this is the
   operator's view of what the agent actually receives. Show which schema version
   the previewed prompt version pins ("pinned schema v3") — after a schema
   republish, the published prompt may briefly pin the prior schema version until
   the seeder carries it forward.
4. **Version flow** — every save = new immutable draft; version history list;
   publish button (one published per prompt); "copy" = load any version's document
   into the editor and save as a new draft.
5. **Validation UX** — surface the 400's per-message errors inline (they're
   prefixed with their location, e.g. `task_overrides.wrap_up.outro: unknown
   placeholder {{patietn_name}}`); a placeholder picker/autocomplete fed by the
   pinned schema's `system_fields` keys + context-leaf paths would prevent most of
   them.

## 5. Process expectations

Superpowers workflow: brainstorm (clarify UX details like layout/diff-view with the
user) → spec in `docs/superpowers/specs/` → plan in `docs/superpowers/plans/` →
subagent-driven execution. The frontend has no changes yet for this feature — the
UI-rendering subset of the schema deliberately ignores `tasks`/prompt constructs, so
nothing existing breaks; this is additive page work.

## 6. Adjacent facts / deferred items you may bump into

- Mutating prompt routes lack the idempotency-key gate sibling route files use —
  known, pre-existing, deferred; don't fix silently, flag if it bites.
- The runtime task builder (agent-worker consumption of `render_task_prompts`) is
  NOT built yet — the preview endpoint is currently the only renderer consumer.
- Voice-lab "run a draft" integration is future work; the editor only needs to
  expose version ids cleanly so that flow can attach later.
- Local dev data: dev DB is seeded with factory prompt v1 for both schemas
  (infertility pinned to schema v3). `just seed-prompts` is idempotent. A data
  migration (`be79c2989c97`) removed legacy-shaped prompt rows everywhere.
- Frontend integration tests hit the API — backend must run locally (`just up`,
  `just migrate`, `just api`; seed via `just seed`).
