# Promoted fields as a typed DSL model (parity with PatientForm)

**Date:** 2026-07-11
**Status:** Approved for implementation
**Extends:** `2026-07-02-form-schema-dsl-v2-design.md` (grammar),
`2026-07-10-dispute-resolve-patient-form-promotion-design.md` (promotion lifecycle)

## Problem

`FormSchemaDoc.promoted_fields` is a free-form `dict[str, str] | None`
(`vera_core/forms/dsl.py`). The keys are exactly the promoted columns of the
`patient_form` table, but nothing ties them structurally to the
`PatientForm` model — column typos are only caught late by the
`PROMOTABLE_COLUMNS` frozenset check inside `_validate_document`, the whole
block is optional (a schema author can silently forget it, leaving worklist
columns `None` forever), and authoring a new schema means hand-writing magic
string keys.

## Decision (user-confirmed)

1. **`PromotedFields` pydantic model** replaces the dict. One attribute per
   promotable `patient_form` column; the class *is* the column list.
2. **Every attribute is required `str`** — no defaults, no `None`. A schema
   must map all eight columns to a leaf path. Restrictions can be loosened
   later without breaking already-compiled artifacts (required → optional is
   backward-compatible for parsing); the reverse is not, so start strict.
3. **`FormSchemaDoc.promoted_fields` becomes required.** A document without
   the block fails validation at authoring/compile time (and at load).
4. **`disease_only` is upgraded, not exempted.** It is a DSL capability-test
   schema; it grows the four fields it was missing so it can map all eight
   columns.

## Design

### 1. dsl.py — the model

```python
class PromotedFields(_Model):
    """patient_form column -> root-anchored leaf path.

    Attribute set mirrors PatientForm's promoted columns (searchable
    identifiers + worklist display fields); every schema must map all of
    them. Declaration order is the compiled-artifact key order.
    """

    patient_name: str
    patient_dob: str
    chart_number: str
    appointment_date: str
    appointment_type: str
    member_id: str
    insurance_provider: str
    insurance_provider_phone_number: str

    def items(self) -> list[tuple[str, str]]:
        """(column, leaf path) pairs, in declaration order."""
        return [(column, getattr(self, column)) for column in type(self).model_fields]
```

- Declaration order matches the current `ibv_form_standard_v2.json`
  serialization order, so that artifact's block stays byte-identical.
- `extra="forbid"` (inherited from `_Model`) + typed attributes: unknown
  columns fail at construction and mypy flags typos inside catalog modules.
- `FormSchemaDoc.promoted_fields: PromotedFields` — required, no default.
- The `PROMOTABLE_COLUMNS` frozenset is deleted; the "unknown column" branch
  of `_validate_document` goes with it. The remaining document-level checks
  stay and now iterate `promoted_fields.items()`:
  - every path resolves to a leaf;
  - every path is a `system_fields` target (guarantees intake presence).
- No compiler/loader changes: all values are plain required strings, so
  `compile_document`'s `exclude_none/exclude_defaults` dump and
  `load_document` round-trip untouched.

### 2. Catalog schemas

- **ibv_standard.py** — same eight mappings, re-keyed as `PromotedFields(...)`
  kwargs. Compiled artifact unchanged.
- **disease_only.py** — gains, mirroring ibv_standard's shapes:
  - `appointment_information` context section: `appointment_type`
    (enum, values `New Patient / Reverification / Follow Up Visit / N/A`,
    default `N/A`, required) and `appointment_date` (date, required,
    `DATE_VALIDATION`).
  - `insurance_reference_information` collect section:
    `insurance_provider_name` and `insurance_phone_number` `text_ask`
    leaves; the section is appended to the `wrap_up` task (before
    `representative_details` — ibv keeps these in its closing task too).
  - `system_fields` gains `appointment_date`, `appointment_type`,
    `insurance_provider_name`, `insurance_provider_phone_number` (same
    handles as ibv_standard).
  - `promoted_fields=PromotedFields(...)` mapping all eight columns.
  - Intake consequence (accepted): the new `system_fields` targets without
    a leaf `default` — `appointment_date`, `insurance_provider_name`,
    `insurance_phone_number` — become required intake fields for this
    schema. It is a dummy/test schema; that is the point of the exercise.

### 3. Consumers

- `intake.promote_columns` — iterates `doc.promoted_fields.items()` instead
  of `(doc.promoted_fields or {}).items()`. `PromotedIdentifiers` (the
  *value* container) keeps its `None` defaults: a mapped leaf can still
  legally yield `None` (leaf with `default` absent from payload,
  `chart_number` `"N/A"` normalization).
- `patient_forms.py` resolve path — `for column, _ in doc.promoted_fields.items()`;
  the `or {}` guard dies.
- No other runtime consumers (checked: grep across backend).

### 4. Parity guard

New unit test asserting three-way column parity so future column adds can't
drift:

```
set(PromotedFields.model_fields)
  == {f.name for f in dataclasses.fields(PromotedIdentifiers)}
  == the promoted PatientForm columns (patient_name … insurance_provider_phone_number)
```

(The `PatientForm` side is asserted against an explicit literal set — the
model has many non-promoted columns, so introspection can't derive "promoted"
automatically; the literal is the documented contract.)

### 5. Artifacts, seeding, tests

- `just compile-schemas`: `ibv_form_standard_v2.json` stays byte-identical —
  the model dumps to the same `{column: path}` JSON mapping in the same key
  order; `disease_only_verification.json` changes (new sections, task
  assignment, `system_fields` and `promoted_fields` entries).
- `just seed-schemas` republishes `disease_only` (order-sensitive equality);
  ibv is not republished since its bytes don't change.
- **Breaking for already-seeded rows:** any pinned `dsl_version: "2.1"`
  `schema_version` row lacking a full eight-key `promoted_fields` block now
  fails `FormSchemaDoc` validation on load (intake/review/resolve). New forms
  are unaffected (deploy runs `seed.py`, which republishes valid documents),
  but existing forms stay pinned via a RESTRICT FK — handled by the cleanup
  migration below. v1 documents never parse through `FormSchemaDoc`; untouched.

### 6. Cleanup data migration (user-confirmed: timestamp-gated destructive)

One alembic data migration that removes dev/test forms pinned to now-invalid
documents. Old documents cannot be backfilled (`disease_only`'s historical
sections lack the leaves the new columns must reference), so removal is the
only honest cleanup.

- **Predicate (both conditions must hold):**
  1. the form's pinned `schema_version.schema_json` has `dsl_version` `2.x`
     AND its `promoted_fields` is missing any of the eight required keys
     (`NOT COALESCE(jsonb ?& array[...eight keys...], false)` — `schema_json`
     is `JSON`, so cast to `jsonb` in the query);
  2. `patient_form.created_at < '2026-07-31T00:00:00Z'` — a hard-coded cutoff.
     Even in a worst-case future (validation loosened, predicate bug), no row
     created after July 2026 can ever match. The cutoff is deliberately a few
     weeks past authoring (not "today"): dev keeps creating forms pinned to the
     still-published block-less document until this branch deploys, and those
     must be swept too (verified 2026-07-11: origin/dev's artifact has no
     promoted_fields; a fresh dev form is pinned to the Jul-10 block-less
     version). Alembic additionally runs each revision once per DB, and on
     fresh DBs the tables are empty — no-op.
- **Delete order (FK-safe):** `call` rows of affected forms first (transcripts,
  call events, call-scoped oversight all CASCADE off `call`), then
  form-scoped oversight rows (`form_id` FK is RESTRICT), then the
  `patient_form` rows (`field_answer` CASCADEs). Stale `schema_version` rows
  are left in place — nothing loads a version no form pins, and deleting them
  would trip the `prompt_version` RESTRICT FK.
- **RLS note:** the affected tables are FORCE-RLS; the deletes only work on the
  privileged migration connection (`migration-database-url` in the deploy,
  the local superuser via `just migrate`) — which is exactly how migrations
  already run.
- **downgrade():** no-op with a comment (data deletion is irreversible).
- Test churn: `test_schema_dsl.py` / `test_intake.py` fixtures must carry a
  full `promoted_fields` block (plus backing leaves + `system_fields`); the
  "valid subset" test becomes "all eight required"; the "unknown column"
  test becomes a pydantic `extra` failure; `disease_only` catalog tests pick
  up the new sections.

## Error handling

- Missing block / missing column / unknown column / non-string value: pydantic
  `ValidationError` at `FormSchemaDoc` (or `PromotedFields`) construction —
  i.e. at authoring or compile, before any artifact exists.
- Path not a leaf / not a `system_fields` target: existing `_validate_document`
  errors, unchanged wording.

## Out of scope

- Loosening rules for hypothetical schemas that genuinely can't map a column
  (explicitly deferred by user: relax later if needed).
- Any `PatientForm` schema change; any frontend change (renderer is
  document-driven).
