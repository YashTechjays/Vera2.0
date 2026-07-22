# Platform User Invite — Design

## Context

Tenant admins can already invite users within their tenant (`POST /users/invitations`,
accept flow at `/tenants/{slug}/auth/invitations/*`). No equivalent exists for platform
operators. The only platform user today is created by `scripts/bootstrap_platform_admin.py`
(`just bootstrap-platform`), which is explicitly a one-time, idempotent seed — it no-ops
the moment any `account_type='platform'` `AppUser` exists.

This gap was deliberate, not an oversight. `docs/superpowers/plans/2026-06-19-platform-operator-login.md`
(the plan that shipped platform login) explicitly deferred it:

> "The platform **invite flow** for ongoing operators (mirroring the tenant invite +
> MFA-on-invite flow, gated by a new `platform:users:invite` permission) is a separable
> subsystem deferred to its own follow-on plan... additional operators are added by
> re-running the bootstrap script until the invite-flow plan ships."

Today there is no way to add a second platform operator at all short of direct DB
surgery. This spec is that follow-on plan.

## Goals

- Let an existing platform operator (`SUPER_ADMIN`) invite another platform operator,
  end to end: invite → email/link → set password → mandatory MFA enrollment → active.
- Give platform operators a management surface (list, deactivate) that doesn't exist yet.
- Close a real lockout risk: a stuck invitee (expired link or expired MFA window) has no
  recovery path in the *existing* tenant flow either. Fix it for both tiers.

## Non-goals

- No new platform role beyond `SUPER_ADMIN` — it is the only platform role today, and
  every invited platform operator is granted it directly. Role selection UI is left for
  if/when a narrower platform role is introduced.
- No change to tenant invite *behavior* other than adding the resend/reset endpoint.

## Data model & permissions

No new tables. Reuses `AppUser` (`account_type='platform'`, `tenant_id=NULL`), `UserRole`
(grants `SUPER_ADMIN`), and the existing `InvitationStore` (Redis, sha256-hashed
single-use tokens) — with **new namespaces** `platform_invite` / `platform_invite_mfa`,
kept distinct from the tenant `invite` / `invite_mfa` namespaces so a platform token can
never be confused with a tenant one.

### Migration

Follows the established pattern (e.g. `20260710_1745_f503e82734cc_seed_form_schemas_read_permission.py`):
adds `platform:users:invite` and `platform:users:read` to `PLATFORM_PERMISSIONS` in
`vera_core/models/rbac_defaults.py`, paired in the same commit with a migration that:

1. `INSERT INTO permission (id, code, description) VALUES (gen_random_uuid(), '<code>', '<description>') ON CONFLICT (code) DO NOTHING`
2. `INSERT INTO role_permission (id, tenant_id, role_id, permission_id) SELECT gen_random_uuid(), NULL, r.id, p.id FROM role r, permission p WHERE r.tenant_id IS NULL AND r.name = 'SUPER_ADMIN' AND p.code = '<code>' ON CONFLICT (role_id, permission_id) DO NOTHING`
3. `downgrade()` raises `RuntimeError`, matching every prior seed migration (a granted
   permission is indistinguishable from live product data once granted).

`platform:users:invite` doubles as the "manage" permission (invite + deactivate + resend),
mirroring how tenant `users:manage` covers invite and deactivate under one permission —
avoids minting a near-duplicate permission.

### RLS constraint: NULL-tenant writes need SECURITY DEFINER functions

The platform-readable RLS policy (`platform_readable_rls_policy_ddl` in `vera_core/db/rls.py`)
is `WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)` — strict equality,
no carve-out for NULL. Under a `platform_session` this evaluates false for any row with
`tenant_id IS NULL`, so **every INSERT or UPDATE of a NULL-tenant row is rejected**, not just
the UPDATE case the codebase already worked around for platform MFA (migration
`f066c667ddc1`). DELETE is unaffected (RLS only evaluates `USING`, not `WITH CHECK`, for
DELETE), so the resend flow's stale-`UserIdentity` cleanup is a plain ORM `DELETE`.

This means invite creation (INSERT `AppUser` + `UserRole`), accept-invite (INSERT
`UserIdentity`), and deactivate/activate-mfa (UPDATE `AppUser.status`) cannot be plain ORM
writes under `platform_session`. Following the exact precedent of `f066c667ddc1` (narrow,
fixed-`search_path` `SECURITY DEFINER` functions owned by `vera_definer_owner`, NOLOGIN/
BYPASSRLS, gated by `current_setting('app.platform', true) = 'on'`, column-scoped GRANTs),
this feature adds three more:

- `platform_create_operator_invite(email, name, invited_by) RETURNS uuid` — atomically
  inserts the invited `AppUser` and the `SUPER_ADMIN` `UserRole` grant.
- `platform_create_password_identity(app_user_id, email, hashed_password) RETURNS uuid` —
  inserts the `UserIdentity` row during accept-invite.
- `platform_set_operator_status(app_user_id, status) RETURNS boolean` — flips `status` to
  `'active'` (activate-mfa) or `'deactivated'` (deactivate), with `status IN ('active',
  'deactivated')` enforced inside the function body so this narrow surface can't be used to
  write an arbitrary status value.

### Audit events

New `AuthEvent` values, kept distinct from the tenant equivalents so platform-operator
lifecycle (privilege-granting activity) is separately auditable:

- `PLATFORM_USER_INVITED`
- `PLATFORM_INVITE_ACCEPTED`
- `PLATFORM_USER_ACTIVATED` (MFA enrollment completed)
- `PLATFORM_USER_DEACTIVATED`
- `PLATFORM_INVITE_RESENT`

Also adds `AuthEvent.INVITE_RESENT` for the tenant-side resend fix.

## Backend API

### Authenticated — tenant tier (existing router, new endpoint)

- `POST /users/{user_id}/resend-invitation` — gated by `users:manage`, only while
  `status="invited"` (409 otherwise, including if the invitee has since gone active).
  Deletes any stale `UserIdentity` row for that `AppUser.id`, reissues a fresh token into
  `INVITE_NS` (72h), resends the email, emits `AuthEvent.INVITE_RESENT`.

### Authenticated — platform tier (new)

- `POST /platform/users/invitations` — mirrors `invite_user`. Creates
  `AppUser(status="invited", account_type=PLATFORM, tenant_id=None, invited_by=caller.user_id)`,
  grants `SUPER_ADMIN` via `UserRole` immediately, mints a token in `platform_invite`
  (72h TTL, same setting as tenant: `settings.invite_ttl_seconds`), builds
  `invite_url = {frontend_base_url}/platform/accept-invite?token=...` (no tenant slug),
  optionally emails via the existing `EmailSender` abstraction (non-fatal failure,
  `invite_url` always returned), emits `PLATFORM_USER_INVITED`.
- `GET /platform/users` — list, gated by `platform:users:read`.
- `POST /platform/users/{user_id}/deactivate` — gated by `platform:users:invite`.
  Refuses (409/422) if this would leave zero active platform operators (lockout guard:
  `count(AppUser WHERE account_type=platform AND status=active) > 1` required).
- `POST /platform/users/{user_id}/resend-invitation` — gated by `platform:users:invite`,
  only while `status="invited"`. Same reset logic as the tenant endpoint, reissuing into
  `platform_invite`, emits `PLATFORM_INVITE_RESENT`.

**Shared implementation**: both resend endpoints call a common internal helper (e.g.
`_reset_and_reissue_invite(session, invites, app_user, namespace)`) that deletes the stale
identity, reissues the token, and rebuilds the invite URL/email — differing only in which
`InvitationStore` namespace and which `AuthEvent` to emit.

### Pre-auth (invitee-facing) — platform tier

Mounted on the existing `platform_auth.py` router, no `{tenant_slug}` in the path (per
the ADR-0006 §A session-shape precedent — platform routes are never tenant-slug-scoped):

- `GET /platform/auth/invitations/validate` — pre-flight check (`valid`/`invalid`/`deactivated`),
  doesn't consume the token.
- `POST /platform/auth/invitations/accept` — sets password (via the new
  `platform_create_password_identity` definer function), unconditionally returns a TOTP
  QR provisioning URI + bridge `mfa_token` (mandatory MFA, no skip path — platform login
  already mandates TOTP for everyone) via the existing `mfa.enroll_platform` helper.
  Leaves `status="invited"`. Emits `PLATFORM_INVITE_ACCEPTED`. Deletes the
  `platform_invite` token (single-use).
- `POST /platform/auth/invitations/activate-mfa` — completes TOTP enrollment via the
  existing `mfa.activate_platform` helper, flips `status="active"` via the new
  `platform_set_operator_status` definer function. **No recovery codes** — platform MFA
  is TOTP-only everywhere in this codebase (consuming a recovery code would need yet
  another definer write on an already-enrolled row; the existing platform login
  enrollment has the identical constraint and the same no-recovery-codes posture). Emits
  `PLATFORM_USER_ACTIVATED`.

Every mutating endpoint gets `Depends(require_idempotency_key)` + `claim_or_conflict(...)`,
per the project-wide idempotency seam requirement.

### Session-shape enforcement

All four new authenticated platform endpoints require an actual `platform_session` (no
tenant GUC set) — never an `elevated_session` (break-glass tenant elevation). A tenant
admin who is currently elevated into a tenant must not be able to reach these routes.

## Frontend

- **New page** `src/pages/PlatformOperators.tsx` — lists platform operators (email,
  status, invited-by), gated by `platform:users:read`. "Invite Operator" button (gated by
  `platform:users:invite`) opens `InvitePlatformOperatorDialog.tsx` — email + name +
  "send email" checkbox (no role picker; always grants `SUPER_ADMIN`), shows the
  resulting `invite_url` with copy-to-clipboard on success. Rows with `status="invited"`
  get a **Resend invitation** action; active rows get **Deactivate** (disabled with a
  tooltip if it's the last active operator).
- **New page** `src/pages/PlatformAcceptInvite.tsx` — same state-machine shape as
  `AcceptInvite.tsx` (`checking → password → mfa → recovery/done`, or
  `invalid`/`deactivated`), but the `mfa` step is unconditional — no branch to skip it.
- **Routes** (`App.tsx`): `/platform/operators` → `PlatformOperators` (inside `AppShell`,
  like `TenantAccess`); `/platform/accept-invite` → `PlatformAcceptInvite` (public,
  pre-auth, outside `AppShell`).
- **Nav** (`src/lib/nav.ts`): new item "Platform Operators", `permission: "platform:users:read"`
  — auto-classified platform-tier by the existing `isPlatformItem()` prefix check.
- **API clients**:
  - `src/lib/api/platform.ts` (unprefixed, matching `listTenants`/`createElevation`):
    `listOperators()`, `inviteOperator(input)`, `deactivateOperator(id)`,
    `resendOperatorInvitation(id)` → `/platform/users/*`.
  - `src/lib/auth/api.ts` (prefixed, matching `platformLogin`): `platformValidateInvite(token)`,
    `platformAcceptInvite(...)`, `platformActivateInviteMfa(...)` → `/platform/auth/invitations/*`.
- **Tenant-side fix**: add a **Resend invitation** action to the existing tenant Users
  list for `status="invited"` rows, calling `resendInvitation(userId)` against
  `POST /users/{user_id}/resend-invitation`.

## Known gap being fixed

Neither the invite link (72h, clock starts at invite creation) nor the MFA bridge token
(72h, clock restarts fresh at password-submit time — not inherited from the original
invite) currently has any resend/reset path in the shipped tenant flow. A user who misses
either window is permanently stuck: `AppUser.status` stays `"invited"`, any re-invite
attempt 409s on the existing email regardless of status, and a second `UserIdentity` for
the same `AppUser.id` is blocked both at the application layer and by a DB unique
constraint (`provider_type`, `provider_subject`). This spec adds the missing resend/reset
endpoint for both tenant and platform tiers (see Backend API above).

**Scoping decision on the old token**: resend's contract is "a fresh working link exists,"
not "the old link is revoked." `InvitationStore` is deliberately keyed by the hash of each
token with no reverse index from `app_user_id` back to a token — tokens are meant to be
un-enumerable. Building one just to revoke a stale token on resend would mean extending
that protocol for marginal benefit: the dominant real case (someone stuck in `"invited"`
status) already means their old link's 72h TTL has lapsed, so there's usually no live
duplicate at all; the token itself carries no PHI (workforce invite). This was confirmed
during implementation (an earlier draft added a full-keyspace Redis SCAN to find and
revoke the old token, which was rejected as disproportionate — see the plan's Task 6 notes).

## Testing plan

**Backend:**
- Migration test: permission seeded + attached to `SUPER_ADMIN`, idempotent re-run.
- Invite creates `account_type=platform, tenant_id=NULL` + immediate `SUPER_ADMIN` grant.
- Accept always returns MFA QR (no skip branch) regardless of any setting.
- Resend: stale `UserIdentity` deleted, fresh token issued (the old token is deliberately
  NOT invalidated — see "Known gap being fixed" below); 409s if invitee already went active.
- Lockout: last active platform operator cannot be deactivated; second-to-last can.
- Session-shape: an elevated tenant session gets 403 on all four new platform endpoints.
- RBAC invariant test extended: `platform:users:invite` / `platform:users:read` can never
  attach to a tenant-scoped role.

**Frontend:**
- Component tests for `PlatformOperators` (list, invite, resend, deactivate-blocked-on-last)
  and the `PlatformAcceptInvite` state machine.
- Component test for the new tenant-side resend button.

**End-to-end (playwright-cli, against the running dev stack):**
- Platform invite flow: log in as the bootstrapped operator → Platform Operators page →
  invite a new operator → capture `invite_url` → open in a fresh browser context → set
  password → complete mandatory MFA (TOTP secret parsed from the QR provisioning URI,
  code computed and submitted) → verify landing as an active platform operator → verify
  the new operator shows `active` in the list.
- Resend flow (both tiers): invite a user, force the token to expire (manipulate the
  Redis TTL in the test harness), click **Resend invitation**, verify the new link works.
  (The stale link is not expected to be actively revoked — see "Known gap being fixed"
  below — so this step doesn't assert on it.)
- Lockout guard: attempt to deactivate the last active platform operator via the UI,
  verify the action is blocked with a clear error rather than silently allowed.

## Verification checklist

- [ ] Backend: `just check` (ruff + mypy --strict + pytest) passes.
- [ ] Frontend: `tsc -b`, `eslint`, `npm test`, `npm run build` all pass.
- [ ] `/simplify` run in-session on the diff before claiming done (repo-wide mandatory rule).
- [ ] Playwright-cli end-to-end flows above run against a live dev stack, not just mocked.
