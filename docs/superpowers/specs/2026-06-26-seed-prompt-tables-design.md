# Seed `prompt` / `prompt_version` (mirror of form-schema seeding)

**Date:** 2026-06-26
**Branch:** `feat/seed-prompts`
**Status:** Design approved, pending implementation

## Problem

The authoring catalog has two prompt tables — `prompt` (the family) and
`prompt_version` (immutable versioned content) — that are currently **never
seeded**. We already seed the parallel `form_schema` / `schema_version` pair from
JSON files under `data/form_schemas/` (`scripts/seed.py:_seed_form_schemas`). We
want the same mechanism for prompts so a baseline prompt lands in the tables.

Prompt **generation from a schema** (the `vera-schema-builder` POC direction) is
explicitly **out of scope** here — for now we load a complete prompt document
as-is, exactly like form schemas. The first prompt's content is a **placeholder**;
real prompt content is swapped in later by editing the JSON file.

## Model facts (already exist — no migration)

From `packages/vera_core/src/vera_core/models/authoring.py`:

- **`prompt`** — `id`, `schema_id` (FK → `form_schema.id`, CASCADE), `name`. No
  unique constraint → idempotency is keyed in code on `(schema_id, name)`.
- **`prompt_version`** — `id`, `prompt_id` (FK → `prompt.id`, CASCADE),
  `schema_version_id` (FK → `schema_version.id`, **RESTRICT, NOT NULL**),
  `version` (int), `composite_json` (JSONB), `status` (draft/published).
  `UniqueConstraint(prompt_id, version)`.

Two deliberate asymmetries vs. `schema_version` to honor:

1. `prompt_version` has **no `published_at` column** — do not set one.
2. `prompt_version` has **no DB-level "one published per prompt" partial index**
   (unlike `schema_version`). We still enforce single-published in the seed code
   for consistency, but the DB does not enforce it.

`schema_version_id` is NOT NULL + RESTRICT, so every prompt version is permanently
bound to one immutable schema version — the prompt cannot be seeded without a
published schema version to bind to.

## Scope

### In scope
- New `data/prompts/` dir: `manifest.json` + one placeholder prompt JSON file.
- New `_seed_prompts(session)` in `scripts/seed.py`, idempotent + versioned.
- Wire into `seed()` after `_seed_form_schemas`; add `seed_prompts()` entry point,
  a `--prompts` CLI flag, and a `just seed-prompts` recipe (mirrors `seed-schemas`).

### Out of scope (YAGNI)
- Prompt generation from a schema (deferred).
- New tables / migrations (`prompt` / `prompt_version` already exist).
- Any API endpoint or frontend surface — seed-only.
- Seeding `prompt_version` from the POC's `compose/*.json` — placeholder only.

## Design

### 1. Data files — `vera-backend/data/prompts/`

Mirrors `data/form_schemas/`:

```
data/prompts/
├── manifest.json
└── ibv_standard_prompt.json
```

`manifest.json` — maps each prompt file to the schema it binds to (by the same
`insurance_type` key form schemas are seeded under):
```json
[{ "file": "ibv_standard_prompt.json", "insurance_type": "infertility_treatment" }]
```

`ibv_standard_prompt.json` — placeholder content; `name` becomes `prompt.name`:
```json
{ "name": "IBV Standard Prompt", "blocks": [], "note": "placeholder — real prompt TBD" }
```

### 2. `_seed_prompts(session)` in `scripts/seed.py`

For each manifest entry:

1. **Resolve the bound schema version.** Look up `form_schema` by
   `insurance_type`; then its **published** `schema_version`. If either is missing
   (e.g. schemas not seeded), print a warning and skip that entry — never crash the
   whole seed.
2. **Find-or-create the `prompt`** keyed on `(schema_id, name)`, where `name` is the
   prompt file's top-level `"name"`.
3. **Version the content** (same shape as `_seed_form_schemas`):
   - If a published `prompt_version` exists with identical `composite_json` → no-op.
   - Otherwise: demote the current published version (if any) to `draft`, then
     insert a new `prompt_version` with `version = max(version)+1`,
     `status = published`, `composite_json` = the file document, and
     `schema_version_id` = the published schema version from step 1.

Idempotent and keyed, exactly like `_seed_form_schemas`. Returns a short summary
list (e.g. `"IBV Standard Prompt v1 (published)"`) for the seed print line.

### 3. Wiring

- In `seed()`, call `await _seed_prompts(session)` **after**
  `await _seed_form_schemas(session)` (dependency: published schema version).
- Add `seed_prompts()` (mirrors `seed_schemas()`), a `--prompts` arg in
  `__main__`, and a `just seed-prompts` recipe (`uv run python scripts/seed.py --prompts`).
- Extend the `seed()` summary print line to include the prompt summary.

### 4. Data flow

```
data/prompts/manifest.json ──▶ insurance_type ──▶ form_schema ──▶ published schema_version
                                                                          │
data/prompts/ibv_standard_prompt.json ──(composite_json)──▶ prompt_version ┘
                                          name ──▶ prompt (schema_id, name)
```

## Error handling

| Case | Behavior |
|---|---|
| Schema for `insurance_type` not seeded / no published version | Print warning, skip that prompt entry (don't crash the seed). |
| Manifest file missing / malformed JSON | Surfaces as the normal file/JSON read error (same as form schemas). |
| Re-run with unchanged prompt JSON | No-op (idempotent). |
| Re-run with changed prompt JSON | Demote old published → draft, insert new published version. |

## Testing

The logic is DB CRUD (not separable pure functions), so verification is behavioral:

1. `just up` → `just migrate` → `just seed`: confirm a `prompt` row + a published
   `prompt_version` (v1) bound to the IBV schema version.
2. **Run `just seed` again** → confirm idempotency: no duplicate `prompt` /
   `prompt_version` rows, summary reports "(unchanged)".
3. `just check` (ruff + mypy) passes.

If an existing seed integration test is present, extend it to assert the prompt
rows; otherwise the above manual idempotency check stands.

## Files touched

| File | Change |
|---|---|
| `data/prompts/manifest.json` | **new** |
| `data/prompts/ibv_standard_prompt.json` | **new** (placeholder) |
| `scripts/seed.py` | edit — add `_seed_prompts`, `seed_prompts`, `--prompts`, wire into `seed()` |
| `justfile` | edit — add `seed-prompts` recipe |

No migrations. No new dependencies.
