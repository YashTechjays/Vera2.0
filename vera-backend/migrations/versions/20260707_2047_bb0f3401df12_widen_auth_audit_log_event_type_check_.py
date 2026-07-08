"""widen auth_audit_log event_type CHECK for prompt authoring

Revision ID: bb0f3401df12
Revises: 098fb25594c1
Create Date: 2026-07-07 20:47:40.914286

Adds `prompt_version_created` / `prompt_version_published`. Same pattern as
467e0adaaea1: drop-and-recreate the named CHECK from the CURRENT enum — a
no-op on a fresh DB (0001 already built it with the new values) and an
in-place widen on an existing one.
"""

from collections.abc import Sequence

from alembic import op

from vera_core.models.enums import AuthEvent, values_of

revision: str = "bb0f3401df12"
down_revision: str | None = "098fb25594c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_auth_audit_log_event_type_valid"
_NEW_VALUES = ("prompt_version_created", "prompt_version_published")


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
