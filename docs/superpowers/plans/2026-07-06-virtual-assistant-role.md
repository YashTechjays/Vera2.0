# Virtual Assistant Role Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new tenant-level `VIRTUAL_ASSISTANT` role, scoped to a dedicated
`voice_lab:sandbox` permission, that a tenant admin can pick when inviting a user —
without weakening the existing `calls:read`-gated real call system or breaking any
existing role's Voice Lab access.

**Architecture:** Backend: a new permission (`voice_lab:sandbox`) and global system
role (`VIRTUAL_ASSISTANT`) added to the existing data-driven RBAC catalog
(`role`/`permission`/`role_permission`/`user_role` tables, ADR-0004); Voice Lab's four
endpoints re-gated onto it; a one-time data migration backfills the permission onto
every role that currently holds `calls:read` so nobody loses access. Frontend: a role
picker added to the invite dialog (backed by a new `GET /roles` client — none existed),
and the sidebar/routing made permission-driven end-to-end so a narrow role's nav
collapses and its default landing route redirects automatically, with zero
role-specific code.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async / Alembic / pytest
(`vera-backend`); React / TypeScript / React Router / Redux Toolkit / vitest
(`vera-frontend`).

## Global Constraints

- Alembic revision IDs are random hex, generated only via `just makemigration` —
  never hand-numbered (`vera-backend/CLAUDE.md`).
- Backend gate: `just check` (ruff + mypy --strict + pytest) must pass before a
  backend task is done. Integration tests need live Postgres/Redis: `just up` then
  `just migrate` once per environment, or they skip.
- Frontend gate: `tsc` + `eslint` + `vitest run` + `vite build` must pass before a
  frontend task is done. The frontend has no RTL/jsdom — tests are logic-only
  (matches existing `nav.test.ts` precedent); don't introduce component-rendering
  test infra to cover this feature.
- Repo-wide mandate (root `CLAUDE.md`): after the implementation is complete, run
  the **code-simplifier** agent ("simplify code") on the diff, then re-run the
  language gate above, before calling the work done.
- PHI/HIPAA guardrails (`vera-backend/CLAUDE.md`, `vera-frontend/CLAUDE.md`) apply
  throughout — this feature touches no PHI fields, but never log a raw email,
  invite token, or session token beyond what the existing code already does.
- Design reference: `docs/superpowers/specs/2026-07-06-virtual-assistant-role-design.md`.

---

### Task 1: RBAC catalog — `voice_lab:sandbox` permission, `VIRTUAL_ASSISTANT` role, test persona

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/models/rbac_defaults.py`
- Modify: `vera-backend/tests/integration/control_plane/conftest.py`
- Modify: `vera-backend/tests/integration/control_plane/test_admin.py`

**Interfaces:**
- Produces: permission code `"voice_lab:sandbox"` in `DEFAULT_PERMISSIONS`; role name
  `"VIRTUAL_ASSISTANT"` in `SYSTEM_ROLES` (holding only `voice_lab:sandbox`).
  `TENANT_ADMIN` gains it automatically (it's `frozenset(DEFAULT_PERMISSIONS)`);
  `SUPERVISOR` gains it via an explicit addition to its literal set.
- Produces: `RBACWorld.virtual_assistant_token: str` — a minted session token for a
  persona holding only the `VIRTUAL_ASSISTANT` role, consumed by Task 3's tests.

- [ ] **Step 1: Add the permission and update the role catalog**

Edit `vera-backend/packages/vera_core/src/vera_core/models/rbac_defaults.py`. Add
`"voice_lab:sandbox"` to `DEFAULT_PERMISSIONS` (right after `calls:write`, since it's
call-adjacent):

```python
DEFAULT_PERMISSIONS: Final[dict[str, str]] = {
    "calls:read": "View calls and their status/results",
    "calls:write": "Create and manage verification calls",
    "voice_lab:sandbox": "Use the Voice Lab sandbox to start and monitor test voice sessions",
    "forms:read": "View form templates and filled forms",
    "forms:write": "Create and edit form templates",
    "users:read": "View users in the tenant",
    "users:manage": "Invite, deactivate, and manage users",
    "roles:manage": "Manage roles and role assignments",
    "tenant:auth:configure": "Enable or disable the tenant's login providers",
    "tenant:config:manage": "View and edit tenant runtime config (persona, knobs)",
    "apikeys:manage": "Issue and revoke inbound API keys",
    "integrations:manage": "Configure outbound integration credentials (e.g. Twilio)",
    "audit:read": "Read the compliance audit log",
    "phi:detokenize": "Reveal raw PHI behind tokens (every use is audited)",
}
```

Then update `SYSTEM_ROLES` — add `voice_lab:sandbox` to `SUPERVISOR`'s explicit set
(so it keeps Voice Lab access) and add the new `VIRTUAL_ASSISTANT` role:

```python
SYSTEM_ROLES: Final[dict[str, frozenset[str]]] = {
    "SUPER_ADMIN": frozenset(ALL_PERMISSIONS),
    "TENANT_ADMIN": frozenset(DEFAULT_PERMISSIONS),
    "SUPERVISOR": frozenset(
        {
            "calls:read",
            "calls:write",
            "voice_lab:sandbox",
            "forms:read",
            "forms:write",
            "users:read",
            "audit:read",
            "phi:detokenize",
        }
    ),
    "VIRTUAL_ASSISTANT": frozenset({"voice_lab:sandbox"}),
}
```

- [ ] **Step 2: Extend the RBACWorld test fixture with a `virtual_assistant` persona**

Edit `vera-backend/tests/integration/control_plane/conftest.py`. In the `RBACWorld`
class, add a field:

```python
class RBACWorld:
    def __init__(self, tenant_id: UUID, other_tenant_id: UUID) -> None:
        self.tenant_id = tenant_id
        self.other_tenant_id = other_tenant_id
        # Filled once sessions are minted (see rbac_world).
        self.admin_token = ""
        self.norole_token = ""
        self.ghost_token = ""
        self.virtual_assistant_token = ""
```

In the `rbac_world` fixture, look up the new role, create the persona user, grant the
role, and mint its token — replace this block:

```python
        admin_role = (
            await session.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'TENANT_ADMIN'")
            )
        ).scalar_one()
        admin = AppUser(
            tenant_id=tenant_id,
            gcip_uid=None,
            email="admin@test.example",
            name="Admin",
            status="active",
        )
        norole = AppUser(
            tenant_id=tenant_id,
            gcip_uid=None,
            email="norole@test.example",
            name="No Role",
            status="active",
        )
        session.add_all([admin, norole])
        await session.flush()
        session.add(UserRole(tenant_id=tenant_id, app_user_id=admin.id, role_id=admin_role))
        admin_id, norole_id = admin.id, norole.id

    world.admin_token = await _mint(
        session_store, user_id=admin_id, tenant_id=tenant_id, email="admin@test.example"
    )
    world.norole_token = await _mint(
        session_store, user_id=norole_id, tenant_id=tenant_id, email="norole@test.example"
    )
    # A valid session whose user_id has no app_user row -> "unknown user" deny.
    world.ghost_token = await _mint(
        session_store, user_id=uuid7(), tenant_id=tenant_id, email="ghost@test.example"
    )
```

with:

```python
        admin_role = (
            await session.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'TENANT_ADMIN'")
            )
        ).scalar_one()
        virtual_assistant_role = (
            await session.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'VIRTUAL_ASSISTANT'")
            )
        ).scalar_one()
        admin = AppUser(
            tenant_id=tenant_id,
            gcip_uid=None,
            email="admin@test.example",
            name="Admin",
            status="active",
        )
        norole = AppUser(
            tenant_id=tenant_id,
            gcip_uid=None,
            email="norole@test.example",
            name="No Role",
            status="active",
        )
        virtual_assistant = AppUser(
            tenant_id=tenant_id,
            gcip_uid=None,
            email="virtual_assistant@test.example",
            name="Virtual Assistant",
            status="active",
        )
        session.add_all([admin, norole, virtual_assistant])
        await session.flush()
        session.add(UserRole(tenant_id=tenant_id, app_user_id=admin.id, role_id=admin_role))
        session.add(
            UserRole(
                tenant_id=tenant_id,
                app_user_id=virtual_assistant.id,
                role_id=virtual_assistant_role,
            )
        )
        admin_id, norole_id, virtual_assistant_id = admin.id, norole.id, virtual_assistant.id

    world.admin_token = await _mint(
        session_store, user_id=admin_id, tenant_id=tenant_id, email="admin@test.example"
    )
    world.norole_token = await _mint(
        session_store, user_id=norole_id, tenant_id=tenant_id, email="norole@test.example"
    )
    world.virtual_assistant_token = await _mint(
        session_store,
        user_id=virtual_assistant_id,
        tenant_id=tenant_id,
        email="virtual_assistant@test.example",
    )
    # A valid session whose user_id has no app_user row -> "unknown user" deny.
    world.ghost_token = await _mint(
        session_store, user_id=uuid7(), tenant_id=tenant_id, email="ghost@test.example"
    )
```

- [ ] **Step 3: Write the failing test — VIRTUAL_ASSISTANT is seeded and visible**

Add to `vera-backend/tests/integration/control_plane/test_admin.py`, in the `# ---
roles ---` section, right after `test_create_custom_role_appears_in_list`:

```python
async def test_virtual_assistant_role_seeded_and_visible(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    listing = await client.get("/api/v1/roles", headers=_auth(rbac_world.admin_token))
    names = {r["name"] for r in listing.json()["data"]}
    assert "VIRTUAL_ASSISTANT" in names
```

- [ ] **Step 4: Run it to confirm it fails**

Run (with `just up && just migrate` already done once in this environment):

```bash
cd vera-backend && uv run pytest tests/integration/control_plane/test_admin.py -k test_virtual_assistant_role_seeded_and_visible -v
```

Expected: FAIL — `AssertionError: assert 'VIRTUAL_ASSISTANT' in {...}` (the role
doesn't exist yet because Step 1 hasn't been applied, or Step 2's fixture change
hasn't landed). If you did Steps 1–3 in order, this actually passes already — in that
case skip to Step 5's build/typecheck confirmation instead of expecting a failure.

- [ ] **Step 5: Confirm the full change is green**

```bash
cd vera-backend && uv run pytest tests/integration/control_plane/test_admin.py -v
```

Expected: PASS — including the existing `test_create_custom_role_appears_in_list`,
`test_assign_and_revoke_role`, etc. (regression check that nothing else broke).

- [ ] **Step 6: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/models/rbac_defaults.py \
        vera-backend/tests/integration/control_plane/conftest.py \
        vera-backend/tests/integration/control_plane/test_admin.py
git commit -m "feat(rbac): add voice_lab:sandbox permission and VIRTUAL_ASSISTANT role"
```

---

### Task 2: Data migration — backfill `voice_lab:sandbox` onto existing `calls:read` roles

**Files:**
- Create: `vera-backend/migrations/versions/<generated>.py` (name and revision id are
  assigned by `alembic revision --autogenerate`, run via `just makemigration` below —
  never hand-numbered)

**Interfaces:**
- Consumes: nothing from Task 1 at runtime (this migration inserts the permission
  and role rows itself — see rationale below) — but conceptually mirrors Task 1's
  code exactly (same permission code `"voice_lab:sandbox"`, same role name
  `"VIRTUAL_ASSISTANT"`).
- Produces: on `alembic upgrade head`, guarantees (a) the `voice_lab:sandbox`
  permission row and `VIRTUAL_ASSISTANT` role row exist, and (b) every role — system
  or tenant-custom — that holds `calls:read` also holds `voice_lab:sandbox`.

Why a migration inserts data seed.py also seeds: `just seed` is an **opt-in** deploy
step (`run_seed` defaults `false` in `.github/workflows/_deploy-vm.yml`), so a
tenant admin must be able to see and assign `VIRTUAL_ASSISTANT` immediately after
`alembic upgrade head` runs — which is unconditional on every deploy — without
depending on someone remembering to also flip `run_seed`. Separately, `just seed`
only ever touches *global* system roles (`_seed_system_roles`), never a tenant's own
custom roles — so only a migration, which can query `role_permission` directly, can
backfill a tenant-custom role that was granted `calls:read` specifically for Voice
Lab access before this change shipped.

- [ ] **Step 1: Generate the migration skeleton**

```bash
cd vera-backend && just makemigration "seed voice lab sandbox permission and role"
```

This prints the new file path under `migrations/versions/` and its revision id.
Open it — `down_revision` should read `"5d7bc8c2f5ca"` (the current single head at
the time this plan was written). If it's a different value, or if `alembic upgrade
head` later reports multiple heads, run `just merge-heads` per the migrations
convention in `vera-backend/CLAUDE.md` — do not hand-edit `down_revision`.

- [ ] **Step 2: Replace the generated file's docstring and `upgrade()`/`downgrade()`**

Keep the autogenerated `revision`/`down_revision`/`branch_labels`/`depends_on`
values exactly as generated. Replace everything else with:

```python
"""seed voice_lab:sandbox permission, VIRTUAL_ASSISTANT role, and backfill existing
calls:read roles

Revision ID: <keep the generated value>
Revises: <keep the generated value>
Create Date: <keep the generated value>

Voice Lab moves off the reused `calls:read` permission onto a dedicated
`voice_lab:sandbox` permission (docs/superpowers/specs/2026-07-06-virtual-assistant-role-design.md).
This inserts the new permission + the global VIRTUAL_ASSISTANT role (holding only
voice_lab:sandbox), and backfills voice_lab:sandbox onto every role — system AND
tenant-custom — that currently holds calls:read, so nobody loses Voice Lab access
once api/v1/voice_lab.py switches its require() gate off calls:read.

Runs on the privileged migration connection (not RLS-bound), same as 0011's provider
seed — the strict WITH CHECK on a NULL-tenant role/role_permission row does not block
it. id columns are client-side defaulted (UUIDv7PKMixin) so every INSERT supplies one
via gen_random_uuid(); created_at/updated_at fall to their server_default now().
"""

from collections.abc import Sequence

from alembic import op

revision: str = "<keep the generated value>"
down_revision: str | None = "<keep the generated value>"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSION_CODE = "voice_lab:sandbox"
_ROLE_NAME = "VIRTUAL_ASSISTANT"


def upgrade() -> None:
    op.execute(
        "INSERT INTO permission (id, code, description) "
        f"VALUES (gen_random_uuid(), '{_PERMISSION_CODE}', "
        "'Use the Voice Lab sandbox to start and monitor test voice sessions') "
        "ON CONFLICT (code) DO NOTHING"
    )
    op.execute(
        "INSERT INTO role (id, tenant_id, name, description) "
        f"VALUES (gen_random_uuid(), NULL, '{_ROLE_NAME}', '') "
        "ON CONFLICT (tenant_id, name) DO NOTHING"
    )
    # Grant voice_lab:sandbox to VIRTUAL_ASSISTANT itself.
    op.execute(
        "INSERT INTO role_permission (id, tenant_id, role_id, permission_id) "
        "SELECT gen_random_uuid(), NULL, r.id, p.id "
        "FROM role r, permission p "
        f"WHERE r.tenant_id IS NULL AND r.name = '{_ROLE_NAME}' AND p.code = '{_PERMISSION_CODE}' "
        "ON CONFLICT (role_id, permission_id) DO NOTHING"
    )
    # Backfill: every role (system or tenant-custom) currently holding calls:read
    # also gets voice_lab:sandbox, so existing Voice Lab access survives the switch.
    op.execute(
        "INSERT INTO role_permission (id, tenant_id, role_id, permission_id) "
        "SELECT gen_random_uuid(), rp.tenant_id, rp.role_id, p_new.id "
        "FROM role_permission rp "
        "JOIN permission p_old ON p_old.id = rp.permission_id AND p_old.code = 'calls:read' "
        f"JOIN permission p_new ON p_new.code = '{_PERMISSION_CODE}' "
        "ON CONFLICT (role_id, permission_id) DO NOTHING"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permission WHERE permission_id IN "
        f"(SELECT id FROM permission WHERE code = '{_PERMISSION_CODE}')"
    )
    op.execute(f"DELETE FROM role WHERE tenant_id IS NULL AND name = '{_ROLE_NAME}'")
    op.execute(f"DELETE FROM permission WHERE code = '{_PERMISSION_CODE}'")
```

- [ ] **Step 3: Apply it and verify the round-trip**

```bash
cd vera-backend && just up && just migrate
uv run alembic downgrade -1
uv run alembic upgrade head
```

Expected: all three commands exit 0, with no error. This is a manual verification,
not a pytest — this repo has no Alembic-migration test harness (migrations are
exercised implicitly by the integration suite always running against a
fully-migrated head DB), and a backfill migration's effect on pre-existing
production data can't be reproduced by a test suite that only ever starts from a
fresh schema. The `test_virtual_assistant_role_seeded_and_visible` test from Task 1
already covers the forward-going (`rbac_defaults.py`) half of this guarantee.

- [ ] **Step 4: Confirm the wider suite still passes**

```bash
cd vera-backend && just check
```

Expected: PASS (ruff, mypy, pytest all clean).

- [ ] **Step 5: Commit**

```bash
git add vera-backend/migrations/versions/
git commit -m "feat(rbac): backfill voice_lab:sandbox onto existing calls:read roles"
```

---

### Task 3: Endpoint gating — Voice Lab moves off `calls:read`

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/voice_lab.py`
- Modify: `vera-backend/tests/integration/control_plane/test_voice_lab.py`

**Interfaces:**
- Consumes: `RBACWorld.virtual_assistant_token` and `RBACWorld.norole_token` (Task 1).
- Produces: no new symbols — behavior change only (permission string the four
  endpoints check).

- [ ] **Step 1: Write the failing tests**

Add to `vera-backend/tests/integration/control_plane/test_voice_lab.py`, after
`test_voice_lab_requires_auth`:

```python
@pytest.mark.asyncio
async def test_virtual_assistant_can_start_and_end_session(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    fake_livekit: FakeLiveKit,
) -> None:
    started = await client.post(
        "/api/v1/voice-lab/sessions",
        headers=_auth(rbac_world.virtual_assistant_token),
        json={"mode": "browser"},
    )
    assert started.status_code == 200, started.text
    room_name = started.json()["data"]["room_name"]

    ended = await client.delete(
        f"/api/v1/voice-lab/sessions/{room_name}",
        headers=_auth(rbac_world.virtual_assistant_token),
    )
    assert ended.status_code == 200, ended.text


@pytest.mark.asyncio
async def test_norole_denied_voice_lab_session(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.post(
        "/api/v1/voice-lab/sessions",
        headers=_auth(rbac_world.norole_token),
        json={"mode": "browser"},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_virtual_assistant_denied_other_admin_endpoints(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    resp = await client.get("/api/v1/users", headers=_auth(rbac_world.virtual_assistant_token))
    assert resp.status_code == 403, resp.text
```

- [ ] **Step 2: Run them to confirm they fail as expected**

```bash
cd vera-backend && uv run pytest tests/integration/control_plane/test_voice_lab.py -k "virtual_assistant or norole_denied" -v
```

Expected: `test_virtual_assistant_can_start_and_end_session` FAILS with `403` (the
persona holds only `voice_lab:sandbox`, but the endpoint still requires
`calls:read`); the other two already PASS (they don't depend on this task's change).

- [ ] **Step 3: Switch the endpoint gate**

Edit `vera-backend/apps/control_plane/src/control_plane/api/v1/voice_lab.py`.

Update the module docstring's auth note (lines 9-10) — replace:

```python
Auth note (acknowledged stopgap): guards with `require("calls:read")`, matching the
interim convention in `calls.py`.
```

with:

```python
Auth note: guarded by the dedicated `voice_lab:sandbox` permission, kept separate
from `calls:read` (which gates the real call system) so a narrow role like
VIRTUAL_ASSISTANT can use this sandbox without seeing real call data.
```

In `list_call_providers`, replace:

```python
    _caller: VerifiedIdentity = require("calls:read"),
```

with:

```python
    _caller: VerifiedIdentity = require("voice_lab:sandbox"),
```

In `start_voice_session`, replace:

```python
    caller: VerifiedIdentity = require("calls:read"),  # TODO: calls:write once catalog grows
```

with:

```python
    caller: VerifiedIdentity = require("voice_lab:sandbox"),
```

In `end_voice_session`, replace:

```python
    _caller: VerifiedIdentity = require("calls:read"),
```

with:

```python
    _caller: VerifiedIdentity = require("voice_lab:sandbox"),
```

In `stream_transcript`, replace:

```python
    allowed = "calls:read" in permissions
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=user_id,
            actor_label=identity.email or identity.subject,
            event_type=AuditEvent.PHI_ACCESS.value,
            resource_type="transcript",
            resource_id=room_name,
            permission_key="calls:read",
            decision="allow" if allowed else "deny",
            request_id=current_request_id(request),
        )
    )
    if not allowed:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="missing permission calls:read"
        )
```

with:

```python
    allowed = "voice_lab:sandbox" in permissions
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=user_id,
            actor_label=identity.email or identity.subject,
            event_type=AuditEvent.PHI_ACCESS.value,
            resource_type="transcript",
            resource_id=room_name,
            permission_key="voice_lab:sandbox",
            decision="allow" if allowed else "deny",
            request_id=current_request_id(request),
        )
    )
    if not allowed:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="missing permission voice_lab:sandbox"
        )
```

- [ ] **Step 4: Run the tests again to confirm they pass**

```bash
cd vera-backend && uv run pytest tests/integration/control_plane/test_voice_lab.py -v
```

Expected: PASS — all tests in the file, including the pre-existing ones using
`rbac_world.admin_token` (TENANT_ADMIN now carries `voice_lab:sandbox` via Task 1's
`DEFAULT_PERMISSIONS` change, so those are unaffected).

- [ ] **Step 5: Full backend gate**

```bash
cd vera-backend && just check
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add vera-backend/apps/control_plane/src/control_plane/api/v1/voice_lab.py \
        vera-backend/tests/integration/control_plane/test_voice_lab.py
git commit -m "feat(voice-lab): gate on voice_lab:sandbox instead of calls:read"
```

---

### Task 4: Frontend — role picker in the invite dialog

**Files:**
- Modify: `vera-frontend/src/lib/auth/api.ts`
- Modify: `vera-frontend/src/components/users/InviteUserDialog.tsx`

**Interfaces:**
- Produces: `RoleSummary` type `{ id: string; name: string; description: string;
  is_system: boolean }` and `listRoles(): Promise<RoleSummary[]>` in
  `@/lib/auth/api`, matching the backend's `GET /roles` response shape exactly
  (`RoleResponse` in `roles.py`).

- [ ] **Step 1: Add the `GET /roles` client**

Append to `vera-frontend/src/lib/auth/api.ts` (after `deactivateUser`):

```typescript
export type RoleSummary = {
  id: string
  name: string
  description: string
  is_system: boolean
}

/** List roles assignable in the caller's tenant (global system roles + this
 *  tenant's custom roles). Requires `roles:manage`. */
export function listRoles() {
  return apiRequest<RoleSummary[]>(`/roles`)
}
```

- [ ] **Step 2: Wire a role picker into the invite dialog**

Rewrite `vera-frontend/src/components/users/InviteUserDialog.tsx` in full:

```tsx
import { useEffect, useState, type FormEvent } from "react"
import { Check, Copy } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select } from "@/components/ui/select"
import { ApiError } from "@/lib/api/client"
import { inviteUser, listRoles, type InviteUserResult, type RoleSummary } from "@/lib/auth/api"

export function InviteUserDialog({ onInvited }: { onInvited?: () => void } = {}) {
  const [open, setOpen] = useState(false)
  const [email, setEmail] = useState("")
  const [name, setName] = useState("")
  const [sendEmail, setSendEmail] = useState(true)
  const [roles, setRoles] = useState<RoleSummary[]>([])
  const [roleId, setRoleId] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<InviteUserResult | null>(null)
  const [copied, setCopied] = useState(false)

  // Load the assignable roles (global system roles + this tenant's custom roles)
  // each time the dialog opens, so the picker reflects any role created since the
  // last time it was shown.
  useEffect(() => {
    if (!open) return
    let cancelled = false
    listRoles()
      .then((r) => {
        if (!cancelled) setRoles(r)
      })
      .catch(() => {
        // Non-fatal: the invite still works with no role selected.
      })
    return () => {
      cancelled = true
    }
  }, [open])

  function copyLink() {
    if (!result) return
    void navigator.clipboard.writeText(result.invite_url).then(() => setCopied(true))
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const res = await inviteUser({
        email,
        name,
        roleIds: roleId ? [roleId] : [],
        sendEmail,
      })
      setResult(res)
      onInvited?.()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not send invitation.")
    } finally {
      setBusy(false)
    }
  }

  function reset() {
    setOpen(false)
    setEmail(""); setName(""); setSendEmail(true); setRoleId(""); setError(null); setResult(null); setBusy(false); setCopied(false)
  }

  const submitLabel = sendEmail ? "Send invitation" : "Create invitation"
  const submitBusyLabel = sendEmail ? "Sending…" : "Creating…"

  return (
    <Dialog open={open} onOpenChange={(o) => (o ? setOpen(true) : reset())}>
      <DialogTrigger asChild>
        <Button>Invite user</Button>
      </DialogTrigger>
      <DialogContent showCloseButton className="max-w-md gap-0 p-0">
        <DialogHeader className="border-b border-border p-5 pr-12">
          <DialogTitle className="text-base font-semibold">Invite a user</DialogTitle>
          <DialogDescription>
            They'll get a link to set a password and join this workspace.
          </DialogDescription>
        </DialogHeader>

        {result ? (
          <>
            <div className="space-y-4 p-5">
              <p className="text-sm">
                Invitation created for <span className="font-medium">{result.email}</span>
                {result.email_sent ? " and emailed." : "."}
              </p>
              <div className="space-y-1.5">
                <Label htmlFor="invite-url">Invite link</Label>
                <div className="flex items-center gap-2">
                  <Input
                    id="invite-url"
                    readOnly
                    value={result.invite_url}
                    onFocus={(e) => e.target.select()}
                    className="font-mono text-xs"
                  />
                  <Button
                    type="button"
                    variant="outline"
                    size="icon"
                    onClick={copyLink}
                    aria-label={copied ? "Link copied" : "Copy invite link"}
                    title={copied ? "Copied" : "Copy"}
                  >
                    {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
                  </Button>
                </div>
              </div>
            </div>
            <div className="flex justify-end border-t border-border p-4">
              <Button onClick={reset} className="min-w-[120px]">Done</Button>
            </div>
          </>
        ) : (
          <form onSubmit={onSubmit}>
            <div className="space-y-4 p-5">
              <div className="space-y-1.5">
                <Label htmlFor="invite-email">Email</Label>
                <Input
                  id="invite-email"
                  type="email"
                  required
                  autoFocus
                  placeholder="person@company.com"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="invite-name">Name</Label>
                <Input
                  id="invite-name"
                  placeholder="Jane Doe"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="invite-role">Role</Label>
                <Select
                  id="invite-role"
                  value={roleId}
                  onChange={(e) => setRoleId(e.target.value)}
                >
                  <option value="">No role (invite only)</option>
                  {roles.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.name}
                    </option>
                  ))}
                </Select>
              </div>
              <div className="flex items-center gap-2 text-sm">
                <Checkbox
                  id="invite-send-email"
                  checked={sendEmail}
                  onCheckedChange={(checked) => setSendEmail(checked === true)}
                />
                <Label htmlFor="invite-send-email" className="font-normal">
                  Send invitation email
                </Label>
              </div>
              {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
            </div>
            <div className="flex justify-end gap-3 border-t border-border p-4">
              <Button type="button" variant="outline" onClick={reset}>Cancel</Button>
              <Button type="submit" disabled={busy} className="min-w-[120px]">
                {busy ? submitBusyLabel : submitLabel}
              </Button>
            </div>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}
```

- [ ] **Step 3: Type-check and lint**

```bash
cd vera-frontend && npx tsc --noEmit && npx eslint src/lib/auth/api.ts src/components/users/InviteUserDialog.tsx
```

Expected: no errors. There's no RTL/jsdom in this repo, so there's no automated
render test for this component (matches the precedent noted in
`docs/superpowers/specs/2026-06-29-prompt-version-publish-design.md`'s Testing
section) — verify manually in Task 6's manual QA pass instead.

- [ ] **Step 4: Commit**

```bash
git add vera-frontend/src/lib/auth/api.ts vera-frontend/src/components/users/InviteUserDialog.tsx
git commit -m "feat(users): add a role picker to the invite dialog"
```

---

### Task 5: Frontend — permission-driven nav lockdown and default-route redirect

**Files:**
- Modify: `vera-frontend/src/lib/nav.ts`
- Modify: `vera-frontend/src/lib/nav.test.ts`
- Create: `vera-frontend/src/components/auth/RequireNavRoute.tsx`
- Modify: `vera-frontend/src/App.tsx`

**Interfaces:**
- Produces: `isRouteVisible(to: string, ctx: NavContext): boolean` and
  `defaultRouteFor(ctx: NavContext): string` in `@/lib/nav`, and a
  `RequireNavRoute({ to, children }: { to: string; children: ReactNode })` component
  in `@/components/auth/RequireNavRoute`.
- Consumes: `NavContext`, `navItems`, `visibleNavFor` (already in `@/lib/nav`);
  `selectPermissions`, `selectIsSuperAdmin`, `selectIsElevated` (already in
  `@/store/authSlice`).

- [ ] **Step 1: Write the failing nav tests**

Edit `vera-frontend/src/lib/nav.test.ts` in full, replacing its contents with:

```typescript
import { describe, expect, it } from "vitest"

import { defaultRouteFor, isRouteVisible, visibleNavFor } from "@/lib/nav"

const ALL_PERMS = ["forms:read", "calls:read", "users:read"]

describe("visibleNavFor", () => {
  it("tenant user sees permission-gated tenant items, never platform items", () => {
    const titles = visibleNavFor({
      permissions: ["users:read"],
      isSuperAdmin: false,
      isElevated: false,
    }).map((i) => i.title)
    expect(titles).not.toContain("Live Monitoring") // calls:read missing
    expect(titles).toContain("Users") // has users:read
    expect(titles).toContain("Settings") // no permission required
    expect(titles).not.toContain("Data Management") // forms:read missing
    expect(titles).not.toContain("Voice Lab") // voice_lab:sandbox missing
    expect(titles).not.toContain("Tenant Access") // platform-only
    expect(titles).not.toContain("Agent Prompt")
    expect(titles).not.toContain("IVR Playbooks")
  })

  it("virtual_assistant-shaped permission set sees only Voice Lab and Settings", () => {
    const titles = visibleNavFor({
      permissions: ["voice_lab:sandbox"],
      isSuperAdmin: false,
      isElevated: false,
    }).map((i) => i.title)
    expect(titles).toEqual(["Voice Lab", "Settings"])
  })

  it("super admin, NOT elevated: only platform items, tenant items hidden", () => {
    // Even with every tenant permission, the tenant menus stay hidden until elevated.
    const titles = visibleNavFor({
      permissions: ALL_PERMS,
      isSuperAdmin: true,
      isElevated: false,
    }).map((i) => i.title)
    expect(titles).toEqual(["Tenant Access", "Agent Prompt", "IVR Playbooks"])
  })

  it("super admin, elevated: platform items first, then tenant items", () => {
    const titles = visibleNavFor({
      permissions: ALL_PERMS,
      isSuperAdmin: true,
      isElevated: true,
    }).map((i) => i.title)
    expect(titles.slice(0, 3)).toEqual(["Tenant Access", "Agent Prompt", "IVR Playbooks"])
    expect(titles).toContain("Live Monitoring")
    expect(titles).toContain("Users")
  })
})

describe("defaultRouteFor", () => {
  it("sends a virtual_assistant-shaped user to Voice Lab", () => {
    expect(
      defaultRouteFor({ permissions: ["voice_lab:sandbox"], isSuperAdmin: false, isElevated: false })
    ).toBe("/voice-lab")
  })

  it("falls back to Settings when no other item is visible", () => {
    expect(
      defaultRouteFor({ permissions: [], isSuperAdmin: false, isElevated: false })
    ).toBe("/settings")
  })
})

describe("isRouteVisible", () => {
  it("hides a gated route the user lacks the permission for", () => {
    expect(
      isRouteVisible("/", { permissions: ["voice_lab:sandbox"], isSuperAdmin: false, isElevated: false })
    ).toBe(false)
  })

  it("shows a route with no matching nav entry (nothing to gate)", () => {
    expect(
      isRouteVisible("/mfa-enroll", { permissions: [], isSuperAdmin: false, isElevated: false })
    ).toBe(true)
  })
})
```

- [ ] **Step 2: Run it to confirm it fails**

```bash
cd vera-frontend && npx vitest run src/lib/nav.test.ts
```

Expected: FAIL — `defaultRouteFor`/`isRouteVisible` don't exist yet (import error),
and the "Live Monitoring"/"Voice Lab" gating assertions don't match the current
`navItems` permissions.

- [ ] **Step 3: Update `nav.ts`**

In `vera-frontend/src/lib/nav.ts`, replace the `navItems` array:

```typescript
export const navItems: NavItem[] = [
  { title: "Live Monitoring", to: "/", icon: Activity },
  { title: "Data Management", to: "/data-management", icon: Database, permission: "forms:read" },
  { title: "Voice Lab", to: "/voice-lab", icon: Mic, permission: "calls:read" },
  { title: "Call History", to: "/call-history", icon: PhoneCall },
  { title: "Analytics", to: "/analytics", icon: BarChart3 },
  { title: "Tenant Access", to: "/tenant-access", icon: KeyRound, superAdminOnly: true },
  { title: "Agent Prompt", to: "/agent-prompt", icon: Bot, superAdminOnly: true },
  { title: "IVR Playbooks", to: "/ivr-playbooks", icon: ListTree, superAdminOnly: true },
  { title: "Users", to: "/users", icon: Users, permission: "users:read" },
  { title: "Settings", to: "/settings", icon: Settings },
]
```

with:

```typescript
export const navItems: NavItem[] = [
  { title: "Live Monitoring", to: "/", icon: Activity, permission: "calls:read" },
  { title: "Data Management", to: "/data-management", icon: Database, permission: "forms:read" },
  { title: "Voice Lab", to: "/voice-lab", icon: Mic, permission: "voice_lab:sandbox" },
  { title: "Call History", to: "/call-history", icon: PhoneCall, permission: "calls:read" },
  { title: "Analytics", to: "/analytics", icon: BarChart3, permission: "calls:read" },
  { title: "Tenant Access", to: "/tenant-access", icon: KeyRound, superAdminOnly: true },
  { title: "Agent Prompt", to: "/agent-prompt", icon: Bot, superAdminOnly: true },
  { title: "IVR Playbooks", to: "/ivr-playbooks", icon: ListTree, superAdminOnly: true },
  { title: "Users", to: "/users", icon: Users, permission: "users:read" },
  { title: "Settings", to: "/settings", icon: Settings },
]
```

Then add these two functions after `visibleNavFor` (end of the file):

```typescript
/** True if `to` appears in the current user's visible nav — gates a route the same
 *  way its sidebar entry is gated, without duplicating the permission logic.
 *  A route with no matching nav entry has nothing to gate, so it's always visible. */
export function isRouteVisible(to: string, ctx: NavContext): boolean {
  const item = navItems.find((i) => i.to === to)
  if (!item) return true
  return visibleNavFor(ctx).includes(item)
}

/** Where to send a user who can't (or shouldn't) land on the route they hit —
 *  their first visible nav item. Settings carries no permission gate, so this is
 *  never empty for an authenticated tenant user. */
export function defaultRouteFor(ctx: NavContext): string {
  return visibleNavFor(ctx)[0]?.to ?? "/settings"
}
```

- [ ] **Step 4: Run the nav tests again to confirm they pass**

```bash
cd vera-frontend && npx vitest run src/lib/nav.test.ts
```

Expected: PASS.

- [ ] **Step 5: Add the `RequireNavRoute` guard and wire it into `App.tsx`**

Create `vera-frontend/src/components/auth/RequireNavRoute.tsx`:

```tsx
import type { ReactNode } from "react"
import { Navigate } from "react-router-dom"
import { defaultRouteFor, isRouteVisible } from "@/lib/nav"
import { useAppSelector } from "@/store/hooks"
import { selectIsElevated, selectIsSuperAdmin, selectPermissions } from "@/store/authSlice"

/** Wraps a routed page with the same visibility rule as its sidebar entry
 *  (`nav.ts`): if the route isn't in the user's visible nav, redirect to their
 *  first visible item instead of rendering a page they have no access to. */
export function RequireNavRoute({ to, children }: { to: string; children: ReactNode }) {
  const permissions = useAppSelector(selectPermissions)
  const isSuperAdmin = useAppSelector(selectIsSuperAdmin)
  const isElevated = useAppSelector(selectIsElevated)
  const ctx = { permissions, isSuperAdmin, isElevated }
  if (isRouteVisible(to, ctx)) return <>{children}</>
  return <Navigate to={defaultRouteFor(ctx)} replace />
}
```

In `vera-frontend/src/App.tsx`, add the import:

```tsx
import { RequireNavRoute } from "@/components/auth/RequireNavRoute"
```

Replace:

```tsx
            <Route index element={<LiveMonitoring />} />
            <Route path="data-management" element={<DataManagement />} />
            <Route path="users" element={<Users />} />
            <Route path="voice-lab" element={<VoiceLab />} />
            <Route path="call-history" element={<Placeholder title="Call History" />} />
            <Route path="analytics" element={<Placeholder title="Analytics" />} />
```

with:

```tsx
            <Route
              index
              element={<RequireNavRoute to="/"><LiveMonitoring /></RequireNavRoute>}
            />
            <Route path="data-management" element={<DataManagement />} />
            <Route path="users" element={<Users />} />
            <Route path="voice-lab" element={<VoiceLab />} />
            <Route
              path="call-history"
              element={<RequireNavRoute to="/call-history"><Placeholder title="Call History" /></RequireNavRoute>}
            />
            <Route
              path="analytics"
              element={<RequireNavRoute to="/analytics"><Placeholder title="Analytics" /></RequireNavRoute>}
            />
```

- [ ] **Step 6: Type-check, lint, and full frontend test run**

```bash
cd vera-frontend && npx tsc --noEmit && npx eslint src/lib/nav.ts src/App.tsx src/components/auth/RequireNavRoute.tsx && npx vitest run
```

Expected: no errors; all tests pass, including the untouched suites.

- [ ] **Step 7: Commit**

```bash
git add vera-frontend/src/lib/nav.ts vera-frontend/src/lib/nav.test.ts \
        vera-frontend/src/components/auth/RequireNavRoute.tsx vera-frontend/src/App.tsx
git commit -m "feat(nav): lock down nav and default route to the user's visible permissions"
```

---

### Task 6: Simplify, full verification, and manual QA

**Files:** none new — this task reviews and verifies the diff from Tasks 1–5.

- [ ] **Step 1: Run the code-simplifier agent on the full diff**

Per the repo-wide mandate in `CLAUDE.md`, invoke the **code-simplifier** agent
("simplify code") against everything changed in Tasks 1–5. It must not change
behavior — only reconcile style/consistency.

- [ ] **Step 2: Re-run both language gates**

```bash
cd vera-backend && just check
cd vera-frontend && npx tsc --noEmit && npx eslint . && npx vitest run && npx vite build
```

Expected: all PASS. If the simplifier touched anything, fix any newly-introduced
issue and re-run until clean.

- [ ] **Step 3: Manual smoke test**

With `just up`, `just migrate`, and `just seed` run locally (`just api` and the
frontend dev server running):

1. Log in as the seeded `TENANT_ADMIN` (`admin@veratechsolutions.example`).
2. Open **Users → Invite user**, confirm the new **Role** dropdown lists
   `VIRTUAL_ASSISTANT` alongside `TENANT_ADMIN`/`SUPERVISOR`; invite a test email
   with `VIRTUAL_ASSISTANT` selected.
3. Accept the invite as that user, log in.
4. Confirm the sidebar shows only **Voice Lab** and **Settings**, and that landing
   on `/` after login redirects to `/voice-lab` instead of showing Live Monitoring.
5. Start a Voice Lab session (browser mode), confirm it connects; end the session.
6. Manually navigate to `/users` and `/call-history` as this user; confirm each
   redirects back to `/voice-lab` rather than rendering.
7. Log back in as the `TENANT_ADMIN` persona; confirm Live Monitoring, Call
   History, Analytics, and Voice Lab are all still visible and functional
   (regression check for the nav-gate and permission-backfill changes).

- [ ] **Step 4: Final commit (if the simplifier or QA fixes produced changes)**

```bash
git add -A
git commit -m "chore: simplify virtual_assistant role changes"
```

---

### Task 7: Invite-time onboarding traceability — `app_user.invited_by` + backfilled `granted_by`

**Added after Tasks 1-6 shipped**, prompted by a review question: today there is no
way to trace who invited a given user. `AppUser` has no `invited_by` column, and
even `UserRole.granted_by` (the closest existing mechanism) is only set by
`assign_role` — `invite_user`'s own initial role grants leave it `NULL`. This task
closes both gaps. Scope is deliberately backend-only: no API response field, no
frontend UI — just the data model, the migration, and the wiring.

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/models/app_user.py`
- Create: `vera-backend/migrations/versions/<generated>.py` (schema migration, via
  `just makemigration` — never hand-numbered)
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/users.py`
- Modify: `vera-backend/tests/integration/control_plane/test_admin.py`

**Interfaces:**
- Produces: `AppUser.invited_by: Mapped[UUID | None]` — FK to `app_user.id`,
  `ondelete="SET NULL"`, nullable (not every user is invited — e.g. the seeded
  admin, or a future non-invite onboarding path).
- Consumes: nothing new from earlier tasks; this is independent of the RBAC/Voice
  Lab work in Tasks 1-6.

- [ ] **Step 1: Add the column to the model**

Edit `vera-backend/packages/vera_core/src/vera_core/models/app_user.py`. The
current field block (after the class docstring) is:

```python
    gcip_uid: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    account_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AccountType.TENANT.value
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

Replace it with (adds `invited_by`, and the two imports it needs):

```python
    gcip_uid: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    account_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AccountType.TENANT.value
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Who invited this user, for onboarding traceability. NULL for a user who
    # wasn't invited (e.g. a seeded admin). Mirrors UserRole.granted_by's
    # ondelete policy — losing the inviter's own account doesn't cascade.
    invited_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
```

Add the needed imports at the top of the file if not already present — check the
existing import block first; you'll need `UUID` (from `uuid`), `ForeignKey` (from
`sqlalchemy`), and `PG_UUID` aliased from `sqlalchemy.dialects.postgresql.UUID`
(the exact alias pattern is already used in `vera_core/models/rbac.py` — match it):

```python
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
```

- [ ] **Step 2: Generate and apply the migration**

```bash
cd vera-backend && just makemigration "add app_user.invited_by for onboarding traceability"
```

This is a genuine schema change (unlike Task 2's data-only migration), so
`alembic revision --autogenerate` should detect the new column + FK directly from
the model diff and populate `upgrade()`/`downgrade()` itself — inspect the
generated file to confirm it added exactly one column (`invited_by`) and one FK
constraint on `app_user`, nothing else. If autogenerate picked up unrelated drift
(has happened before on this repo — Task 2's implementer saw 3 unrelated index
drops in their autogenerate output), remove anything not about `invited_by` from
both `upgrade()` and `downgrade()`. Confirm `down_revision` chains to the actual
current head (whatever Task 1-6's migration became, or later if more have landed
on `dev` since) — if `alembic heads` shows more than one, run `just merge-heads`,
never hand-edit `down_revision`.

Apply it and verify the round-trip:

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

All three must exit 0.

- [ ] **Step 3: Write the failing test**

Add to `vera-backend/tests/integration/control_plane/test_admin.py`, in the
`# --- users / invitations ---` section, right after
`test_invite_returns_link_and_captures_email`:

```python
async def test_invite_records_inviter_and_role_grant_provenance(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    roles = await client.get("/api/v1/roles", headers=_auth(rbac_world.admin_token))
    supervisor_id = next(r["id"] for r in roles.json()["data"] if r["name"] == "SUPERVISOR")

    invite = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={
            "email": "provenance@test.example",
            "send_email": False,
            "role_ids": [supervisor_id],
        },
    )
    assert invite.status_code == 200, invite.text
    user_id = invite.json()["data"]["user_id"]

    async with admin_sessionmaker() as session:
        invited_by = await session.scalar(
            text("SELECT invited_by FROM app_user WHERE id = :i").bindparams(i=UUID(user_id))
        )
        granted_by, granted_at = (
            await session.execute(
                text(
                    "SELECT granted_by, granted_at FROM user_role"
                    " WHERE app_user_id = :i AND role_id = :r"
                ).bindparams(i=UUID(user_id), r=UUID(supervisor_id))
            )
        ).one()

    admin_id = await session_admin_id(session, rbac_world)  # see note below
    assert str(invited_by) == admin_id
    assert str(granted_by) == admin_id
    assert granted_at is not None
```

`RBACWorld` doesn't expose the admin persona's `app_user.id` directly (only its
session token) — rather than inventing a `session_admin_id` helper, resolve it the
same way the fixture itself does, inline in the test:

```python
    async with admin_sessionmaker() as session:
        admin_id = await session.scalar(
            text("SELECT id FROM app_user WHERE email = 'admin@test.example'")
        )
        invited_by = await session.scalar(
            text("SELECT invited_by FROM app_user WHERE id = :i").bindparams(i=UUID(user_id))
        )
        granted_by, granted_at = (
            await session.execute(
                text(
                    "SELECT granted_by, granted_at FROM user_role"
                    " WHERE app_user_id = :i AND role_id = :r"
                ).bindparams(i=UUID(user_id), r=UUID(supervisor_id))
            )
        ).one()

    assert invited_by == admin_id
    assert granted_by == admin_id
    assert granted_at is not None
```

Use this second version (drop the first draft's `session_admin_id` call — it
doesn't exist). Run it to confirm it fails (the columns aren't set yet):

```bash
uv run pytest tests/integration/control_plane/test_admin.py -k test_invite_records_inviter_and_role_grant_provenance -v
```

Expected: FAIL — `invited_by`/`granted_by` are `NULL`, not the admin's id.

- [ ] **Step 4: Wire it up in `invite_user`**

Edit `vera-backend/apps/control_plane/src/control_plane/api/v1/users.py`.

Replace:

```python
    user = AppUser(
        tenant_id=tenant_id,
        email=email,
        name=body.name,
        status="invited",
        account_type=AccountType.TENANT.value,
    )
    session.add(user)
    await session.flush()
    for role_id in body.role_ids:
        session.add(UserRole(tenant_id=tenant_id, app_user_id=user.id, role_id=role_id))
```

with:

```python
    user = AppUser(
        tenant_id=tenant_id,
        email=email,
        name=body.name,
        status="invited",
        account_type=AccountType.TENANT.value,
        invited_by=caller.user_id,
    )
    session.add(user)
    await session.flush()
    for role_id in body.role_ids:
        session.add(
            UserRole(
                tenant_id=tenant_id,
                app_user_id=user.id,
                role_id=role_id,
                granted_by=caller.user_id,
                granted_at=func.now(),
            )
        )
```

`func` is already imported in this file (used elsewhere) — check the top of the
file first; if it isn't, add `from sqlalchemy import func` (match how `roles.py`
imports it: `from sqlalchemy.sql import func`).

- [ ] **Step 5: Run the test again to confirm it passes**

```bash
cd vera-backend && uv run pytest tests/integration/control_plane/test_admin.py -v
```

Expected: PASS — including the new test and every pre-existing test in the file
(regression check).

- [ ] **Step 6: Full backend gate**

```bash
just check
```

Expected: PASS (ruff, mypy, pytest all clean).

- [ ] **Step 7: Commit**

```bash
git add vera-backend/packages/vera_core/src/vera_core/models/app_user.py \
        vera-backend/migrations/versions/ \
        vera-backend/apps/control_plane/src/control_plane/api/v1/users.py \
        vera-backend/tests/integration/control_plane/test_admin.py
git commit -m "feat(users): record invited_by and backfill granted_by/granted_at at invite time"
```
