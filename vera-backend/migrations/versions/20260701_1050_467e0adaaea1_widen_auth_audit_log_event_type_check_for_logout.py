"""widen auth_audit_log.event_type CHECK for logout

Adds the `logout` auth event. `auth_audit_log.event_type` is constrained by a CHECK
built from the `AuthEvent` StrEnum (`ck_auth_audit_log_event_type_valid`; see 0006/0017).
Drop-and-recreate the named constraint from the CURRENT enum: `DROP ... IF EXISTS` is a
no-op on a fresh DB (where 0001 already built it with the new value) and an in-place
widen on an existing one. The value list is derived from the enum so it can't drift.
"""

from collections.abc import Sequence

from alembic import op

from vera_core.models.enums import AuthEvent, values_of

revision: str = "467e0adaaea1"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_auth_audit_log_event_type_valid"

# The value set before this migration (current enum minus `logout`), for downgrade.
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
    "persona_tweak_updated",
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
