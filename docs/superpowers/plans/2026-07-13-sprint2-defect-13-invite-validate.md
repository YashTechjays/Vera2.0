# Sprint 2 Defect #13: Invite Validate Endpoint + Pre-flight UI Check

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Block the Set Password form for ineligible invited users (deactivated) by adding a lightweight token-scoped `GET .../invitations/validate` endpoint and calling it on mount in AcceptInvite.tsx before rendering the form.

**Architecture:** The backend gets a new `GET /tenants/{tenant_slug}/auth/invitations/validate?token=...` endpoint that looks up the token and returns `{ "state": "valid" | "invalid" | "deactivated" }` — no PHI, no token consumption. The frontend AcceptInvite.tsx gains a `"checking"` phase on mount that calls the new endpoint and gates which screen is shown.

**Tech Stack:** FastAPI (Python 3.12, asyncio, SQLAlchemy async, Pydantic v2), React + TypeScript (Vite, React Router v6, Zod), pytest + httpx for integration tests, ruff + mypy on the backend, tsc + eslint on the frontend.

## Global Constraints

- Python 3.12, PEP 695 type params, asyncio-only (no anyio).
- All backend responses use `ResponseModel[T]` via `ok(payload)`; errors via `raise CustomAPIException / UnauthorizedError`.
- Pre-auth route lives under `/tenants/{tenant_slug}/auth/invitations/...` (same prefix as `accept` and `activate-mfa`).
- Unknown/malformed slug → uniform 401 — no tenant enumeration; unknown/expired/bogus token → `"invalid"` (uniform — no distinction between missing, expired, or used).
- Endpoint must NOT consume/delete the token and must NOT reveal email/name/user-id.
- `Cache-Control: no-store` on every response in the validate endpoint and the accept endpoint (defense-in-depth).
- Frontend: no PHI in URLs/state; no `localStorage`; tight TypeScript types.
- Tests for backend live under `tests/integration/control_plane/` (the CI testpath).
- After implementation run `/simplify` per repo-wide CLAUDE.md; then re-run `just check` (backend) and `tsc + eslint + npm test` (frontend).

---

### Task 1: Backend — validate endpoint + no-store header on accept

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/auth.py` (lines 170–180 for new model; line 737 for new route; line 818 for no-store header on accept response)

**Interfaces:**
- Produces: `GET /tenants/{tenant_slug}/auth/invitations/validate?token=...` → `ResponseModel[InviteValidateResponse]`
- `InviteValidateResponse.state: Literal["valid", "invalid", "deactivated"]`

- [ ] **Step 1: Read the file before editing (required before any Edit)**

Read `vera-backend/apps/control_plane/src/control_plane/api/v1/auth.py` in full to understand current imports, model definitions, and the accept_invitation route structure. Key sections:
- Lines 170–180: `AcceptInviteRequest`, `AcceptInviteResponse`, `ActivateInviteMfaRequest` models — insert new model nearby.
- Lines 737–818: `accept_invitation` endpoint — insert new endpoint before it; add `response.headers["Cache-Control"] = "no-store"` inside accept_invitation.
- Line 74: `router = APIRouter(tags=["auth"])` — the new route goes on the same router.
- Note the existing imports: `Response` is already imported from `fastapi`; `INVITE_NS` from `control_plane.auth.invitations`; `AppUser` from `vera_core.models`; `select` from `sqlalchemy`.

- [ ] **Step 2: Add `InviteValidateResponse` model**

In `vera-backend/apps/control_plane/src/control_plane/api/v1/auth.py`, after the `ActivateInviteMfaRequest` class (around line 185), add:

```python
class InviteValidateResponse(BaseModel):
    state: Literal["valid", "invalid", "deactivated"]
```

The `Literal` import is already present (line 26).

- [ ] **Step 3: Add the validate endpoint**

In `vera-backend/apps/control_plane/src/control_plane/api/v1/auth.py`, add the following new route **before** the `accept_invitation` route (before line 737). The function signature matches the accept route's dependency pattern but uses GET with a query param:

```python
@router.get(
    "/tenants/{tenant_slug}/auth/invitations/validate",
    response_model=ResponseModel[InviteValidateResponse],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
    ),
)
async def validate_invitation(
    tenant_slug: str,
    token: str,
    response: Response,
    sessionmaker: Sessionmaker,
    invites: Invites,
) -> ResponseModel[InviteValidateResponse]:
    """Token-scoped invite pre-flight: returns the eligibility state without
    consuming the token or revealing any PHI. Because the caller must already
    possess the high-entropy secret token, this does not enable enumeration.
    `Cache-Control: no-store` — the result reflects live DB state."""
    response.headers["Cache-Control"] = "no-store"
    tenant_id = await resolve_tenant_id(sessionmaker, tenant_slug)
    invite = await invites.get(INVITE_NS, token)
    if tenant_id is None or invite is None or invite.tenant_id != tenant_id:
        return ok(InviteValidateResponse(state="invalid"))

    async with tenant_session(sessionmaker, tenant_id) as session:
        user = (
            await session.execute(select(AppUser).where(AppUser.id == invite.app_user_id))
        ).scalar_one_or_none()

    if user is None:
        return ok(InviteValidateResponse(state="invalid"))
    if user.status == "invited":
        return ok(InviteValidateResponse(state="valid"))
    if user.status == "deactivated":
        return ok(InviteValidateResponse(state="deactivated"))
    # already activated, or any other non-invited, non-deactivated status → invalid
    return ok(InviteValidateResponse(state="invalid"))
```

- [ ] **Step 4: Add `Cache-Control: no-store` to `accept_invitation`**

The `accept_invitation` function (around line 747) currently takes no `response: Response` parameter. Add it:

```python
async def accept_invitation(
    tenant_slug: str,
    body: AcceptInviteRequest,
    request: Request,
    response: Response,          # ← add this
    sessionmaker: Sessionmaker,
    kms: KMS,
    audit: AuthAudit,
    invites: Invites,
    settings: AppSettings,
) -> ResponseModel[AcceptInviteResponse]:
```

Then at the top of the function body (before the `tenant_id = await resolve_tenant_id(...)` line), add:

```python
    response.headers["Cache-Control"] = "no-store"
```

- [ ] **Step 5: Run ruff and mypy to check**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-backend
uv run ruff check apps/control_plane/src/control_plane/api/v1/auth.py
uv run mypy apps/control_plane/src/control_plane/api/v1/auth.py
```

Expected: no errors (ignore known livekit errors in dtmf.py/livekit_gateway.py — those are different files). Fix any errors before proceeding.

- [ ] **Step 6: Commit**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-backend
git add apps/control_plane/src/control_plane/api/v1/auth.py
git commit -m "fix(auth): token-scoped invite validate endpoint + no-store (sprint-2 #13)

Adds GET /tenants/{tenant_slug}/auth/invitations/validate?token=... returning
{state: valid|invalid|deactivated}. Does not consume the token, reveals no PHI.
Also adds Cache-Control: no-store to the accept_invitation response.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014PNLDcrjuaRB8hhYc4sQUj"
```

---

### Task 2: Backend — integration tests for the validate endpoint

**Files:**
- Modify: `vera-backend/tests/integration/control_plane/test_admin.py`

**Interfaces:**
- Consumes: `GET /api/v1/tenants/{tid}/auth/invitations/validate?token=<token>` from Task 1
- Consumes: `rbac_world` fixture (provides `tenant_id`, `admin_token`) from conftest.py
- Consumes: `client: httpx.AsyncClient` from conftest.py

- [ ] **Step 1: Understand what fixtures are available**

The test file already has imports and fixtures from conftest.py:
- `client: httpx.AsyncClient` — the ASGI test client
- `rbac_world: RBACWorld` — provides `rbac_world.tenant_id` (UUID) and `rbac_world.admin_token`
- `_auth(token)` and `_idem()` helpers already defined at the top of the test file

The invite flow pattern used in existing tests (e.g., `test_invite_then_accept_activates_user`):
1. POST `/api/v1/users/invitations` with admin auth + idempotency key → get `invite_url`
2. Extract token from `invite_url.split("token=", 1)[1]`
3. Hit the endpoint under test

For the deactivated test:
4. Deactivate the user: POST `/api/v1/users/{user_id}/deactivate` with admin auth
5. Hit validate → expect `"deactivated"`

- [ ] **Step 2: Write the four failing tests**

Open `vera-backend/tests/integration/control_plane/test_admin.py` and add these four tests after the existing `test_deactivate_user` test (around line 222). Place them as a coherent block:

```python
# --- invitations/validate -----------------------------------------------------


async def test_validate_valid_token(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    """A fresh invite token returns state='valid'."""
    tid = rbac_world.tenant_id
    invite = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"email": "validate_valid@test.example", "send_email": False},
    )
    assert invite.status_code == 200, invite.text
    token = invite.json()["data"]["invite_url"].split("token=", 1)[1]

    resp = await client.get(
        f"/api/v1/tenants/{tid}/auth/invitations/validate",
        params={"token": token},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["state"] == "valid"
    # No PHI in response body
    assert "email" not in data
    assert "user_id" not in data
    assert "name" not in data
    # Cache-Control header set
    assert resp.headers.get("cache-control") == "no-store"


async def test_validate_deactivated_user_token(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    """A token whose user has been deactivated returns state='deactivated'."""
    tid = rbac_world.tenant_id
    invite = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"email": "validate_deactivated@test.example", "send_email": False},
    )
    assert invite.status_code == 200, invite.text
    user_id = invite.json()["data"]["user_id"]
    token = invite.json()["data"]["invite_url"].split("token=", 1)[1]

    # Deactivate the user before they accept
    deactivate = await client.post(
        f"/api/v1/users/{user_id}/deactivate",
        headers=_auth(rbac_world.admin_token),
    )
    assert deactivate.status_code == 200, deactivate.text

    resp = await client.get(
        f"/api/v1/tenants/{tid}/auth/invitations/validate",
        params={"token": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["state"] == "deactivated"
    assert resp.headers.get("cache-control") == "no-store"


async def test_validate_bogus_token_returns_invalid(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    """A missing/bogus token returns state='invalid' (not a 4xx error)."""
    tid = rbac_world.tenant_id
    resp = await client.get(
        f"/api/v1/tenants/{tid}/auth/invitations/validate",
        params={"token": "this-is-not-a-real-token"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["state"] == "invalid"
    assert resp.headers.get("cache-control") == "no-store"


async def test_validate_used_token_returns_invalid(
    client: httpx.AsyncClient, rbac_world: RBACWorld
) -> None:
    """After a token is consumed by accept, validate returns state='invalid'."""
    tid = rbac_world.tenant_id
    invite = await client.post(
        "/api/v1/users/invitations",
        headers={**_auth(rbac_world.admin_token), **_idem()},
        json={"email": "validate_used@test.example", "send_email": False},
    )
    assert invite.status_code == 200, invite.text
    token = invite.json()["data"]["invite_url"].split("token=", 1)[1]

    # Consume the token via accept
    accept = await client.post(
        f"/api/v1/tenants/{tid}/auth/invitations/accept",
        json={"token": token, "password": "strong-password-123"},
    )
    assert accept.status_code == 200, accept.text

    # Now validate returns invalid (token consumed, user is no longer "invited")
    resp = await client.get(
        f"/api/v1/tenants/{tid}/auth/invitations/validate",
        params={"token": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["state"] == "invalid"
```

- [ ] **Step 3: Run the tests to see them fail (TDD red)**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-backend
uv run pytest tests/integration/control_plane/test_admin.py::test_validate_valid_token \
  tests/integration/control_plane/test_admin.py::test_validate_deactivated_user_token \
  tests/integration/control_plane/test_admin.py::test_validate_bogus_token_returns_invalid \
  tests/integration/control_plane/test_admin.py::test_validate_used_token_returns_invalid \
  -v 2>&1 | tail -30
```

Expected: 4 failures (FAILED with 404/connection error — endpoint doesn't exist yet if run before Task 1, or PASS if Task 1 already done). If the DB is unavailable (`Can't locate revision` Alembic drift), note the error and run with `--ignore` of unrelated tests — the important thing is seeing the endpoint tests fail/pass cleanly.

- [ ] **Step 4: Run the tests to see them pass (TDD green)**

After Task 1 is done (endpoint exists), run the same command above. Expected: 4 PASSED.

If the DB is unavailable, run:
```bash
uv run pytest tests/integration/control_plane/test_admin.py -k "validate" -v 2>&1 | tail -30
```
And note whether the skip is due to DB unavailability (acceptable — document in report) vs a code bug (fix it).

- [ ] **Step 5: Run the full auth integration suite to avoid regressions**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-backend
uv run pytest tests/integration/control_plane/test_admin.py \
  tests/integration/control_plane/test_login_flow.py -v 2>&1 | tail -40
```

Expected: all pass (or pre-existing DB-drift skip, not new failures).

- [ ] **Step 6: Commit**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-backend
git add tests/integration/control_plane/test_admin.py
git commit -m "test(auth): integration tests for invite validate endpoint (sprint-2 #13)

Four cases: valid token, deactivated user's token, bogus token, used token.
Asserts no-store header and no PHI in response body.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014PNLDcrjuaRB8hhYc4sQUj"
```

---

### Task 3: Frontend — validateInvite API call + AcceptInvite pre-flight

**Files:**
- Modify: `vera-frontend/src/lib/auth/api.ts`
- Modify: `vera-frontend/src/pages/AcceptInvite.tsx`

**Interfaces:**
- Consumes: `GET /tenants/{slug}/auth/invitations/validate?token=...` from Task 1
- Produces: `validateInvite(slug: string, token: string): Promise<InviteValidateResult>` in `api.ts`
- Produces: New `"checking"` phase in `AcceptInvite.tsx` Phase union; renders loading spinner → then correct screen

- [ ] **Step 1: Read both files before editing**

Read `vera-frontend/src/lib/auth/api.ts` (all) and `vera-frontend/src/pages/AcceptInvite.tsx` (all) to confirm current structure. Key things to note:
- `api.ts` line 52: `const tenantAuth = (slug: string) => \`/tenants/${encodeURIComponent(slug)}/auth\``
- `api.ts` line 106: `acceptInvite` function — mirror this pattern for `validateInvite` using GET
- `AcceptInvite.tsx` line 13-17: the `Phase` union — add `{ kind: "checking" }` and `{ kind: "deactivated" }` and `{ kind: "invalid" }`
- `AcceptInvite.tsx` line 25: `useState<Phase>({ kind: "password" })` — change initial state to `{ kind: "checking" }`
- `AcceptInvite.tsx` line 66: the `!token` guard — keep it, but move below checking phase rendering

- [ ] **Step 2: Add `validateInvite` to `vera-frontend/src/lib/auth/api.ts`**

Add after the `acceptInvite` function (after line 112):

```typescript
export type InviteValidateResult = {
  state: "valid" | "invalid" | "deactivated"
}

export function validateInvite(slug: string, token: string) {
  return apiRequest<InviteValidateResult>(
    `${tenantAuth(slug)}/invitations/validate?token=${encodeURIComponent(token)}`,
    { method: "GET", auth: false },
  )
}
```

- [ ] **Step 3: Rewrite AcceptInvite.tsx with pre-flight check**

Replace the full content of `vera-frontend/src/pages/AcceptInvite.tsx` with:

```typescript
import { useState, type FormEvent, useEffect } from "react"
import { useNavigate, useParams, useSearchParams } from "react-router-dom"
import { QRCodeSVG } from "qrcode.react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { PasswordInput } from "@/components/ui/password-input"
import { Label } from "@/components/ui/label"
import { ApiError } from "@/lib/api/client"
import { RecoveryCodes } from "@/components/auth/RecoveryCodes"
import { acceptInvite, activateInviteMfa, validateInvite } from "@/lib/auth/api"

type Phase =
  | { kind: "checking" }
  | { kind: "invalid" }
  | { kind: "deactivated" }
  | { kind: "password" }
  | { kind: "mfa"; mfaToken: string; provisioningUri: string | null }
  | { kind: "recovery"; codes: string[] }
  | { kind: "done" }

export function AcceptInvite() {
  const { tenantSlug = "" } = useParams()
  const [params] = useSearchParams()
  const token = params.get("token") ?? ""
  const navigate = useNavigate()

  const [phase, setPhase] = useState<Phase>({ kind: "checking" })
  const [password, setPassword] = useState("")
  const [code, setCode] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const loginHref = "/login"

  useEffect(() => {
    if (!token) {
      setPhase({ kind: "invalid" })
      return
    }
    let cancelled = false
    validateInvite(tenantSlug, token)
      .then((res) => {
        if (cancelled) return
        if (res.state === "valid") {
          setPhase({ kind: "password" })
        } else if (res.state === "deactivated") {
          setPhase({ kind: "deactivated" })
        } else {
          setPhase({ kind: "invalid" })
        }
      })
      .catch(() => {
        if (!cancelled) setPhase({ kind: "invalid" })
      })
    return () => {
      cancelled = true
    }
  }, [tenantSlug, token])

  async function onSetPassword(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const res = await acceptInvite(tenantSlug, token, password)
      if (res.mfa_required) {
        setPhase({ kind: "mfa", mfaToken: res.mfa_token ?? "", provisioningUri: res.provisioning_uri })
      } else {
        setPhase({ kind: "done" })
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "This invitation is invalid or has expired.")
    } finally {
      setBusy(false)
    }
  }

  async function onActivateMfa(e: FormEvent) {
    e.preventDefault()
    if (phase.kind !== "mfa") return
    setError(null)
    setBusy(true)
    try {
      const res = await activateInviteMfa(tenantSlug, phase.mfaToken, code)
      setPhase({ kind: "recovery", codes: res.recovery_codes })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Activation failed.")
    } finally {
      setBusy(false)
    }
  }

  if (phase.kind === "checking") {
    return (
      <CenteredCard title="Checking invitation…" desc="Please wait a moment.">
        <div className="flex justify-center py-4">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
        </div>
      </CenteredCard>
    )
  }

  if (phase.kind === "invalid") {
    return (
      <CenteredCard title="Invalid invitation" desc="This invite link is missing, invalid, or has expired.">
        <Button className="w-full" onClick={() => navigate(loginHref)}>Go to sign in</Button>
      </CenteredCard>
    )
  }

  if (phase.kind === "deactivated") {
    return (
      <CenteredCard
        title="Account deactivated"
        desc="This account has been deactivated. Please contact your administrator."
      >
        <Button className="w-full" onClick={() => navigate(loginHref)}>Go to sign in</Button>
      </CenteredCard>
    )
  }

  if (phase.kind === "recovery") {
    return (
      <CenteredCard title="Account ready" desc="Save your recovery codes, then sign in.">
        <RecoveryCodes codes={phase.codes} onContinue={() => navigate(loginHref, { replace: true })} />
      </CenteredCard>
    )
  }

  if (phase.kind === "done") {
    return (
      <CenteredCard title="Account active" desc="Your account is ready.">
        <Button className="w-full" onClick={() => navigate(loginHref, { replace: true })}>Sign in</Button>
      </CenteredCard>
    )
  }

  if (phase.kind === "mfa") {
    return (
      <CenteredCard title="Set up two-factor" desc="Scan the QR code, then enter a code to finish.">
        <div className="space-y-4">
          {phase.provisioningUri && (
            <div className="flex justify-center rounded-md bg-white p-4">
              <QRCodeSVG value={phase.provisioningUri} size={180} />
            </div>
          )}
          <form onSubmit={onActivateMfa} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="code">Authentication code</Label>
              <Input id="code" inputMode="numeric" autoComplete="one-time-code" required
                value={code} onChange={(e) => setCode(e.target.value)} />
            </div>
            {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
            <Button type="submit" className="w-full" disabled={busy}>{busy ? "Activating…" : "Activate"}</Button>
          </form>
        </div>
      </CenteredCard>
    )
  }

  return (
    <CenteredCard title="Accept your invitation" desc="Choose a password to activate your account.">
      <form onSubmit={onSetPassword} className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="password">Password</Label>
          <PasswordInput id="password" autoComplete="new-password" required minLength={8}
            value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
        <Button type="submit" className="w-full" disabled={busy}>{busy ? "Saving…" : "Set password"}</Button>
      </form>
    </CenteredCard>
  )
}

function CenteredCard({ title, desc, children }: { title: string; desc: string; children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="text-lg">{title}</CardTitle>
          <CardDescription>{desc}</CardDescription>
        </CardHeader>
        <CardContent>{children}</CardContent>
      </Card>
    </div>
  )
}
```

Key behaviors preserved from the original:
- `{ replace: true }` on `navigate(loginHref, ...)` after `done` and `recovery` (fix #14 preserved).
- `ApiError` import and usage in catch blocks.
- All existing `mfa`, `recovery`, `done` phases and their rendering.

- [ ] **Step 4: Run TypeScript check**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-frontend
npx tsc --noEmit 2>&1 | head -40
```

Expected: no errors. If `useEffect` or `import` errors appear, check that `useEffect` was added to the react import line (it was already destructured from "react" in the original; the rewrite adds it).

- [ ] **Step 5: Run ESLint**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-frontend
npm run lint 2>&1 | head -40
```

Expected: no errors (or pre-existing warnings unrelated to these two files).

- [ ] **Step 6: Run frontend tests**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-frontend
npm test -- --run 2>&1 | tail -30
```

Expected: all tests pass. The AcceptInvite page has no existing unit tests (the test files are `clipboard.test.ts`, `nav.test.ts`, `roles.test.ts`), so no regressions expected.

- [ ] **Step 7: Commit**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-frontend
git add src/lib/auth/api.ts src/pages/AcceptInvite.tsx
git commit -m "fix(fe): pre-flight invite validation blocks set-password for ineligible users (sprint-2 #13)

AcceptInvite.tsx now calls GET .../invitations/validate on mount before showing
the Set Password form. Deactivated accounts see a clear message; invalid/expired
tokens show the existing invalid-link screen. Loading state shown during check.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014PNLDcrjuaRB8hhYc4sQUj"
```

---

### Task 4: Simplify + Gate Run + Report

**Files:**
- Run: `/simplify` on recently modified files
- Write: `/Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/.superpowers/sdd/sprint2-m13-report.md`

**Interfaces:**
- Consumes: all files modified in Tasks 1–3
- Produces: report file at `.superpowers/sdd/sprint2-m13-report.md`

- [ ] **Step 1: Run the code-simplifier (mandatory per repo-wide CLAUDE.md)**

Invoke the `/simplify` skill on the recently modified files:
- `vera-backend/apps/control_plane/src/control_plane/api/v1/auth.py`
- `vera-backend/tests/integration/control_plane/test_admin.py`
- `vera-frontend/src/lib/auth/api.ts`
- `vera-frontend/src/pages/AcceptInvite.tsx`

The simplifier only cleans up clarity/consistency — it does NOT change behavior. After it runs, re-run all gates below.

- [ ] **Step 2: Run the full backend gate**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-backend
just check 2>&1 | tail -50
```

If the DB is unavailable (`Can't locate revision`), run just lint + mypy and run the affected tests directly:
```bash
uv run ruff check .
uv run mypy --ignore-missing-imports apps/control_plane/src/ packages/vera_core/src/ 2>&1 | grep -v "dtmf.py\|livekit_gateway.py"
uv run pytest tests/integration/control_plane/test_admin.py -k "validate or accept" -v 2>&1 | tail -30
```

- [ ] **Step 3: Run the full frontend gate**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-frontend
npx tsc --noEmit && npm run lint && npm test -- --run
```

- [ ] **Step 4: Write the report**

Create `/Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/.superpowers/sdd/sprint2-m13-report.md`:

```markdown
# Sprint 2 Defect #13 — Invite Validate Endpoint Fix

## Endpoint Contract

`GET /tenants/{tenant_slug}/auth/invitations/validate?token=<token>`

- **Auth:** none (pre-auth, token-gated)
- **Response:** `ResponseModel<{ state: "valid" | "invalid" | "deactivated" }>`
- **Headers:** `Cache-Control: no-store`
- **Token consumed:** NO
- **PHI disclosed:** NONE (no email, name, user_id in response)

State semantics:
- `valid` → token resolves, user.status == "invited" → show Set Password form
- `deactivated` → token resolves, user.status == "deactivated" → show clear message
- `invalid` → token missing/expired/not-found OR user missing OR user already activated → show invalid-link screen (uniform — no enumeration between cases)

## Files Changed

| File | Lines |
|------|-------|
| `vera-backend/apps/control_plane/src/control_plane/api/v1/auth.py` | +`InviteValidateResponse` model; +`validate_invitation` route; +`response: Response` param + `Cache-Control: no-store` header in `accept_invitation` |
| `vera-backend/tests/integration/control_plane/test_admin.py` | +`test_validate_valid_token`, `test_validate_deactivated_user_token`, `test_validate_bogus_token_returns_invalid`, `test_validate_used_token_returns_invalid` |
| `vera-frontend/src/lib/auth/api.ts` | +`InviteValidateResult` type; +`validateInvite()` function |
| `vera-frontend/src/pages/AcceptInvite.tsx` | Rewrote Phase union to include `checking`/`invalid`/`deactivated`; added `useEffect` on mount to call `validateInvite`; added loading spinner + deactivated screen |

## Enumeration Safety

The validate endpoint requires the caller to possess the high-entropy 43-character URL-safe token (256 bits of entropy from `secrets.token_urlsafe(32)`) to get anything other than `"invalid"`. The token is hashed with SHA-256 at rest (Redis key). An attacker without the token cannot probe user statuses. Unknown slugs, unknown tokens, and already-used tokens all return HTTP 200 `{ state: "invalid" }` — no distinct error shapes that would reveal whether a tenant or user exists.

The `"deactivated"` state is only reachable with a valid, unexpired token whose user is specifically in deactivated status. Since that token was generated by the invite flow and delivered to the invited user, revealing "deactivated" to the token holder is appropriate — they need to know to contact their admin rather than retry.

## Test Evidence

| Test | Expected | Actual |
|------|----------|--------|
| `test_validate_valid_token` | state=valid, no-store header, no PHI | ✓ |
| `test_validate_deactivated_user_token` | state=deactivated, no-store header | ✓ |
| `test_validate_bogus_token_returns_invalid` | state=invalid, 200 OK | ✓ |
| `test_validate_used_token_returns_invalid` | state=invalid after token consumed | ✓ |

## Gate Results

Backend:
- `ruff check .`: PASS
- `mypy` (excluding dtmf.py/livekit_gateway.py known errors): PASS
- `pytest tests/integration/control_plane/test_admin.py -k validate`: PASS / [note DB status]

Frontend:
- `tsc --noEmit`: PASS
- `npm run lint`: PASS
- `npm test -- --run`: PASS

## Regression Check

- Fix #14 (`{ replace: true }` on post-activation navigate) preserved: `navigate(loginHref, { replace: true })` remains in `done` and `recovery` phases.
- Existing `accept_invitation` behavior unchanged: token is still consumed on POST, `Cache-Control: no-store` is new defense-in-depth.
```

- [ ] **Step 5: Final commit for report**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0
mkdir -p .superpowers/sdd
git add .superpowers/sdd/sprint2-m13-report.md
git commit -m "docs(sprint2): defect #13 fix report

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014PNLDcrjuaRB8hhYc4sQUj"
```

---

## Self-Review

**Spec coverage:**
1. ✅ Token-scoped validate endpoint `GET .../invitations/validate?token=...` — Task 1
2. ✅ States: valid/invalid/deactivated — Task 1 endpoint logic
3. ✅ No token consumption, no PHI, Cache-Control: no-store — Task 1
4. ✅ Cache-Control: no-store on `accept_invitation` — Task 1 Step 4
5. ✅ Integration tests for all four cases — Task 2
6. ✅ Frontend loading state, deactivated screen, invalid screen, valid → form — Task 3
7. ✅ Fix #14 preserved (`{ replace: true }`) — Task 3 Step 3 code explicitly carries it
8. ✅ Simplify step + gates — Task 4
9. ✅ Report to `.superpowers/sdd/sprint2-m13-report.md` — Task 4

**Placeholder scan:** No TBDs, all code blocks are complete.

**Type consistency:**
- `InviteValidateResponse` (Python) / `InviteValidateResult` (TypeScript) both use `state` field with the same three literal values.
- `validateInvite` in `api.ts` referenced identically in the import line of `AcceptInvite.tsx`.
- `Phase.kind` values match all `if (phase.kind === ...)` branches.
