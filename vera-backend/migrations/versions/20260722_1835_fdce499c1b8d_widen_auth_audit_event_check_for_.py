"""widen auth audit event check for platform invite

Revision ID: fdce499c1b8d
Revises: 3876c58097c2
Create Date: 2026-07-22 18:35:35.344993

Adds invite_resent, platform_user_invited, platform_invite_accepted,
platform_user_activated, platform_user_deactivated, platform_invite_resent.
Same pattern as fb43bdd169b2/3f8ecb6efb86: drop-and-recreate the named CHECK from
the CURRENT enum — a no-op on a fresh DB and an in-place widen on an existing one.
"""

from collections.abc import Sequence

from alembic import op

from vera_core.models.enums import AuthEvent, values_of

revision: str = "fdce499c1b8d"
down_revision: str | None = "3876c58097c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_auth_audit_log_event_type_valid"
_NEW_VALUES = (
    "invite_resent",
    "platform_user_invited",
    "platform_invite_accepted",
    "platform_user_activated",
    "platform_user_deactivated",
    "platform_invite_resent",
)


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
