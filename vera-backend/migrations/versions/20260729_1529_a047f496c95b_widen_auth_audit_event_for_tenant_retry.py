"""widen auth audit event check for tenant_retry_config_updated

Revision ID: a047f496c95b
Revises: 9de48c83deeb
Create Date: 2026-07-29 15:29:20.221935

Adds `tenant_retry_config_updated` (platform operator changed a tenant's auto-retry
flag/threshold). Same pattern as 2435e03793ff: drop-and-recreate the named CHECK from
the CURRENT enum — a no-op on a fresh DB and an in-place widen on an existing one.
"""

from collections.abc import Sequence

from alembic import op

from vera_core.models.enums import AuthEvent, values_of

revision: str = "a047f496c95b"
down_revision: str | None = "9de48c83deeb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_auth_audit_log_event_type_valid"
_NEW_VALUES = ("tenant_retry_config_updated",)


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
