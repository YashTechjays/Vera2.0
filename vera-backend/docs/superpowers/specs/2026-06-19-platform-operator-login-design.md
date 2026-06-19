# Platform Operator Login — Design

**Date:** 2026-06-19
**Status:** Approved, pending implementation plan
**Related:** ADR-0006 (platform runtime & elevation), ADR-0004 (server-side permission resolution), `apps/control_plane` auth chain
**Coordinates with:** branch `refactore/remove-tenant-slug-from-url-scope` (the flat-URL refactor) — see "Cross-worktree coordination" below. That refactor's own analysis names platform-operator login as its open item; this design is the missing mint path.

## Problem

Login today is tenant-scoped: the only entry point is
`POST /api/v1/tenants/{tenant_slug}/auth/login`, which resolves the slug to a
`tenant_id` and runs everything inside that tenant's RLS session. **Platform
operators** — Vera's own staff (`account_type='platform'`, `tenant_id=NULL`,
holding the `SUPER_ADMIN` role) — belong to no tenant and have no slug, so they
cannot log in at all. ADR-0006 §D earmarked GCIP SSO for this and deferred it,
leaving platform operators creatable only via seeded/minted sessions in tests.

The platform plane is otherwise **already built**: null-tenant `app_user`
(CHECK: `platform ⇔ tenant_id IS NULL`), `SessionData.tenant_id: UUID | None`,
`platform_session` / `platform_scoped_session` / `self_scoped_session`, the
`app.platform` RLS GUC, the `SUPER_ADMIN` system role with `platform:*`
permissions, `platform_require()`, scoped elevation, and null-tenant
`auth_audit_log`. The **only** missing pieces are the login *entry point* and a
way to create the first operator.

## Goal & scope

Add a tenant-less login path for platform operators.

- **Mechanism now:** local password + **mandatory** TOTP MFA.
- **Future-proofing:** built behind a *provider seam* so GCIP SSO later is a new
  `provider_type` behind the same resolver and the same session-minting tail —
  **not** a parallel verify path. (Satisfies the "compatible with GCIP SSO as a
  provider later" requirement and the ADR's warning not to hard-wire the
  mechanism.)
- No tenant slug appears anywhere in the platform login surface.

**Out of scope (YAGNI):** platform-side elevation changes (already built),
operator password-reset, and a `platform:users:read`/list endpoint unless the
invite flow needs it.

## Key conceptual model — why platform ≠ tenant providers

Two distinct *populations of humans*, not two connections to one org:

- **Tenant users** = customers' staff. Each tenant is a customer organization;
  the per-tenant `sso_provider` table holds *N* rows (one IdP per customer),
  keyed by `tenant_id` and protected by tenant RLS.
- **Platform operators** = Vera's own staff. One global population, so their
  provider config is a **single global config**, not per-tenant. It cannot live
  in `sso_provider` (that table is tenant-keyed; a null-tenant operator has
  nothing to key on).

The same human acting in both roles is **two separate `app_user` rows** (the DB
CHECK forces it) — deliberate privilege separation, so a customer-side login
never silently carries break-glass power.

## Design

### 1. The provider seam (GCIP-ready)

- New single-row global config table **`platform_login_provider`** (null-tenant):
  `provider_type`, `enabled`, `enforce_mfa` (with room for GCIP `issuer` /
  `client_id` later). Seeded with one row: `password`, `enabled=true`,
  `enforce_mfa=true`.
- New `resolve_platform_login_provider(session, provider_type) -> LoginProvider | None`
  mirrors the tenant `resolve_login_provider` and returns the **same
  `LoginProvider` dataclass** (`provider_type`, `enforce_mfa`). Reads the global
  config inside a `platform_session`. GCIP later = add a row + a verify branch;
  the session-minting tail is untouched.

### 2. Schema / RLS changes

- `user_identity`: change `TenantScopedMixin` → `NullableTenantColumnMixin`
  (`tenant_id` becomes nullable) and add `user_identity` to
  `PLATFORM_READABLE_TABLES` (currently `("app_user", "user_role")`). This lets a
  `platform_session` (`app.platform='on'`) resolve the operator's null-tenant
  credential row while tenant sessions stay strict/fail-closed. Pre-launch, no
  data → direct column change + dev DB recreate (per the repo bootstrapping
  convention), no backfill.
- New `platform_login_provider` table with a platform-readable (or
  catalog-readable-when-NULL) RLS policy.
- MFA secret ref for platform operators keyed **`mfa/platform/{user_id}`**
  instead of `mfa/{tenant_id}/{user_id}` — a new branch in `mfa.mfa_secret_ref`
  (or a `platform_mfa_secret_ref` helper).

### 3. URL convention (reconciled with the flat-URL refactor)

After `refactore/remove-tenant-slug-from-url-scope`, the URL encodes the plane
**only pre-auth** (no token to resolve from); authenticated routes derive the
plane from the token's `account_type`. Four buckets:

| Bucket | Path shape | Caller | Plane source |
|---|---|---|---|
| Pre-auth | `/tenants/{slug}/auth/*` \| `/platform/auth/*` | anyone | the URL |
| Shared self-actions | flat `/auth/me\|logout\|session/keepalive` | both | token (`self_scoped_session`) |
| Tenant-plane resources | flat `/users`, `/roles`, `/calls`, … | tenant users **and elevated operators** | token tenant **or** active elevation grant |
| Platform-plane resources | `/platform/*` | operators | token (null-tenant) |

`/platform` is the platform-plane analog of the tenant `{slug}` — it identifies
the plane pre-auth, exactly as the slug does for tenants.

**Note (corrected):** flat tenant routes are reachable by an *elevated* operator
(the refactor's `tenant_context` resolves the target tenant from the grant and
uses `elevated_session`); a *non-elevated* operator gets 403. Flat routes never
return "platform" data — operator self-service MFA enroll/providers stay
tenant-plane (`require → tenant_context`), so they are **not** shared with
operators. That is why platform operators enroll MFA at invite-accept/bootstrap,
not via a flat enroll route.

### 4. Endpoints (new)

**Pre-auth platform routes — `/platform/auth/*` (mirror the 4 tenant pre-auth slug routes):**

- `POST /api/v1/platform/auth/login` — body `{email, password}`. Opens a
  `platform_session`, calls `resolve_platform_login_provider`, loads the platform
  credential via `_load_platform_password_creds` (null-tenant variant of
  `_load_password_creds`). On password success mints
  `SessionData(account_type='platform', tenant_id=None, tenant_slug=None,
  provider_type='password', mfa_passed=False, ...)`. MFA is always required →
  returns a challenge token.
- `POST /api/v1/platform/auth/mfa/verify` — verifies TOTP against
  `mfa/platform/{user_id}`, deletes the challenge, mints the full session
  (`mfa_passed=True`).
- `POST /api/v1/platform/auth/invitations/accept` +
  `POST /api/v1/platform/auth/invitations/activate-mfa` — operator onboarding
  (token-based, pre-auth); this is where an invited operator's MFA is enrolled.

**Authenticated platform-plane resource — `/platform/*`:**

- `POST /api/v1/platform/users/invitations` — an operator invites another
  operator; gated by `platform_require('platform:users:invite')`.

**Reused unchanged (shared, plane-agnostic):** `/auth/me`, `/auth/logout`,
`/auth/session/keepalive` — already null-tenant-safe via
`self_scoped_session → platform_session`.

**Dropped from an earlier draft (YAGNI):** a standalone authenticated
`/platform/auth/mfa/enroll` + `/activate`. Platform MFA is enrolled at
bootstrap (operator #1) and at invite-accept (everyone else); a re-enroll
endpoint is not needed initially.

Response/error contract follows the control_plane rules: `ResponseModel[T]` via
`ok(...)`, `CustomAPIException`, `Cache-Control: no-store`. Unknown-email /
disabled-provider / bad-password all collapse to the uniform 401 (no operator
enumeration), mirroring the tenant login posture.

### 5. Bootstrap (operator #1)

`scripts/bootstrap_platform_admin.py` — idempotent, run-once, uses the
definer/BYPASSRLS path like `seed.py`. Reads email/password from env. Creates:

- platform `app_user` (`account_type='platform'`, `tenant_id=NULL`),
- `user_identity` (bcrypt hash + MFA fields, `tenant_id=NULL`),
- `user_role → SUPER_ADMIN`,
- the `platform_login_provider` `password` row if absent,
- **enrolls MFA and prints the `otpauth://` URI once** so the operator can scan
  it immediately.

**No-op if any platform operator already exists.** From then on, operator #1
invites everyone else.

### 6. Ongoing operators (invite flow)

Extend the existing invitation + MFA-on-invite flow with a platform variant:

- `POST /api/v1/platform/users/invitations` (authenticated, platform-plane)
  gated by a new permission **`platform:users:invite`** (added to
  `PLATFORM_PERMISSIONS` and granted to the `SUPER_ADMIN` role).
- The pre-auth accept routes `POST /api/v1/platform/auth/invitations/accept` and
  `.../activate-mfa` create a platform `app_user` (null-tenant) + `SUPER_ADMIN`
  grant + password identity + MFA enrollment, mirroring the tenant invite-accept
  path. Add `platform:users:read` only if a listing endpoint is actually needed.

### 7. Audit

All platform-login events (`LOGIN_FAILURE`, `MFA_CHALLENGE`, `LOGIN_SUCCESS`)
write to `auth_audit_log` with `tenant_id=NULL` (already supported; the column is
nullable and has SELECT/INSERT-only policies). Pre-provider failures
(unknown/disabled) return the uniform un-audited 401, matching the tenant path.

### 8. ADR

Amend **ADR-0006 §D**: record that local password + mandatory MFA behind a
provider seam is the interim platform login, with GCIP SSO as a future provider
row. Keep the ADR the source of truth (do not leave it saying "no platform login
exists").

## Cross-worktree coordination (flat-URL refactor)

This design and `refactore/remove-tenant-slug-from-url-scope` touch the same auth
chain and are mutually dependent. Whichever lands first, the second rebases onto
it. Contract between them:

- **`account_type` on the session is a hard dependency.** The refactor adds
  `account_type` as a **required** field on `SessionData` / `VerifiedIdentity` and
  branches `tenant_context` on it (never on `tenant_id is None`). The platform
  login mint path **must** set `account_type='platform'`, `tenant_id=None`. If
  platform login lands first, it introduces `account_type` itself (matching the
  refactor's enum/field shape) so the refactor rebases cleanly.
- **The refactor builds the consumer; this builds the producer.** The refactor's
  `tenant_context` platform branch (operator → elevation grant's target tenant) is
  test-only until a real `tenant_id=None` session exists. This design is that
  session. After both land, an operator: logs in (null-tenant session) →
  `POST /platform/elevations` (grant) → hits flat tenant routes scoped to the
  target tenant via `elevated_session`.
- **No URL collisions.** Platform login adds only `/platform/auth/*` and
  `/platform/users/invitations`; the refactor only removes `/tenants/{slug}` from
  *authenticated* routes and keeps the 4 tenant pre-auth slug routes. The two path
  sets are disjoint.

## Decisions captured

| Decision | Choice | Rationale |
|---|---|---|
| Auth mechanism | Local password + mandatory TOTP MFA, behind a provider seam | Ships now with no external dependency; GCIP slots in later as a provider |
| Provider config storage | Single-row global `platform_login_provider` table | One global config (not per-tenant); gives GCIP issuer/client-id a home; data-driven toggles |
| Operator #1 | Idempotent `bootstrap_platform_admin.py` | Breaks the chicken-and-egg; run-once, no-op if an operator exists |
| Ongoing operators | Platform variant of the invite + MFA-on-invite flow | Consistent with existing UX; gated by `platform:users:invite` |
| MFA | Mandatory for all platform operators | Break-glass power must not be single-factor |
| `user_identity.tenant_id` | Make nullable + platform-readable RLS | Only way a null-tenant operator can hold a credential row; mirrors `app_user`/`user_role` |

## Risks / notes

- `user_identity` becoming nullable widens a security-critical table — the
  platform-readable RLS policy must be added in the **same** migration so
  null-tenant rows are never visible to tenant sessions.
- The login lookup runs in a `platform_session` **pre-auth** (before the operator
  is authenticated), exactly as tenant login reads creds inside `tenant_session`
  pre-auth. `platform_session` exposes only null-tenant global rows (zero PHI),
  so this is the same trust posture, not a new exposure.
- DB clock discipline still applies: `last_login_at`, audit, and any expiry are
  DB-minted (`func.now()`), never app-clock.
