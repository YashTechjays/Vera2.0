"""backfill rep_call_reference_number_field into existing schema_version rows

`rep_call_reference_number_field` became a REQUIRED top-level key on every dsl
2.x FormSchemaDoc (vera_core/forms/dsl.py). schema_version rows are immutable
and patient_form.schema_version_id RESTRICTs forever (see GitHub issue #114 for
the underlying gap: FormSchemaDoc validation isn't scoped per dsl_version), so
any already-persisted schema_version document missing this key fails
FormSchemaDoc validation the next time it's re-parsed — dispute-resolve
(patient_forms.py), retry dispatch (queue_dispatcher.py), and mid-call answer
recompute (field_answers.py / worker_events.py) all re-validate a form's own
pinned document.

Unlike the promoted_fields precedent (9d09f73f7357, a destructive delete), the
value here is safely backfillable: `call_reference_number` has lived at the
exact same leaf path since each schema's very first commit
(infertility_treatment: 3675c19d, disease_only: eaf1484e) and has never moved,
so every historical row can be patched with the correct value in place instead
of losing data.

**Why this is Python-driven instead of a single `jsonb` UPDATE**: `schema_json`
is Postgres `json`, not `jsonb`, specifically because document key order IS
field/section order (forms/CLAUDE.md, "Document key order IS field/section
order"; models/authoring.py). A `SET schema_json = (schema_json::jsonb || ...)
::json` merge silently re-sorts every object's keys at every nesting level on
the way through `jsonb` — which would corrupt task/question order for a
form mid-retry (queue_dispatcher.py re-parses and re-compiles the pinned
document's prompts at dispatch) and field order in the review UI, on exactly
the historical rows this migration exists to keep serving unchanged. Instead,
eligible rows are read as raw text (`::text`, never `::jsonb`), patched with
the one new key via `patch_document` (plain `json.loads`/`json.dumps`, which
preserve key order exactly like the compiler does), and written back —
nothing about the existing document is disturbed except the one addition.

Two independent guards:
- predicate: only rows whose OWN sections tree still has a call_reference_number
  leaf at the expected path for that insurance_type are eligible;
- idempotency: only rows that don't already carry the key are touched, so
  re-running this migration is a no-op.

Before mutating anything, upgrade() first counts rows that are dsl 2.x, missing
the key, AND do NOT resolve the expected leaf — if any exist, it aborts loudly
rather than silently leaving (or mismatching) a row; this should be impossible
given the path history above, but the check costs nothing and this table holds
compiled prompts for a HIPAA-regulated voice pipeline. Because the guard is
fail-closed, a genuinely unresolvable row makes upgrade() abort on every
re-run until it's investigated — by design, not a flaky migration.

Only the two insurance types known at authoring time are covered
(infertility_treatment, disease_only) — any insurance type added after this
point is authored with the field already required (dsl.py enforces it at
compile time), so it never needs backfilling.
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e05205e0a173"
down_revision: str | None = "c8921c9301da"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# insurance_type -> (section_key, field_key) for the pre-existing
# call_reference_number leaf.
PATH_BY_INSURANCE_TYPE: dict[str, tuple[str, str]] = {
    "infertility_treatment": ("insurance_representative", "call_reference_number"),
    "disease_only": ("representative_details", "call_reference_number"),
}

# Rows eligible for backfill (dsl 2.x, key missing) whose sections tree does NOT
# have the expected leaf — should always count 0; a non-zero count aborts
# upgrade() before anything is mutated. Read-only: casting to jsonb here is
# safe, the result is never written back.
UNRESOLVABLE_COUNT_STATEMENTS: tuple[str, ...] = tuple(
    f"""
    SELECT count(*)
    FROM schema_version sv
    JOIN form_schema fs ON fs.id = sv.schema_id
    WHERE fs.insurance_type = '{insurance_type}'
      AND (sv.schema_json ->> 'dsl_version') LIKE '2.%'
      AND NOT (sv.schema_json::jsonb ? 'rep_call_reference_number_field')
      AND NOT COALESCE(
          ((sv.schema_json::jsonb) #> '{{sections,{section_key},fields}}') ? '{field_key}',
          FALSE
      )
    """
    for insurance_type, (section_key, field_key) in PATH_BY_INSURANCE_TYPE.items()
)

# Rows eligible for backfill (dsl 2.x, key missing, leaf resolves). Selects the
# RAW json text (::text, never ::jsonb) so patch_document() can preserve the
# exact original key order at every level — see module docstring.
SELECT_ELIGIBLE_STATEMENTS: tuple[str, ...] = tuple(
    f"""
    SELECT sv.id, sv.schema_json::text AS schema_json_text
    FROM schema_version sv
    JOIN form_schema fs ON fs.id = sv.schema_id
    WHERE fs.insurance_type = '{insurance_type}'
      AND (sv.schema_json ->> 'dsl_version') LIKE '2.%'
      AND NOT (sv.schema_json::jsonb ? 'rep_call_reference_number_field')
      AND COALESCE(
          ((sv.schema_json::jsonb) #> '{{sections,{section_key},fields}}') ? '{field_key}',
          FALSE
      )
    """
    for insurance_type, (section_key, field_key) in PATH_BY_INSURANCE_TYPE.items()
)

# A single row's write: bind params keep the JSON text out of the SQL literal.
# CAST(...), not `:doc::json` — a bind param immediately followed by `::` trips
# up text()'s bind-parameter parsing (it never recognizes `:doc` as a param at
# all, leaving it as a literal `:doc` sent to the driver, which then fails to
# parse it as SQL).
UPDATE_ROW_SQL = "UPDATE schema_version SET schema_json = CAST(:doc AS json) WHERE id = :id"


def patch_document(schema_json_text: str, path: str) -> str:
    """Add rep_call_reference_number_field to a compiled document's raw JSON
    text without disturbing any existing key's position at any nesting level —
    a plain dict/json round-trip (no jsonb), since json.loads/json.dumps
    preserve key insertion order exactly, unlike a Postgres jsonb cast."""
    doc = json.loads(schema_json_text)
    doc["rep_call_reference_number_field"] = path
    return json.dumps(doc)


def abort_if_unresolvable(count: int, insurance_type: str) -> None:
    """The guard's abort decision, isolated from SQL execution so it can be
    unit-tested directly against a count without a database."""
    if count:
        raise RuntimeError(
            "backfill_rep_call_reference_number_field: "
            f"{count} schema_version row(s) for insurance_type={insurance_type!r} "
            "are dsl 2.x, missing rep_call_reference_number_field, and do NOT "
            "resolve the expected call_reference_number leaf — investigate "
            "before re-running (see this migration's docstring)."
        )


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
    # Backfilling only ever adds a key every dsl 2.x document is required to
    # carry anyway (dsl.py); nothing depends on removing it again.
    pass
