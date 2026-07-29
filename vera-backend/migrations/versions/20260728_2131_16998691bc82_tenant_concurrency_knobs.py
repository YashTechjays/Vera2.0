"""tenant concurrency knobs

Revision ID: 16998691bc82
Revises: e05205e0a173
Create Date: 2026-07-28 21:31:16.148323

"""

from collections.abc import Sequence

from alembic import op

revision: str = "16998691bc82"
down_revision: str | None = "800f6a788346"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Statements run by `upgrade()` (repo convention: exposed for migration tests).
UPGRADE_STATEMENTS: tuple[str, ...] = (
    # Idempotent: a fresh DB already has the column via 0001's create_all off the
    # live models; only an already-provisioned DB needs the ADD (repo migration rule).
    "ALTER TABLE tenant ADD COLUMN IF NOT EXISTS max_concurrent_calls INTEGER",
    # Behavior-preserving backfill: the dispatcher's tenant-wide cap used to read
    # max_agents_per_va, so existing tenants keep exactly their current capacity.
    "UPDATE tenant SET max_concurrent_calls = max_agents_per_va WHERE max_concurrent_calls IS NULL",
    # Rollout no-op (runs AFTER the capacity snapshot above): every pre-PR tenant sits
    # at the never-admin-settable default (3), which this release turns into an enforced
    # per-VA enqueue gate — lift those to the schema ceiling (20) so queueing behavior
    # only tightens once an admin deliberately lowers the knob. Fresh CI DBs have no rows.
    "UPDATE tenant SET max_agents_per_va = 20 WHERE max_agents_per_va = 3",
    "ALTER TABLE tenant ALTER COLUMN max_concurrent_calls SET NOT NULL",
    "ALTER TABLE tenant ALTER COLUMN max_concurrent_calls SET DEFAULT 25",
    # Serves the per-VA in-flight count (enqueue gate) and the dispatcher's active
    # count; stays small because terminal forms fall out of the predicate.
    """
    CREATE INDEX IF NOT EXISTS ix_patient_form_in_flight
    ON patient_form (tenant_id, enqueued_by_id)
    WHERE status IN ('in_queue', 'in_call', 'ai_processing')
    """,
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_patient_form_in_flight")
    op.execute("ALTER TABLE tenant DROP COLUMN IF EXISTS max_concurrent_calls")
