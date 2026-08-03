"""widen auth audit event check for password reset

Revision ID: 38fbe1f6de7c
Revises: 2435e03793ff
Create Date: 2026-07-29 15:24:36.625943

Adds `password_reset_requested` / `password_reset_completed` (VR2-104). Same
pattern as 94f5bc060fac: drop-and-recreate the named CHECK from the CURRENT
enum — a no-op on a fresh DB (0001 already built it with the new values) and
an in-place widen on an existing one.
"""

from collections.abc import Sequence

from alembic import op

from vera_core.models.enums import AuthEvent, values_of

revision: str = "38fbe1f6de7c"
down_revision: str | None = "2435e03793ff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_auth_audit_log_event_type_valid"
_NEW_VALUES = ("password_reset_requested", "password_reset_completed")


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
    # adds, so it can never drift from a hand-maintained snapshot (matches 94f5bc060fac).
    _recreate(tuple(v for v in values_of(AuthEvent) if v not in _NEW_VALUES))
