# vera_core.forms — form-schema DSL v2 (scoped)

Inherits the repo root `vera-backend/CLAUDE.md`. This package owns the form-schema
DSL: the single source of truth that drives UI rendering, voice-agent prompt
generation, and transcript extraction. Full grammar + consumer contracts:
`docs/superpowers/specs/2026-07-02-form-schema-dsl-v2-design.md` (§4 grammar,
§4.10 validation rules, §5 consumer contracts).

## Layout

- `dsl.py` — the typed contract: pydantic models ARE the grammar (`FormSchemaDoc`,
  `Section`, `Leaf`, `Group`, `Task`, conditions), plus the document validator,
  `compile_document` / `load_document`.
- `authoring.py` — macros for authoring (`enum_ask`, `text_ask`, `cpt_group`,
  `money_triplet`, `eq`/`ref`, …). Reuse these; don't hand-roll dicts.
- `catalog/` — one author module per schema (`ibv_standard.py`, `disease_only.py`)
  returning a `FormSchemaDoc`, registered in `catalog/__init__.py`.
- `conditions.py` — runtime condition evaluator + leaf gate-chain walk (mirrors the
  frontend evaluator; keep semantics in sync with
  `vera-frontend/src/lib/ibv/conditions.ts`).
- `intake.py` / `review.py` — the DB-free endpoint readers, version-gated on
  `dsl_version` (v1 documents keep working through the legacy branches).
- Compiled artifacts: `data/form_schemas/*.json` + `manifest.json`
  (`{file, insurance_type}` entries consumed by the seeder).

## Prime rule: compiled JSON is generated output

**Never hand-edit `data/form_schemas/*.json`.** Change the catalog module, then run
`just compile-schemas`. The freshness test (`tests/unit/forms/test_schema_dsl.py`)
fails CI on any drift, and round-trip (`load → compile` = identity) must hold.

## Adding a NEW schema / insurance type — checklist

1. **Enum + CHECK migration.** Add the member to `InsuranceType`
   (`vera_core/models/enums.py`). `form_schema.insurance_type` is DB-CHECK-
   constrained (`ck_form_schema_insurance_type_valid`), so write a migration that
   drops + recreates that constraint with the new value list (`just makemigration`,
   then hand-edit). ⚠ Autogenerate also emits unrelated index drops from known
   model/DB drift (`ix_audit_log_tenant_seq`, `ix_auth_audit_log_*`) — delete those
   ops, keep only your change, and give `downgrade()` a real reverse (delete the
   new type's rows before narrowing the CHECK).
2. **Author the catalog module** (`catalog/<name>.py`, a `build_<name>()` returning
   `FormSchemaDoc(dsl_version="2.1", …)`) and register it in `catalog/__init__.py`.
3. **Compile**: `just compile-schemas` writes the artifact; add a `manifest.json`
   entry (keep existing entries).
4. **Seed**: `just seed-schemas` publishes a `schema_version` (idempotent; the
   equality check is **order-sensitive** — a reordered document republishes).
5. **Gates**: `just fmt && just lint && uv run mypy`, then pytest — see the root
   CLAUDE.md test notes; some enum-asserting tests (`tests/unit/db/test_authoring.py`)
   enumerate `InsuranceType` and need updating.
6. **Frontend needs NO changes.** The UI fetches the exact document a form is
   pinned to via `GET /schema-versions/{schema_version_id}` and renders everything
   from it (layout, tables, conditions, color coding, legend, modal title).

## Validator rules that bite first (full list: spec §4.10)

- Every `collect` section belongs to **exactly one** task; `context`/`ui_only`
  sections to none. `tasks` is required (may be `[]` only if no collect sections).
- `ask`-role leaves need `prompt.ask`; `confirm`-role need `prompt.confirm`;
  enums need `values`.
- All condition / `system_fields` / `ask_groups` / `alternatives` /
  `contradictions.fields` paths are **root-anchored** (`sections.<key>...`) and
  must resolve to defined leaves.
- `promoted_fields` is REQUIRED and total: a `PromotedFields` block mapping all
  eight patient_form columns; each path must resolve to a leaf AND be a
  `system_fields` target.
- `rep_call_reference_number_field` is REQUIRED: a single root-anchored path
  naming which leaf holds the representative's call reference number. Only
  checked for leaf existence — unlike `promoted_fields` it does NOT need to be
  a `system_fields` target (this value is collected during the call, not known
  beforehand).
- `inapplicable_value` is only legal where self or an ancestor carries
  `applicable_when`.

## Semantics worth remembering

- **Document key order IS field/section order** (spec §4.1). That's why
  `schema_version.schema_json` / `prompt_version.composite_json` are Postgres
  `JSON`, not `JSONB` (JSONB re-sorts keys) — don't "normalize" them back.
- `field_answer.field_path` is byte-identical to the schema path — one namespace
  across schema, conditions, intake, extraction and the UI values map.
- Leaf `role` drives everything downstream: `ask`/`confirm` = collected on the
  call; `context` = injected as agent background; `input`/`readonly` (and every
  leaf of a `ui_only` section) = never voice-touched. `system_fields` binds
  platform handles to paths and wins over role in the UI color coding.
- **Role decides whether an intake value may settle a GATE.** `call_plan.gating_seed` drops
  `ask`-role paths from the worker's pre-call baseline: an `ask` leaf is collected on the
  call, so a value on file for one is a baseline, never an answer. `confirm` stays (on file
  to be read back), `context`/`input` stay (clinic-supplied). `PlanRunController.update_answers`
  MERGES the call's answers onto that baseline — a wholesale replace puts the intake values
  back, which is why the Observer pushes `_recorded` and not its full `_on_file` map.
- **`default` is an export/completion fallback, never an answer.** The export writes it when
  nothing was collected (`export_form_sheet`) and `completion_pct_v2` counts it filled; the
  call's owed set (`owed_now`) ignores it. The intake UI materializes it into `field_answer`
  at create, so a `default` on a leaf that GATES another question used to delete that question
  from the compiled prompt — `validate_confirm_defaults` rejects the confirm case, and
  `gating_seed` makes the ask case inert.
- `rep_call_reference_number_field` is the one generalized place to look for a
  schema's rep call reference number, regardless of insurance type — a retry
  mechanism reads it to decide whether a previous attempt already captured a
  valid reference number (empty/never-collected means treat the retry as a
  fresh call). See
  `docs/superpowers/specs/2026-07-21-rep-call-reference-number-field-design.md`.
- Bumping the grammar (`dsl_version`) means updating: the `Literal` in `dsl.py`,
  the version gates in `intake.py`/`review.py`/`conditions.is_v2`, and the
  frontend `parseSchema` guard.
