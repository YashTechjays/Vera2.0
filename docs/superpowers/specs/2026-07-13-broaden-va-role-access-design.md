# Broaden `virtual_assistant` role to Live Monitoring + Data Management — design

**Date:** 2026-07-13
**Branch:** `feat/broaden-va-role-access`
**Status:** Design — pending approval

## Problem / goal

The `VIRTUAL_ASSISTANT` global system role currently holds only
`voice_lab:sandbox` (see `2026-07-06-virtual-assistant-role-design.md`, which
explicitly deferred expanding VA's permissions to a later task). We need VA
users to also access and manage the **Live Monitoring** and **Data
Management** pages.

## Scope (v1)

No new permission codes are needed — both pages are already gated by existing
`DEFAULT_PERMISSIONS` entries that VA simply doesn't hold yet:

| Page | Permission | Feature it unlocks |
|---|---|---|
| Live Monitoring | `calls:read` | List active calls, listen-in/join, live transcript (SSE), end call |
| Live Monitoring | `calls:publish` | "Visible to All" toggle — publish/revoke a call to other VAs |
| Data Management | `forms:read` | Records list, record detail, form-schema version |
| Data Management | `forms:write` | Resolve field disputes, edit record status |

Grant all 4 to `VIRTUAL_ASSISTANT` ("access and manage," not read-only).

Frontend nav (`nav.ts`) and page guards are already permission-driven off the
effective-permissions array — both sidebar items and their features appear
automatically once VA holds these codes. **No frontend changes.**

**Out of scope:**
- Any new permission code or new page/feature.
- The deferred `PATCH /roles/{id}/permissions` runtime-editing endpoint —
  still not built; this expansion goes through `rbac_defaults.py` + a
  migration, per the established pattern.
- Backfilling any *other* role — VA is the only role gaining permissions here.

## Backend

### 1. Permission catalog (`vera_core/models/rbac_defaults.py`)

Add the 4 codes to `SYSTEM_ROLES["VIRTUAL_ASSISTANT"]` (line ~73), additive to
the existing `voice_lab:sandbox`:

```python
"VIRTUAL_ASSISTANT": frozenset({
    "voice_lab:sandbox", "calls:read", "calls:publish", "forms:read", "forms:write",
}),
```

Keeps `scripts/seed.py`'s idempotent upsert correct for fresh/dev environments.

### 2. Data migration (new Alembic revision)

Follows `20260710_1745_f503e82734cc_seed_form_schemas_read_permission.py`'s
shape (simple grant, no new permission row, no backfill needed since we're
granting directly to VA rather than splitting/renaming a permission):

For each of the 4 codes:
```sql
INSERT INTO role_permission (id, tenant_id, role_id, permission_id)
SELECT gen_random_uuid(), r.tenant_id, r.id, p.id
FROM role r, permission p
WHERE r.tenant_id IS NULL AND r.name = 'VIRTUAL_ASSISTANT' AND p.code = '<code>'
ON CONFLICT (role_id, permission_id) DO NOTHING;
```

`downgrade()` raises `RuntimeError` per repo convention (a backfilled grant is
indistinguishable from one added since by real usage — blind deletion isn't
safe). Generated via `just makemigration "..."`, not hand-numbered.

### 3. Endpoint gating

No change — `calls.py` and `patient_forms.py` already gate on these 4 codes
via `require(...)`. Granting VA the permission is sufficient.

## Frontend

No change. `nav.ts` already gates Live Monitoring on `calls:read` and Data
Management on `forms:read`; page-level actions (`InterveneModal`,
`IbvFormModal`, etc.) already gate on `calls:publish`/`forms:write` via
`usePermission(...)`.

## Testing

- **Backend**: extend `tests/unit/test_rbac_defaults.py` to assert
  `VIRTUAL_ASSISTANT` now includes all 4 codes. Add/extend a `virtual_assistant`
  persona test in `tests/integration/control_plane/conftest.py`'s `RBACWorld`
  asserting VA can now hit `GET /calls`, `POST /calls/{id}/publish`,
  `GET /patient-forms`, `PUT /patient-forms/{id}/status` (previously 403).
  Add a migration test asserting the grant lands on the `VIRTUAL_ASSISTANT`
  role row after running the new revision.

## Verification

- Backend: `just check` (ruff + mypy + pytest) clean, including new RBAC tests.
- Per repo CLAUDE.md: run **"simplify code"** on the change before committing.
- Manual: log in as a `virtual_assistant` user, confirm Live Monitoring and
  Data Management now appear in the sidebar and their actions (listen-in,
  publish, edit/resolve records) work without 403s.
