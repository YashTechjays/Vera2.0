"""ivr_playbook and insurance_provider status checks + unique provider name

Revision ID: 0e78b863d8a3
Revises: d1f1fff0692c
Create Date: 2026-07-03 17:16:56.407900

Locks down the two GLOBAL catalog tables the IVR-playbook feature relies on:

- A case-insensitive UNIQUE provider name (`uq_insurance_provider_name` on `lower(name)`)
  so the shared catalog can't hold duplicate providers (incl. case variants).
- CHECK constraints pinning `status` to the ProviderStatus / PlaybookStatus catalogs, so a
  mis-cased or garbage status can't silently drop a row from every `status = 'active'` lookup.

Both live in the models too, so migration 0001 materializes them for a FRESH DB — this
migration only fixes EXISTING DBs, and mirrors d1f1fff0692c's defensive posture: the tables
predate the strict write path (seed scripts / ops SQL), so it first NORMALIZES the data that
would otherwise block the constraints, then creates them idempotently (safe on a fresh DB where
the objects already exist). A garbage status was already non-functional (nothing matched
'active'), so demoting it to 'inactive' changes no live behaviour.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0e78b863d8a3"
down_revision: str | None = "d1f1fff0692c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROVIDER_CK = "ck_insurance_provider_status_valid"
_PLAYBOOK_CK = "ck_ivr_playbook_status_valid"
_NAME_INDEX = "uq_insurance_provider_name"


def _add_status_check(table: str, constraint: str) -> None:
    # ADD CONSTRAINT has no IF NOT EXISTS; a DO block keeps this a no-op on a fresh DB that
    # already carries the constraint from migration 0001's model materialization.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = '{constraint}'
            ) THEN
                ALTER TABLE {table} ADD CONSTRAINT {constraint}
                    CHECK (status IN ('active', 'inactive'));
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    # 1. Normalize statuses so the CHECKs can be created: lowercase, then demote anything
    #    outside the catalog to 'inactive' (it was already dead — never matched 'active').
    for table in ("insurance_provider", "ivr_playbook"):
        op.execute(f"UPDATE {table} SET status = lower(status)")
        op.execute(
            f"UPDATE {table} SET status = 'inactive' WHERE status NOT IN ('active', 'inactive')"
        )

    # 2. De-dup provider names case-insensitively so the unique index can build: keep the
    #    earliest row's name, disambiguate later collisions with a short id suffix.
    op.execute(
        """
        UPDATE insurance_provider AS p
        SET name = p.name || ' [' || left(p.id::text, 8) || ']'
        WHERE EXISTS (
            SELECT 1 FROM insurance_provider AS q
            WHERE lower(q.name) = lower(p.name)
              AND (q.created_at, q.id) < (p.created_at, p.id)
        )
        """
    )

    # 3. Create the constraints idempotently (a fresh DB already has them from the model DDL).
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_NAME_INDEX} ON insurance_provider (lower(name))"
    )
    _add_status_check("insurance_provider", _PROVIDER_CK)
    _add_status_check("ivr_playbook", _PLAYBOOK_CK)


def downgrade() -> None:
    op.execute(f"ALTER TABLE ivr_playbook DROP CONSTRAINT IF EXISTS {_PLAYBOOK_CK}")
    op.execute(f"ALTER TABLE insurance_provider DROP CONSTRAINT IF EXISTS {_PROVIDER_CK}")
    op.execute(f"DROP INDEX IF EXISTS {_NAME_INDEX}")
