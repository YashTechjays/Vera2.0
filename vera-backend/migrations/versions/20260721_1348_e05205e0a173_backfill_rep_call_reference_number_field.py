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

Two independent guards:
- predicate: only rows whose OWN sections tree still has a call_reference_number
  leaf at the expected path for that insurance_type are eligible;
- idempotency: only rows that don't already carry the key are touched, so
  re-running this migration is a no-op.

Before mutating anything, upgrade() first counts rows that are dsl 2.x, missing
the key, AND do NOT resolve the expected leaf — if any exist, it aborts loudly
rather than silently leaving (or mismatching) a row; this should be impossible
given the path history above, but the check costs nothing and this table holds
compiled prompts for a HIPAA-regulated voice pipeline.

Only the two insurance types known at authoring time are covered
(infertility_treatment, disease_only) — any insurance type added after this
point is authored with the field already required (dsl.py enforces it at
compile time), so it never needs backfilling.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e05205e0a173"
down_revision: str | None = "c8921c9301da"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# insurance_type -> (section_key, field_key) for the pre-existing
# call_reference_number leaf.
_PATH_BY_INSURANCE_TYPE: dict[str, tuple[str, str]] = {
    "infertility_treatment": ("insurance_representative", "call_reference_number"),
    "disease_only": ("representative_details", "call_reference_number"),
}

# Rows eligible for backfill (dsl 2.x, key missing) whose sections tree does NOT
# have the expected leaf — should always count 0; a non-zero count aborts
# upgrade() before anything is mutated.
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
    for insurance_type, (section_key, field_key) in _PATH_BY_INSURANCE_TYPE.items()
)

# Exposed as a module constant so the integration test
# (tests/integration/db/test_backfill_rep_call_reference_number_field_migration.py)
# executes the EXACT statements the migration runs — the two cannot drift.
UPDATE_STATEMENTS: tuple[str, ...] = tuple(
    f"""
    UPDATE schema_version sv
    SET schema_json = (
        (sv.schema_json::jsonb) || jsonb_build_object(
            'rep_call_reference_number_field', 'sections.{section_key}.{field_key}'
        )
    )::json
    FROM form_schema fs
    WHERE fs.id = sv.schema_id
      AND fs.insurance_type = '{insurance_type}'
      AND (sv.schema_json ->> 'dsl_version') LIKE '2.%'
      AND NOT (sv.schema_json::jsonb ? 'rep_call_reference_number_field')
      AND COALESCE(
          ((sv.schema_json::jsonb) #> '{{sections,{section_key},fields}}') ? '{field_key}',
          FALSE
      )
    """
    for insurance_type, (section_key, field_key) in _PATH_BY_INSURANCE_TYPE.items()
)


def upgrade() -> None:
    conn = op.get_bind()
    for statement in UNRESOLVABLE_COUNT_STATEMENTS:
        remaining = conn.exec_driver_sql(statement).scalar_one()
        if remaining:
            raise RuntimeError(
                "backfill_rep_call_reference_number_field: "
                f"{remaining} schema_version row(s) are dsl 2.x, missing "
                "rep_call_reference_number_field, and do NOT resolve the "
                "expected call_reference_number leaf — investigate before "
                "re-running (see this migration's docstring)."
            )
    for statement in UPDATE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    # Backfilling only ever adds a key every dsl 2.x document is required to
    # carry anyway (dsl.py); nothing depends on removing it again.
    pass
