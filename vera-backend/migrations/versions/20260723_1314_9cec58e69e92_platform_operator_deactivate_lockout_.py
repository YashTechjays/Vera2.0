"""platform operator deactivate lockout atomic guard

Revision ID: 9cec58e69e92
Revises: d226261a20ca
Create Date: 2026-07-23 13:14:30.062050

PR #126 review flagged a TOCTOU race in `api/v1/platform_users.py::deactivate_operator`:
the active-operator count was read and acted on as two separate statements (no lock, no
isolation override — this DB runs at default Postgres READ COMMITTED), so two concurrent
`deactivate` calls against two different active operators could each read `active_count
== 2`, each pass the "don't deactivate the last one" guard, and both commit — leaving
zero active platform operators, which is unrecoverable (no bootstrap path once any
platform operator has ever existed).

This migration folds the count-check-and-write into `platform_set_operator_status`
(`d226261a20ca`) itself, replacing its body only — same name, same argument types, same
return type (`boolean`), so this is a plain `CREATE OR REPLACE FUNCTION`, not a
drop-and-recreate.

**Verified empirically** (scratch function, same owner/grant shape as this one — CREATE,
`REVOKE EXECUTE ... FROM PUBLIC`, confirm the default-privilege grant to the app role via
`aclexplode(proacl)`, then `CREATE OR REPLACE` with a changed body and re-check): an
unchanged name/args/return-type `CREATE OR REPLACE FUNCTION` preserves the target's
existing owner AND its materialized ACL byte-for-byte — no grant gained, none lost. So the
`ALTER FUNCTION ... OWNER TO` / `REVOKE`+`GRANT EXECUTE` lockdown from `d226261a20ca` does
NOT need to be re-asserted here. (This is a narrower case than the CLAUDE.md note on a
definer fn's **param type** changing, which does lose ownership under `CREATE OR REPLACE`
— this migration changes neither params nor return type.)

**First draft of this fix was wrong — caught by manual two-session concurrency testing
before it reached a migration.** The first attempt used `PERFORM ... FOR UPDATE` to lock
the active set, then a *separate* `SELECT count(*) ...` statement to read it back. That is
unsound: PL/pgSQL statements inside a single function call executed from one client-issued
`SELECT platform_set_operator_status(...)` all share the ONE snapshot Postgres fixes at the
start of that top-level statement — READ COMMITTED's "new snapshot per statement" rule
applies to the client's outer statement, not to each internal SPI query inside it. So a
transaction that blocked on the `FOR UPDATE` lock, then woke up after the blocking
transaction committed, still read the *pre-block* snapshot in its follow-up `COUNT` query —
both transactions in a live two-session test observed `active_count == 2` and BOTH
deactivations went through, reproducing the exact zero-active-operators bug this migration
exists to close. The fix: lock AND read the active set in the SAME query, using the
locking clause's row-level recheck (Postgres's EvalPlanQual, which re-evaluates a
blocked-then-unblocked row's WHERE-qualification against its now-committed data) instead of
a follow-up plain SELECT. Re-tested with the same two-session scenario after the fix:
exactly one deactivation succeeds, the other is rejected, no deadlock.

New body, only for `p_status = 'deactivated'`:
  1. `SELECT array_agg(id) FROM (SELECT id FROM app_user WHERE account_type='platform' AND
     status='active' ORDER BY id FOR UPDATE) locked_active` — locks every currently-active
     platform-operator row, in a fixed (id) order, and reads back exactly the set that
     survived the lock-wait recheck, in the same statement. A concurrent deactivate of a
     DIFFERENT active operator blocks here until the first transaction commits or rolls
     back; once unblocked, a row the first transaction actually deactivated is excluded
     from `v_active_ids` (it no longer matches `status = 'active'`), so this reads the
     genuinely post-commit active set — not a stale pre-wait one. The `ORDER BY id` makes
     every caller acquire these row locks in the same order, ruling out a lock-ordering
     deadlock between two concurrent deactivations of two different active operators.
  2. Block (return SQL NULL — distinguishable from `false`/"not found") only when the
     target is itself a member of that active set AND it is the only member. Deactivating
     an already-deactivated or still-invited operator is never in `v_active_ids`, so it is
     deliberately unaffected by this guard (matches the prior Python-side
     `user.status == "active"` gate this migration replaces).
  3. Otherwise, the same `UPDATE ... WHERE id = p_app_user_id AND tenant_id IS NULL AND
     account_type = 'platform'` as before, returning `row_count > 0`.

The three-way return contract is now `true` (flipped) / `false` (no such platform
operator) / `NULL` (blocked by the lockout guard) — still a single `boolean`, since SQL
booleans are three-valued. The Python wrapper (`auth/platform_provisioning.py::
set_operator_status`) and the endpoint (`api/v1/platform_users.py::deactivate_operator`)
are updated in the same commit to read this signal instead of doing their own
count-then-check.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "9cec58e69e92"
down_revision: str | None = "d226261a20ca"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEARCH_PATH = "SET search_path = pg_catalog, public"
_GUARD = "current_setting('app.platform', true) = 'on'"

_SET_OPERATOR_STATUS = f"""
CREATE OR REPLACE FUNCTION platform_set_operator_status(
    p_app_user_id uuid,
    p_status text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
DECLARE
    v_count bigint;
    v_active_ids uuid[];
BEGIN
    IF ({_GUARD}) IS NOT TRUE THEN
        RAISE EXCEPTION 'platform_set_operator_status: not a platform session';
    END IF;
    IF p_status NOT IN ('active', 'deactivated') THEN
        RAISE EXCEPTION 'platform_set_operator_status: invalid status %', p_status;
    END IF;

    IF p_status = 'deactivated' THEN
        -- Lock the active set AND read it back in one statement (see migration
        -- docstring for why a separate follow-up COUNT is unsound here): the FOR
        -- UPDATE lock-wait recheck means a row a concurrent caller just deactivated
        -- is excluded post-wait, so v_active_ids reflects genuinely post-commit state.
        SELECT array_agg(id) INTO v_active_ids
          FROM (
              SELECT id
                FROM app_user
               WHERE account_type = 'platform' AND status = 'active'
               ORDER BY id
                 FOR UPDATE
          ) locked_active;

        -- Block only when the target is itself the last member of that active set.
        -- Deactivating an already-deactivated or still-invited operator never appears
        -- in v_active_ids, so it stays unaffected by this guard.
        IF p_app_user_id = ANY(v_active_ids) AND array_length(v_active_ids, 1) = 1 THEN
            RETURN NULL;  -- distinguishable "blocked by lockout" signal (see wrapper)
        END IF;
    END IF;

    UPDATE app_user
       SET status = p_status
     WHERE id = p_app_user_id
       AND tenant_id IS NULL
       AND account_type = 'platform';
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count > 0;
END;
$$
"""

_SET_OPERATOR_STATUS_PREVIOUS = f"""
CREATE OR REPLACE FUNCTION platform_set_operator_status(
    p_app_user_id uuid,
    p_status text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
DECLARE
    v_count bigint;
BEGIN
    IF ({_GUARD}) IS NOT TRUE THEN
        RAISE EXCEPTION 'platform_set_operator_status: not a platform session';
    END IF;
    IF p_status NOT IN ('active', 'deactivated') THEN
        RAISE EXCEPTION 'platform_set_operator_status: invalid status %', p_status;
    END IF;

    UPDATE app_user
       SET status = p_status
     WHERE id = p_app_user_id
       AND tenant_id IS NULL
       AND account_type = 'platform';
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count > 0;
END;
$$
"""


def upgrade() -> None:
    # Same name/args/return type as d226261a20ca's version -> plain replace, no
    # ALTER OWNER / REVOKE+GRANT re-assertion needed (see docstring).
    op.execute(_SET_OPERATOR_STATUS)


def downgrade() -> None:
    op.execute(_SET_OPERATOR_STATUS_PREVIOUS)
