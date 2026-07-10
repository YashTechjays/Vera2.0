"""export_artifact table

Revision ID: 13b8dc4aaa83
Revises: 578952c20dec
Create Date: 2026-07-10 09:00:00.000000

Replaces the draft export_artifact schema (gcs_uri NOT NULL, disclosed_at, no
sha256/exported_by) with the Phase-4 ledger design: sha256 identifies the exact
bytes streamed to the caller; exported_by links to the acting user; gcs_uri is
reserved for future stored-artifact variants and stays NULL today.

Fresh DBs get the new table from 0001's create_all; this migration covers
already-provisioned DBs and is idempotent both ways.
"""

from collections.abc import Sequence

from alembic import op

import vera_core.models  # noqa: F401 — registers export_artifact on Base.metadata
from vera_core.db import Base
from vera_core.db.rls import rls_policy_ddl

revision: str = "13b8dc4aaa83"
down_revision: str | None = "578952c20dec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # Fresh DBs get the table (and generic RLS) from 0001's create_all; this
    # covers already-provisioned DBs. checkfirst + guarded policies = idempotent.
    Base.metadata.tables["export_artifact"].create(bind, checkfirst=True)

    # For already-provisioned DBs: migrate the old schema to the new one.
    # ADD COLUMN IF NOT EXISTS is idempotent; fresh DBs skip these (columns exist).
    op.execute("ALTER TABLE export_artifact ADD COLUMN IF NOT EXISTS sha256 VARCHAR(64)")
    op.execute(
        "ALTER TABLE export_artifact "
        "ADD COLUMN IF NOT EXISTS exported_by UUID REFERENCES app_user(id) ON DELETE SET NULL"
    )
    # Make gcs_uri nullable and shrink to 512 on provisioned DBs (single rewrite).
    # On fresh DBs the column is already nullable VARCHAR(512) from create_all — noop.
    op.execute(
        "ALTER TABLE export_artifact "
        "ALTER COLUMN gcs_uri DROP NOT NULL, "
        "ALTER COLUMN gcs_uri TYPE VARCHAR(512)"
    )
    # Drop the old disclosed_at column if it exists (provisioned DBs only).
    op.execute("ALTER TABLE export_artifact DROP COLUMN IF EXISTS disclosed_at")
    # Drop old check constraint (provisioned DBs have 'xlsx','pdf'; new allows only 'xlsx').
    # DROP CONSTRAINT IF EXISTS is natively idempotent — no DO block needed.
    op.execute(
        "ALTER TABLE export_artifact DROP CONSTRAINT IF EXISTS ck_export_artifact_format_valid"
    )
    # Add the new format check — guarded against duplicate_object.
    op.execute(
        "DO $$ BEGIN "
        "ALTER TABLE export_artifact ADD CONSTRAINT ck_export_artifact_format_valid "
        "CHECK (format IN ('xlsx')); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    )
    # RLS: already enabled on provisioned DBs; natively idempotent ALTER + guarded CREATE.
    for stmt in rls_policy_ddl("export_artifact"):
        if stmt.lstrip().upper().startswith("CREATE POLICY"):
            op.execute(f"DO $$ BEGIN {stmt}; EXCEPTION WHEN duplicate_object THEN NULL; END $$")
        else:  # ALTER TABLE ENABLE/FORCE RLS — natively idempotent
            op.execute(stmt)


def downgrade() -> None:
    # WARNING: on already-provisioned DBs this destroys the pre-existing disclosure-ledger
    # table and all its rows — there is no undo; the data is permanently gone.
    op.execute("DROP TABLE IF EXISTS export_artifact")
