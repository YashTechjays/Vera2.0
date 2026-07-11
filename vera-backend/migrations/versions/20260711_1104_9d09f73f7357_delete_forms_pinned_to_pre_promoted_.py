"""delete forms pinned to pre promoted_fields docs

One-time, timestamp-gated destructive cleanup (2026-07-11 design doc).
`promoted_fields` became a REQUIRED eight-key block on every dsl 2.x document;
forms pinned (RESTRICT FK) to older v2 schema_version rows can no longer parse
and cannot be backfilled (the old documents lack the leaves the new columns
must reference), so their pre-prod test forms are removed.

Two independent guards, so this can never eat future data:
- predicate: the pinned document is dsl 2.x AND its promoted_fields block is
  missing at least one required key — impossible for any document compiled
  after this change (dsl.py validation rejects it at authoring/compile/load);
- cutoff: only rows created before 2026-07-31 UTC qualify — headroom past
  the planned deploy (dev keeps creating forms pinned to the block-less
  document until then), while still guaranteeing a worst-case future
  (validation loosened, predicate bug) touches nothing created after July 2026.

Delete order honors the RESTRICT FKs: export_artifact and call first
(everything under call CASCADEs), then patient_form (field_answer CASCADEs).
Stale schema_version rows stay — nothing loads a version no form pins, and
prompt_version references them RESTRICT.

Runs on the privileged migration connection (BYPASSRLS) like every migration;
the affected tables are FORCE RLS, so an RLS-bound role would silently match
zero rows instead.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "9d09f73f7357"
down_revision: str | None = "39f81ad53651"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The eight keys PromotedFields requires (vera_core/forms/dsl.py).
_REQUIRED_KEYS = (
    "patient_name",
    "patient_dob",
    "chart_number",
    "appointment_date",
    "appointment_type",
    "member_id",
    "insurance_provider",
    "insurance_provider_phone_number",
)
_KEYS_SQL = ", ".join(f"'{k}'" for k in _REQUIRED_KEYS)
_CUTOFF = "2026-07-31 00:00:00+00"

_STALE_FORMS = f"""
    SELECT pf.id
    FROM patient_form pf
    JOIN schema_version sv ON sv.id = pf.schema_version_id
    WHERE pf.created_at < TIMESTAMPTZ '{_CUTOFF}'
      AND (sv.schema_json ->> 'dsl_version') LIKE '2.%'
      AND NOT COALESCE(
          jsonb_exists_all(
              (sv.schema_json::jsonb) -> 'promoted_fields', ARRAY[{_KEYS_SQL}]
          ),
          FALSE
      )
"""


# Exposed as a module constant so the integration test
# (tests/integration/db/test_promoted_fields_cleanup_migration.py) executes the
# EXACT statements the migration runs — the two cannot drift. Order honors the
# RESTRICT FKs (see docstring).
DELETE_STATEMENTS: tuple[str, ...] = (
    f"DELETE FROM export_artifact WHERE form_id IN ({_STALE_FORMS})",
    f"DELETE FROM call WHERE form_id IN ({_STALE_FORMS})",
    f"DELETE FROM patient_form WHERE id IN ({_STALE_FORMS})",
)


def upgrade() -> None:
    for statement in DELETE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    # Data deletion is irreversible; the removed rows were pre-prod test forms.
    pass
