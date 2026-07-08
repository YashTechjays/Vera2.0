"""schema and prompt documents stored as JSON to preserve DSL key order

JSONB normalizes object key order (length, then bytewise), but a DSL v2
document's key order IS its field/section order (spec §4.1 "document order") —
the UI renderer and the prompt compiler both walk it in order. Plain JSON stores
the document text verbatim. Rows already normalized by JSONB keep their sorted
order (the cast cannot recover it); re-seed to republish order-preserving
versions (`just seed-schemas` / `just seed-prompts`).

Revision ID: 211edd19b786
Revises: 0022
Create Date: 2026-07-05 10:32:27.707899

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "211edd19b786"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "prompt_version",
        "composite_json",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        type_=postgresql.JSON(astext_type=sa.Text()),
        existing_nullable=False,
        postgresql_using="composite_json::json",
    )
    op.alter_column(
        "schema_version",
        "schema_json",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        type_=postgresql.JSON(astext_type=sa.Text()),
        existing_nullable=False,
        postgresql_using="schema_json::json",
    )


def downgrade() -> None:
    op.alter_column(
        "schema_version",
        "schema_json",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        type_=postgresql.JSONB(astext_type=sa.Text()),
        existing_nullable=False,
        postgresql_using="schema_json::jsonb",
    )
    op.alter_column(
        "prompt_version",
        "composite_json",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        type_=postgresql.JSONB(astext_type=sa.Text()),
        existing_nullable=False,
        postgresql_using="composite_json::jsonb",
    )
