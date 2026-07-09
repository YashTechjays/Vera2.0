"""platform definer EXECUTE lockdown

Lock down EXECUTE on the two f066c667ddc1 SECURITY DEFINER functions: Postgres grants
EXECUTE to PUBLIC by default, so any DB principal could invoke the privileged write path
(guarded only by the self-set `app.platform` GUC — not a barrier against a role that can
run SQL). Revoke PUBLIC and grant only the deployed app role (`$VERA_APP_DB_ROLE`, unset →
`CURRENT_USER`). Advances devops-todo #12. (PR #68 review, comment 1.)
"""

import os
from collections.abc import Sequence

from alembic import op

revision: str = "3f7a9c2e8b41"
down_revision: str | None = "f066c667ddc1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Deployment-specific app role that may EXECUTE the definer functions (prod: the Cloud SQL
# app role; tests: vera_rls_test). Templated at deploy time; unset (local/CI, where
# migrations run as the app user) → CURRENT_USER.
_APP_ROLE = os.environ.get("VERA_APP_DB_ROLE") or "CURRENT_USER"

_SIGNATURES = (
    "platform_store_mfa_seed(uuid, bytea, bytea, text)",
    "platform_activate_mfa(uuid, bytea)",
)


def upgrade() -> None:
    for sig in _SIGNATURES:
        op.execute(f"REVOKE EXECUTE ON FUNCTION {sig} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO {_APP_ROLE}")


def downgrade() -> None:
    for sig in _SIGNATURES:
        op.execute(f"REVOKE EXECUTE ON FUNCTION {sig} FROM {_APP_ROLE}")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO PUBLIC")
