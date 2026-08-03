"""widen auth audit event check for concurrency config updated

Revision ID: 94f5bc060fac
Revises: 16998691bc82
Create Date: 2026-07-28 22:23:18.474979

Adds `concurrency_config_updated`. Same pattern as 3f8ecb6efb86:
drop-and-recreate the named CHECK from the CURRENT enum — a no-op on a fresh
DB (0001 already built it with the new values) and an in-place widen on an
existing one.
"""

from collections.abc import Sequence

from alembic import op

from vera_core.models.enums import AuthEvent, values_of

revision: str = "94f5bc060fac"
down_revision: str | None = "16998691bc82"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_auth_audit_log_event_type_valid"
_NEW_VALUES = ("concurrency_config_updated",)


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
    # adds, so it can never drift from a hand-maintained snapshot (matches 3f8ecb6efb86).
    _recreate(tuple(v for v in values_of(AuthEvent) if v not in _NEW_VALUES))
