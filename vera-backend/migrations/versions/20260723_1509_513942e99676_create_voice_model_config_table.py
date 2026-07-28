"""create voice_model_config table

Global, append-only log of voice-cascade model overrides
(packages/vera_core/src/vera_core/models/voice_model_config.py) — no tenant_id, no RLS,
mirrors Prompt/FormSchema. Also lives in the models, so migration 0001 materializes it
for a FRESH DB — this migration only provisions it on an EXISTING DB (mirrors
0e78b863d8a3_ivr_playbook_and_insurance_provider_...'s idempotent posture: guarded
CREATE TABLE / ADD CONSTRAINT, safe to run even where the objects already exist).

Revision ID: 513942e99676
Revises: 738e38d86bdb
Create Date: 2026-07-23 15:09:19.086635

Rebased onto origin/dev's tip (738e38d86bdb, "grant calls:intervene to
VIRTUAL_ASSISTANT") ahead of merging this branch's PR — both branches had forked new
migrations off the same prior head (9cec58e69e92), which would otherwise leave two
heads after merge. Not yet shared, so relinked in place instead of `just merge-heads`.

"""

from collections.abc import Sequence

from alembic import op

# Keep generated revision/down_revision/branch_labels/depends_on above — do not edit

revision: str = "513942e99676"
down_revision: str | None = "738e38d86bdb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_model_config (
            id UUID PRIMARY KEY,
            stage VARCHAR(16) NOT NULL,
            provider VARCHAR(64),
            model VARCHAR(200),
            created_by_user_id UUID REFERENCES app_user(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_voice_model_config_stage_valid'
            ) THEN
                ALTER TABLE voice_model_config ADD CONSTRAINT ck_voice_model_config_stage_valid
                    CHECK (stage IN ('stt', 'llm', 'tts'));
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_voice_model_config_model_provider_pair'
            ) THEN
                ALTER TABLE voice_model_config
                    ADD CONSTRAINT ck_voice_model_config_model_provider_pair
                    CHECK (
                        (model IS NULL AND provider IS NULL)
                        OR (model IS NOT NULL AND provider IS NOT NULL)
                    );
            END IF;
        END $$;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_voice_model_config_stage_created_at "
        "ON voice_model_config (stage, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS voice_model_config")
