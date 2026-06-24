"""widen auth_audit_log.event_type CHECK for persona_tweak_updated

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-23

`auth_audit_log.event_type` is constrained by a CHECK built from the `AuthEvent`
StrEnum (`ck_auth_audit_log_event_type_valid`; see 0006). This release adds the
tenant runtime-config event `persona_tweak_updated`, so an already-provisioned
database rejects it until the CHECK is widened.

Drop-and-recreate the named constraint from the CURRENT enum, exactly as 0006:
`DROP ... IF EXISTS` makes it a no-op on a fresh DB (where 0001 already built it
with the new value) and an in-place widen on an existing one. The value list is
derived from the enum so it can never drift.
"""

from collections.abc import Sequence

from alembic import op

from vera_core.models.enums import AuthEvent, values_of

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_auth_audit_log_event_type_valid"

# The set before this migration (0006 baseline + 0001 user/admin events), used to
# restore on downgrade — the current enum minus `persona_tweak_updated`.
_OLD_VALUES = (
    "login_success",
    "login_failure",
    "mfa_challenge",
    "user_invited",
    "invite_accepted",
    "user_deactivated",
    "role_created",
    "role_grant",
    "role_revoke",
    "api_key_created",
    "api_key_revoked",
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
