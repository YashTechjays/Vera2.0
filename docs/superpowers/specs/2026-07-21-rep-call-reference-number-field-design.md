# Rep call reference number as a DSL concept (`rep_call_reference_number_field`)

**Date:** 2026-07-21
**Status:** Approved for implementation
**Extends:** `2026-07-02-form-schema-dsl-v2-design.md` (grammar),
`2026-07-11-promoted-fields-typed-model-design.md` (precedent for a required-field
DSL addition and its migration pattern)
**Related:** GitHub issue #114 (`FormSchemaDoc` validation isn't scoped per
`dsl_version` — the underlying architectural gap this design works around, not fixes)

## Problem

Every insurance-type schema's voice call ends by asking the representative for a
call reference number, but nothing in the DSL marks *which* leaf holds it. It
exists today only as an ordinary `ask`-role leaf
(`sections.insurance_representative.call_reference_number` for
`infertility_treatment`, `sections.representative_details.call_reference_number`
for `disease_only`), policed only by hand-written prompt prose in the `wrap_up`
task ("must be actual values — never accept 'None'..."). A retry mechanism that
needs to decide "did a previous attempt actually capture a reference number, or
should this be treated as a fresh call" has no generalized, schema-declared place
to look — it would have to hard-code the leaf path per insurance type, the same
anti-pattern `system_fields`/`promoted_fields` already solved for other
well-known values.

## Decision (user-confirmed)

1. **`rep_call_reference_number_field: str`** — a new required top-level key on
   `FormSchemaDoc`, a single root-anchored leaf path (not a dict, since there is
   exactly one such field per schema).
2. **Required, no default.** Every schema must declare it — same strictness
   posture as `promoted_fields` ("start strict; required → optional is
   backward-compatible for parsing, the reverse is not").
3. **Validated only for leaf existence** (`path in leaves`) — no additional
   role/type constraint, and (unlike `promoted_fields`) **no requirement to also
   be a `system_fields` target**. `system_fields` marks values known *before*
   the call; this value is collected *during* the call, so the two concepts are
   orthogonal.
4. **Scope: DSL concept only.** No change to `Call.call_reference_no`/
   `CallLineage` (still dead/unwired), no retry-mechanism logic, no frontend
   change (confirmed: the frontend's schema parser has no strict validation and
   already ignores `promoted_fields`/`stt_key_terms`).
5. **Backward compatibility: backfill, not delete.** Unlike `promoted_fields`
   (whose migration deleted pre-prod forms because the new value had no
   historical referent), `call_reference_number` has lived at the same leaf path
   since each schema's very first commit — so existing `schema_version` rows are
   patched in place via a data migration, not destroyed.

## Design

### 1. dsl.py — the model

```python
class FormSchemaDoc(_Model):
    ...
    promoted_fields: PromotedFields
    # Single root-anchored leaf path: where this insurance type's rep call reference
    # number lives. The one generalized place retry/resume logic reads regardless of
    # insurance type — empty/never-collected on a prior attempt means no valid retry
    # state (treat as a fresh call).
    rep_call_reference_number_field: str
    stt_key_terms: list[str] | None = None
```

Declaration order matches the reference example (`system_fields` →
`promoted_fields` → `rep_call_reference_number_field` → `stt_key_terms`),
preserving the "document key order is field order" convention for the compiled
artifact.

### 2. Validation (`_validate_document`)

Added next to the existing `system_fields`/`promoted_fields` blocks:

```python
if self.rep_call_reference_number_field not in leaves:
    errors.append(
        f"rep_call_reference_number_field: {self.rep_call_reference_number_field!r} "
        "does not resolve to a leaf"
    )
```

### 3. Catalog schemas

- `catalog/ibv_standard.py`:
  `rep_call_reference_number_field="sections.insurance_representative.call_reference_number"`,
  added after `promoted_fields=PromotedFields(...)`.
- `catalog/disease_only.py`:
  `rep_call_reference_number_field="sections.representative_details.call_reference_number"`,
  same position.

Both reuse the existing `call_reference_number` leaf — no new fields, no leaf
changes, no enum/CHECK migration (unlike adding a new insurance type).

### 4. Artifacts, seeding, tests

- `just compile-schemas` regenerates both `data/form_schemas/*.json` artifacts,
  reproducing the exact snippet from the original request.
- `just seed-schemas` republishes both schemas (the document text changed, so
  `_same_document`'s order-sensitive string compare fails and a new
  `schema_version` is inserted for each).
- `tests/unit/forms/test_schema_dsl.py`:
  - `minimal_doc()` fixture gains
    `"rep_call_reference_number_field": "sections.basics.plan_type"`.
  - New test: deleting the key raises `ValidationError` (required).
  - New test: pointing it at a non-existent path (`sections.basics.missing`)
    raises `ValidationError` ("does not resolve to a leaf").
  - New assertions that both compiled schemas expose the correct path.

### 5. Backward compatibility — breaking for already-seeded rows, and the fix

**Breaking, same mechanism as `promoted_fields`:** any pinned `dsl_version: "2.x"`
`schema_version` row lacking `rep_call_reference_number_field` fails
`FormSchemaDoc` validation the next time it's re-parsed. Unguarded,
production-critical call sites: `patient_forms.py` intake (`:206`) and
`resolve_disputes` (`:702`) — 500s; `queue_dispatcher.py`'s
`_resolve_plan_template` and `ivr_selection.py` — caught, but the form silently
never dispatches again; `field_answers.py`'s `recompute_form_projection` via
`worker_events.py` — caught by the outer consumer loop, but the Redis Streams
entry redelivers forever. (The root cause — `FormSchemaDoc` validation isn't
scoped to a document's own `dsl_version` — is tracked separately as GitHub issue
#114; this design works around it rather than fixing it.)

**Why this one is backfillable, unlike `promoted_fields`:** `call_reference_number`
has lived at the exact same leaf path since each schema's very first commit
(`infertility_treatment`: `3675c19d`; `disease_only`: `eaf1484e`) and has never
moved (`git log --follow -p` on both catalog modules confirms no
rename/relocation). So the correct value for every historical row is knowable and
can be written back — no data needs to be deleted.

**Migration** (Alembic, `vera-backend/migrations/versions/`, one-time data
migration alongside the `dsl.py` change): backfills
`rep_call_reference_number_field` into every `schema_version` row (both
`infertility_treatment` and `disease_only`, published and demoted alike) that is
`dsl_version` `2.x` and doesn't already carry the key.

**Revised during final review**: an earlier version of this migration wrote the
backfilled value via `SET schema_json = (schema_json::jsonb || jsonb_build_object(...))::json`
— a single SQL `UPDATE`. The final whole-branch review caught that this is
wrong: `schema_json` is Postgres `json`, not `jsonb`, *specifically* because
document key order is field/section order (`forms/CLAUDE.md`,
"Semantics worth remembering"). Casting through `jsonb` silently re-sorts every
object's keys at every nesting level — Postgres jsonb orders keys by length
then byte value, not insertion order — so the backfilled rows (exactly the
ones still serving in-flight/retrying forms) would have come back with
scrambled task/question order (`queue_dispatcher.py` re-parses and
re-compiles the pinned document's prompts at dispatch, bucketing and numbering
questions in document order) and scrambled field order in the review UI. The
fix (already implemented, not merely proposed) reads each eligible row's raw
JSON **text** (`::text`, never `::jsonb`) and patches it in Python
(`json.loads` / mutate / `json.dumps`, which preserve key order exactly, unlike
a jsonb round-trip) before writing it back — so the "patch in place, nothing
else changes" guarantee is actually true, not just true for parsing.

Pure-SQL-only isn't possible for the write step under this constraint (there's
no `jsonb`-free way to add one key while a Postgres query is running), so the
guard/count queries stay pure SQL (read-only, so casting to `jsonb` there is
harmless), and the actual patch runs in Python — statements and the patch
function are exposed as module-level constants (`UNRESOLVABLE_COUNT_STATEMENTS`,
`SELECT_ELIGIBLE_STATEMENTS`, `patch_document`, `abort_if_unresolvable`) so an
integration test exercises the exact logic the migration runs, mirroring
`9d09f73f7357`'s "statements as a tested module constant" pattern as closely as
the order-preservation requirement allows:

```python
PATH_BY_INSURANCE_TYPE: dict[str, tuple[str, str]] = {
    "infertility_treatment": ("insurance_representative", "call_reference_number"),
    "disease_only": ("representative_details", "call_reference_number"),
}

# Rows eligible for backfill (dsl 2.x, key missing) whose sections tree does NOT
# have the expected leaf — should always count 0; non-zero aborts upgrade().
# Read-only: casting to jsonb here is safe, the result is never written back.
UNRESOLVABLE_COUNT_STATEMENTS: tuple[str, ...] = tuple(
    f"""SELECT count(*) FROM schema_version sv JOIN form_schema fs ON fs.id = sv.schema_id
        WHERE fs.insurance_type = '{insurance_type}'
          AND (sv.schema_json ->> 'dsl_version') LIKE '2.%'
          AND NOT (sv.schema_json::jsonb ? 'rep_call_reference_number_field')
          AND NOT COALESCE(((sv.schema_json::jsonb) #> '{{sections,{section_key},fields}}') ? '{field_key}', FALSE)"""
    for insurance_type, (section_key, field_key) in PATH_BY_INSURANCE_TYPE.items()
)

# Rows eligible for backfill (dsl 2.x, key missing, leaf resolves). Selects the
# RAW json text (::text, never ::jsonb) so patch_document() can preserve order.
SELECT_ELIGIBLE_STATEMENTS: tuple[str, ...] = tuple(
    f"""SELECT sv.id, sv.schema_json::text AS schema_json_text
        FROM schema_version sv JOIN form_schema fs ON fs.id = sv.schema_id
        WHERE fs.insurance_type = '{insurance_type}'
          AND (sv.schema_json ->> 'dsl_version') LIKE '2.%'
          AND NOT (sv.schema_json::jsonb ? 'rep_call_reference_number_field')
          AND COALESCE(((sv.schema_json::jsonb) #> '{{sections,{section_key},fields}}') ? '{field_key}', FALSE)"""
    for insurance_type, (section_key, field_key) in PATH_BY_INSURANCE_TYPE.items()
)

UPDATE_ROW_SQL = "UPDATE schema_version SET schema_json = CAST(:doc AS json) WHERE id = :id"


def patch_document(schema_json_text: str, path: str) -> str:
    """Order-preserving: json.loads/json.dumps keep every existing key's
    position at every nesting level exactly as it was."""
    doc = json.loads(schema_json_text)
    doc["rep_call_reference_number_field"] = path
    return json.dumps(doc)


def abort_if_unresolvable(count: int, insurance_type: str) -> None:
    if count:
        raise RuntimeError(f"... investigate before re-running (insurance_type={insurance_type!r})")


def upgrade() -> None:
    conn = op.get_bind()
    for insurance_type, statement in zip(
        PATH_BY_INSURANCE_TYPE, UNRESOLVABLE_COUNT_STATEMENTS, strict=True
    ):
        abort_if_unresolvable(conn.exec_driver_sql(statement).scalar_one(), insurance_type)

    for (section_key, field_key), statement in zip(
        PATH_BY_INSURANCE_TYPE.values(), SELECT_ELIGIBLE_STATEMENTS, strict=True
    ):
        path = f"sections.{section_key}.{field_key}"
        for row_id, schema_json_text in conn.exec_driver_sql(statement).all():
            conn.execute(
                sa.text(UPDATE_ROW_SQL),
                {"doc": patch_document(schema_json_text, path), "id": row_id},
            )


def downgrade() -> None:
    pass  # only ever adds a key every dsl 2.x document is required to carry anyway
```

Two guards, matching the precedent's philosophy: (1) idempotency — only rows
missing the key are touched, so a re-run is a no-op; (2) a pre-flight count
(`abort_if_unresolvable`, isolated so it's independently unit-testable) that
aborts the whole migration (transaction rolls back) before any row is patched
if any eligible row's own `sections` tree doesn't actually have the expected
leaf — should never fire per the git-history check above, but costs nothing
given this table drives a HIPAA-regulated voice pipeline. Because the guard is
fail-closed, a genuinely unresolvable row makes `upgrade()` abort on every
re-run until investigated — by design, not a flaky migration.

**Test:**
`tests/integration/db/test_backfill_rep_call_reference_number_field_migration.py`,
mirroring `test_promoted_fields_cleanup_migration.py` — fixture `schema_version`
rows per insurance type: one missing the key with the leaf present (gets
backfilled), one already carrying the key (idempotency — untouched, asserted via
a second run), a v1 doc with no `dsl_version` (ignored), one
deliberately-mismatched row (leaf missing from its `sections` tree, asserted via
`UNRESOLVABLE_COUNT_STATEMENTS`), and — the regression test for the ordering
bug above — a row with deliberately non-alphabetical sibling keys, asserting
the backfilled document's existing key order is untouched at every nesting
level and the new key is simply appended. `abort_if_unresolvable` gets its own
DB-free unit tests (raises on a nonzero count, no-ops on zero) since it's the
one piece of `upgrade()`'s control flow no SQL-level test exercises.

**Deploy ordering:** the migration runs via `just migrate` alongside the code
deploy that makes the field required, so by the time the new validator is live,
every existing row (published or demoted) already satisfies it. `just
seed-schemas` then republishes a fresh, correctly-key-ordered `schema_version`
per type for new forms going forward; the backfilled rows keep serving whatever
forms are still pinned to them.

### 6. Docs

- `vera_core/forms/CLAUDE.md`: add `rep_call_reference_number_field` to the
  "Validator rules that bite first" list; a short mention under "Semantics
  worth remembering" (mirrors how `promoted_fields`/`system_fields` are
  documented there).

## Error handling

- Missing key: pydantic `ValidationError` at `FormSchemaDoc` construction
  (authoring/compile/load) — same as `promoted_fields`.
- Path not a leaf: existing `_validate_document` error path, same wording style
  as `system_fields`.
- Pre-existing `schema_version` rows that can't be backfilled (should not exist,
  per git history): the migration aborts loudly rather than silently leaving or
  mismatching a row.

## Out of scope

- Fixing the underlying architectural gap (`FormSchemaDoc` validation not scoped
  per `dsl_version`) — tracked as GitHub issue #114.
- Any retry-mechanism logic that reads this field to decide fresh-call vs.
  resume — a separate, larger future task.
- Wiring `Call.call_reference_no`/`CallLineage` (still dead/unwired) or a
  `validation.reject_placeholders`-style enforcement of the "no placeholder
  values" rule (already deferred by the original v2 design spec, §10).
- Any frontend change (confirmed unnecessary).
