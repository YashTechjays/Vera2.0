"""widen auth audit event check for tenant_observer_updated

Revision ID: 2435e03793ff
Revises: 749ffe826565
Create Date: 2026-07-23 15:22:00.000000

Adds `tenant_observer_updated` (super-admin flipped a tenant's AI form-filling switch).
Same pattern as fdce499c1b8d: drop-and-recreate the named CHECK from the CURRENT enum —
a no-op on a fresh DB and an in-place widen on an existing one.
"""

from collections.abc import Sequence

from alembic import op

from vera_core.models.enums import AuthEvent, values_of

revision: str = "2435e03793ff"
down_revision: str | None = "749ffe826565"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_auth_audit_log_event_type_valid"
_NEW_VALUES = ("tenant_observer_updated",)


def _check(values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"CHECK (event_type IN ({quoted}))"


def _recreate(values: Sequence[str]) -> None:
    op.execute(f"ALTER TABLE auth_audit_log DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(f"ALTER TABLE auth_audit_log ADD CONSTRAINT {_CONSTRAINT} {_check(values)}")


def upgrade() -> None:
    _recreate(values_of(AuthEvent))


def downgrade() -> None:
    _recreate(tuple(v for v in values_of(AuthEvent) if v not in _NEW_VALUES))
