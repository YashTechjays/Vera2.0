"""backfill percent field answers into their canonical "<n>%" form

Every writer of a `percent`-typed leaf now canonicalizes on write
(`vera_core.forms.intake.normalize_percent_value`), but rows written before that
change carry whichever spelling their writer produced — a bare `"20"` from the
extractor beside the authored `"0%"` from the either/or auto-fill. Storage is
display here (the review UI and the xlsx export render the stored string
verbatim), so one CPT matrix column could show both.

Rewrites ALL rows for a percent leaf, not just `is_current`, and that is the
non-obvious decision. `field_answers.baseline_value` filters on `source` and
deliberately NOT on `is_current`, so a superseded intake/human row is still the
dispute baseline; an `is_current`-only backfill would leave that baseline `"20"`
against a canonical `"20%"` AI answer and MANUFACTURE a false dispute on every
historical form — and an unresolved dispute blocks
`exception_review -> completed`. `priors_by_path` (patient_forms.py) likewise
reads every row for an edited path to tell an OVERRIDE swap-back from a CORRECT.
Safe because `field_answer` is not a WORM table (only `audit_log` /
`auth_audit_log` are, see `0001_initial.py`), `is_current` is mutated in normal
operation anyway, and the rewrite is format-only. `dispute_action` and
`call_form_snapshot` are deliberately untouched — those record what a human or a
call actually saw.

Python-driven rather than one UPDATE because percent-ness is only knowable from a
recursive walk of the pinned `schema_version.schema_json`, and the canonical form
needs trailing-zero stripping and unit-word folding.

**Frozen copies, deliberately.** `percent_leaf_paths` / `canonical_percent` below
duplicate a subset of `vera_core.forms.intake` instead of importing it: a
migration must keep doing what it did the day it ran while the runtime rule
evolves. `canonical_percent` is a strict SUBSET — no schema-literal folding,
because `"N/A"` and `"0%"` already sit in the DB in their authored spelling.

**Idempotent**: a row whose canonical form equals its stored value is skipped, so
re-running changes nothing.
"""

import json
import re
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "5752b98fa277"
down_revision: str | None = "d8cee818167e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `field_answer` carries FORCE ROW LEVEL SECURITY with
# `USING (tenant_id = current_setting('app.tenant_id', true)::uuid)` (see db/rls.py,
# applied by 0001_initial.py). A migration never sets that GUC, so `current_setting`
# returns NULL, the predicate is NULL, and an unprivileged role sees ZERO rows and
# updates ZERO rows while the migration exits GREEN. It works because the migration
# role is privileged (deploy/README.md provisions the migration URL as `postgres` with
# BYPASSRLS; locally docker-compose's POSTGRES_USER is a superuser) — the same thing
# 0012_auth_audit_hash_chain.py relies on for its cross-tenant WORM backfill. Because
# the failure is SILENT, assert the privilege rather than trusting it.
PRIVILEGE_SQL = "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"

# Read-only: casting to jsonb here is safe, the result is never written back.
SELECT_SCHEMA_VERSIONS_SQL = """
    SELECT id, schema_json::text
    FROM schema_version
    WHERE (schema_json ->> 'dsl_version') LIKE '2.%'
"""

# Every answer for a percent leaf of one schema_version — is_current NOT filtered, see
# the module docstring. `->> 'value'` renders a JSON number and a JSON string alike, so a
# legacy numeric 20 normalizes to the string "20%" like everything else.
SELECT_ANSWERS_SQL = """
    SELECT fa.id, fa.value ->> 'value'
    FROM field_answer fa
    JOIN patient_form pf ON pf.id = fa.form_id
    WHERE pf.schema_version_id = :schema_version_id
      AND fa.field_path = ANY(:paths)
      AND fa.value ->> 'value' IS NOT NULL
      AND fa.id > :after
    ORDER BY fa.id
    LIMIT :limit
"""

# Keyset pagination on the UUIDv7 PK (monotonic, index-ordered) so an install with a large
# `field_answer` never materializes every row — and every update dict for them — in one go
# inside the single migration transaction.
_CHUNK = 5_000

# CAST(:new AS text), not `:new::text` — a bind param immediately followed by `::` breaks
# text()'s bind-parameter parsing (documented at length in e05205e0a173).
UPDATE_ANSWER_SQL = """
    UPDATE field_answer
    SET value = jsonb_build_object('value', CAST(:new AS text))
    WHERE id = :id
"""

_NUMBER_RE = re.compile(r"^(\d+)(?:\.(\d+))?$")
_SUFFIX_RE = re.compile(r"\s*(?:%|percent|pct)$", re.IGNORECASE)


def abort_if_rls_would_hide_rows(privileged: bool) -> None:
    """Refuse to run under a role RLS would silently hide every row from. Isolated from
    SQL execution so it is unit-testable against a bare bool."""
    if not privileged:
        raise RuntimeError(
            "backfill_percent_field_answers: current_user has neither SUPERUSER nor "
            "BYPASSRLS. field_answer has FORCE ROW LEVEL SECURITY and a migration never "
            "sets app.tenant_id, so this would update ZERO rows and exit successfully. "
            "Run migrations as the privileged role (see deploy/README.md)."
        )


def percent_leaf_paths(doc: dict[str, Any]) -> list[str]:
    """Root-anchored paths of every `type: "percent"` leaf in a compiled document.

    Frozen copy of the DSL's field walk. `fields` is a CONTAINER key and is NOT part of
    the path — `field_answer.field_path` is `sections.<section>.<field>[.<field>...]`.
    Recurses on the presence of `fields` rather than on `type == "group"`, so a future
    container type is still traversed."""
    paths: list[str] = []

    def walk(fields: Any, prefix: str) -> None:
        if not isinstance(fields, dict):
            return
        for key, node in fields.items():
            if not isinstance(node, dict):
                continue
            path = f"{prefix}.{key}"
            if "fields" in node:
                walk(node["fields"], path)
            elif node.get("type") == "percent":
                paths.append(path)

    for section_key, section in (doc.get("sections") or {}).items():
        if isinstance(section, dict):
            walk(section.get("fields"), f"sections.{section_key}")
    return paths


def canonical_percent(raw: str) -> str | None:
    """The canonical ``"<n>%"`` form of `raw`, or None when it is not a recognized numeric
    percent — blank, a schema literal like "N/A"/"0%", or prose like "20% after
    deductible". Those rows are left exactly as they are."""
    text = raw.strip()
    if not text:
        return None
    match = _NUMBER_RE.match(_SUFFIX_RE.sub("", text))
    if match is None:
        return None
    whole, frac = match.group(1).lstrip("0") or "0", (match.group(2) or "").rstrip("0")
    return f"{whole}.{frac}%" if frac else f"{whole}%"


def upgrade() -> None:
    conn = op.get_bind()
    abort_if_rls_would_hide_rows(bool(conn.exec_driver_sql(PRIVILEGE_SQL).scalar()))

    for version_id, schema_json_text in conn.exec_driver_sql(SELECT_SCHEMA_VERSIONS_SQL).all():
        paths = percent_leaf_paths(json.loads(schema_json_text))
        if not paths:
            continue
        # The non-partial `ix_field_answer_baseline` serves this; `fa_current_uq` cannot,
        # since it is partial on `is_current` and this deliberately spans every row.
        after = UUID(int=0)
        while rows := conn.execute(
            sa.text(SELECT_ANSWERS_SQL),
            {
                "schema_version_id": version_id,
                "paths": paths,
                "after": after,
                "limit": _CHUNK,
            },
        ).all():
            after = rows[-1][0]
            updates = [
                {"id": row_id, "new": canonical}
                for row_id, stored in rows
                if (canonical := canonical_percent(stored)) is not None and canonical != stored
            ]
            if updates:
                conn.execute(sa.text(UPDATE_ANSWER_SQL), updates)


def downgrade() -> None:
    """Data backfill — deliberately NOT reversed, matching the e05205e0a173 and
    c247bd741862 precedents.

    A mechanical inverse (strip one trailing `%`) would be WRONG, not merely lossy: it
    cannot tell an authored `"0%"` — always stored with the sign, never touched here —
    from a `"20%"` canonicalized out of `"20"`, so it would corrupt the former to "fix"
    the latter. The pre-change code accepted any shape, so leaving these rows canonical is
    safe when the code is rolled back."""
