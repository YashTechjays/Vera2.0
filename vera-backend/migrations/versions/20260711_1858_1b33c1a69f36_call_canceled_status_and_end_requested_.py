"""call canceled status and end_requested_by

CANCELED is the user-requested terminal call status (End Call in Live
Monitoring) — never auto-retried. `end_requested_by_id` is the durable end
intent the endpoint stamps before tearing the room down, so the sweeper can
close a call whose worker event never arrived as CANCELED instead of FAILED.

Idempotent: migration `0001`'s `create_all` already gives a *fresh* DB the
column, FK, widened CHECK, and widened partial index (off the live model) —
see the migrations bullet in CLAUDE.md — so every op here must tolerate the
object already existing.

Revision ID: 1b33c1a69f36
Revises: 9d09f73f7357
Create Date: 2026-07-11 18:58:56.917468

"""

from collections.abc import Sequence

from alembic import op

revision: str = "1b33c1a69f36"
down_revision: str | None = "9d09f73f7357"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TERMINAL_OLD = "'completed', 'failed', 'no_answer', 'busy'"
_TERMINAL_NEW = _TERMINAL_OLD + ", 'canceled'"
_LIVE = "'initiated', 'ringing', 'ivr', 'active', 'waiting', 'critical'"
_STATUSES_OLD = f"{_LIVE}, {_TERMINAL_OLD}"
_STATUSES_NEW = f"{_LIVE}, {_TERMINAL_NEW}"


def upgrade() -> None:
    op.execute("ALTER TABLE call ADD COLUMN IF NOT EXISTS end_requested_by_id UUID")
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE call ADD CONSTRAINT fk_call_end_requested_by_id_app_user
                FOREIGN KEY (end_requested_by_id) REFERENCES app_user (id)
                ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        """
    )
    op.execute("ALTER TABLE call DROP CONSTRAINT IF EXISTS ck_call_current_status_valid")
    op.execute(
        "ALTER TABLE call ADD CONSTRAINT ck_call_current_status_valid "
        f"CHECK (current_status IN ({_STATUSES_NEW}))"
    )
    # The one-live-call-per-form partial index bakes the terminal list into its
    # predicate: without 'canceled' there, a canceled call stays "live" and the
    # form could never be dialed again (unique violation on the next dispatch).
    op.execute("DROP INDEX IF EXISTS uq_call_active_form")
    op.execute(
        "CREATE UNIQUE INDEX uq_call_active_form ON call (form_id) "
        f"WHERE current_status NOT IN ({_TERMINAL_NEW})"
    )


def downgrade() -> None:
    op.execute("UPDATE call SET current_status = 'failed' WHERE current_status = 'canceled'")
    op.execute("DROP INDEX IF EXISTS uq_call_active_form")
    op.execute(
        "CREATE UNIQUE INDEX uq_call_active_form ON call (form_id) "
        f"WHERE current_status NOT IN ({_TERMINAL_OLD})"
    )
    op.execute("ALTER TABLE call DROP CONSTRAINT IF EXISTS ck_call_current_status_valid")
    op.execute(
        "ALTER TABLE call ADD CONSTRAINT ck_call_current_status_valid "
        f"CHECK (current_status IN ({_STATUSES_OLD}))"
    )
    op.execute("ALTER TABLE call DROP CONSTRAINT IF EXISTS fk_call_end_requested_by_id_app_user")
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS end_requested_by_id")
