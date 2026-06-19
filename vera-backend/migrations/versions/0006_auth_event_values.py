"""refresh auth_audit_log.event_type CHECK with the new AuthEvent values

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-17

`auth_audit_log.event_type` is constrained by a CHECK built from the `AuthEvent`
StrEnum (`check_in`). Migration 0001 materializes that DDL from `Base.metadata`, so
the constraint was frozen with the enum's values at the time it ran. This release
adds user-admin / API-key / custom-role events (`user_invited`, `invite_accepted`,
`user_deactivated`, `role_created`, `api_key_created`, `api_key_revoked`), so an
already-provisioned database rejects them until the CHECK is widened.

Drop-and-recreate the named constraint from the CURRENT enum: `DROP ... IF EXISTS`
makes it a no-op on a fresh DB (where 0001 already built it with the new values),
and an in-place widen on an existing one. The value list is derived from the enum
so it can never drift.
"""

from collections.abc import Sequence

from alembic import op

from vera_core.models.enums import AuthEvent, values_of

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_auth_audit_log_event_type_valid"

# The set before this migration (0001 baseline), used to restore on downgrade.
_OLD_VALUES = (
    "login_success",
    "login_failure",
    "mfa_challenge",
    "role_grant",
    "role_revoke",
    "tenant_elevation_granted",
    "tenant_elevation_ended",
    "provider_enabled",
    "provider_disabled",
    "authz_allow",
    "authz_deny",
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
    _recreate(_OLD_VALUES)
