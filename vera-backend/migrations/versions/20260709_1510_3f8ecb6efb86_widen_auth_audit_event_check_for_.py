"""widen auth audit event check for retention policy

Revision ID: 3f8ecb6efb86
Revises: 6fad3a3e6c29
Create Date: 2026-07-09 15:10:58.484948

Adds `retention_policy_updated`. Same pattern as fb43bdd169b2:
drop-and-recreate the named CHECK from the CURRENT enum — a no-op on a fresh
DB (0001 already built it with the new values) and an in-place widen on an
existing one.
"""

from collections.abc import Sequence

from alembic import op

from vera_core.models.enums import AuthEvent, values_of

revision: str = "3f8ecb6efb86"
down_revision: str | None = "6fad3a3e6c29"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_auth_audit_log_event_type_valid"
_NEW_VALUES = ("retention_policy_updated",)


def _check(values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"CHECK (event_type IN ({quoted}))"


def _recreate(values: Sequence[str]) -> None:
    op.execute(f"ALTER TABLE auth_audit_log DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(f"ALTER TABLE auth_audit_log ADD CONSTRAINT {_CONSTRAINT} {_check(values)}")


def upgrade() -> None:
    _recreate(values_of(AuthEvent))


def downgrade() -> None:
    # Derive the pre-migration set from the enum minus the value(s) this migration
    # adds, so it can never drift from a hand-maintained snapshot (matches fb43bdd169b2).
    _recreate(tuple(v for v in values_of(AuthEvent) if v not in _NEW_VALUES))
