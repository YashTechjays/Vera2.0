# ADR-0006: Platform runtime — null-tenant identity, scoped elevation, SECURITY DEFINER writes

Date: 2026-06-16 · Status: Accepted — A/B/C implemented (migration `0002`); **D (GCIP) deferred**
· Update 2026-06-19: token-scoped self-session endpoints (`/auth/me`, `/auth/logout`,
`/auth/session/keepalive`) + session idle/absolute-cap lifecycle implemented (see §A,
`vera2-database-design.md` §3.5.2)
· Update 2026-06-21: interim local **password + mandatory TOTP** platform login landed
(`/api/v1/platform/auth/*`, migration `0011`, `bootstrap_platform_admin.py`) behind a
provider seam — a stand-in for §D until GCIP provisioning unblocks (see §D-interim)

> **Compliance caveat.** `vera2-database-design.md` §5.1 flags `tenant_elevation` as
> *subject to change pending compliance review*. A/B/C are built but inert until a
> platform operator and a grant exist; if compliance reshapes or drops scoped
> elevation, revisit §B/§C before any production use.

## Context

The password + MFA + `sso_provider` login work (opaque Redis sessions replacing
the GCIP/static verifier switch) deliberately scoped itself to **tenant users**:
`VerifiedIdentity.tenant_id` is a non-null `UUID`, every request opens a
`tenant_session(tenant_id)` that sets the `app.tenant_id` GUC, and RLS confines
every query to one tenant.

The schema is already provisioned for the platform tier (verified against
`migrations/versions/0001_initial.py` + models, see `vera2-database-design.md`
§3.5.4 / §3.5.9):

- `app_user.account_type ∈ {tenant, platform}` with a CHECK pairing `platform`
  ⇔ `tenant_id IS NULL` (a SUPER_ADMIN is a tenant-less operator).
- `role.tenant_id` nullable; `SUPER_ADMIN` is a global system role (seeded in
  `rbac_defaults.SYSTEM_ROLES`). `role` / `role_permission` use the lenient
  *catalog* RLS policy (`vera_core.db.rls.catalog_rls_policy_ddl`) so a session
  can read global rows.
- `tenant_elevation(super_admin_user_id, target_tenant_id, reason, granted_at,
  expires_at, ended_at)` with a **SELECT-only** RLS policy: an elevated session
  (GUC = target) reads its own grant. Grant creation and the platform "all
  active elevations" read are explicitly **not** policy-covered.
- `audit_log.elevation_session_id` FK links PHI access back to the grant.
- `auth_audit_log.tenant_id` is **nullable** for platform-level events.

What does **not** exist yet (the runtime this ADR governs):

1. **No null-tenant / no-GUC session path.** `tenant_session` requires a UUID;
   an unset GUC is fail-closed (zero rows). A platform operator with
   `tenant_id IS NULL` has no session shape today.
2. **No SECURITY DEFINER functions.** `tenant_elevation` INSERT, the
   cross-tenant "active elevations" read, and null-tenant `auth_audit_log`
   INSERT are all blocked by RLS with no sanctioned path through.
3. **`DatabaseAuthAuditWriter` rejects null-tenant records** (raises
   `NotImplementedError`) — see `vera_core/audit/writer.py`.
4. **No GCIP login path.** GCIP is intended as another per-tenant
   `sso_provider`, exchanged for the same opaque session at login.

## Decision (deferred — to be implemented in a follow-up plan)

Build the platform runtime as a distinct, separately-reviewed body of work:

### A. Platform identity & session shape
- Make `VerifiedIdentity.tenant_id` `UUID | None`. A platform session carries
  `tenant_id = None` and `user_id` of a `platform` `app_user`.
- Add a **no-GUC platform session** for platform-scoped reads (global catalog
  tables only). It must never touch a tenant-scoped PHI table without first
  establishing an elevation GUC. `tenant_guard` stays mandatory on tenant routes
  and rejects a null-tenant identity outright.
- **Self-session endpoints are token-scoped, not tenant routes** (implemented
  2026-06-19): `GET /auth/me`, `POST /auth/logout`, `POST /auth/session/keepalive`
  authenticate via `current_identity` only — no `{tenant_slug}`, no `tenant_guard` —
  and derive any RLS scope from the verified identity via `self_scoped_session`
  (a tenant user's own `tenant_session`; a platform operator's no-GUC
  `platform_session`). This is what lets a platform operator read/refresh/end **their
  own** session without an elevation grant — `tenant_guard` would otherwise 403 a
  null-tenant caller. Pre-auth routes (`login`, `mfa/verify`, `invitations/*`) stay
  tenant-slug-scoped (the slug is the only pre-token tenant selector).
- **Privilege comes only from RBAC** (`user_role → SUPER_ADMIN`), never from
  `tenant_id IS NULL` (`vera2-database-design.md` §3.5.9 security rule).

### B. Scoped elevation (break-glass)
- `POST /platform/elevations` → create a time-boxed, single-tenant grant
  (reason required); `POST /platform/elevations/{id}/end` → revoke early.
- An elevated request sets the **normal tenant GUC = target_tenant_id**, so RLS
  is fully in force; PHI rows read while elevated stamp
  `audit_log.elevation_session_id`. App-side enforcement checks
  `expires_at > now()` and `ended_at IS NULL` per request.

### C. SECURITY DEFINER functions (new migration required)
Narrow, schema-qualified, `SECURITY DEFINER` functions owned by a privileged
role — **never `BYPASSRLS`** on the app role (§3.5.9):
- `create_elevation_grant(super_admin_user_id, target_tenant_id, reason, duration_minutes) → uuid`
  (expiry is computed DB-side as `now() + interval` — no app clock takes part, so
  every elevation timestamp comes from the single database clock)
- `end_elevation_grant(elevation_id) → void`
- `log_auth_event(tenant_id | NULL, app_user_id, event_type, ip, meta) → uuid`
  (unblocks null-tenant `auth_audit_log`; replaces the current
  `NotImplementedError` guard in `DatabaseAuthAuditWriter`).
- `active_elevation_grants(...) → setof` for the platform oversight read.
Each sets a fixed `search_path` and is the *only* sanctioned write path for its
table.

### D. GCIP as a login provider — **DEFERRED** (interim password+MFA login landed 2026-06-21)
- Re-introduce GCIP token verification as a **login path** (`/auth/sso` or a
  token-exchange endpoint) that, on success, mints the same opaque session —
  not as a request-time `TokenVerifier`. Per-tenant enablement via the existing
  `sso_provider` row (`provider_type='google_oidc'`, `gcip_provider_id`).
- **GCIP not implemented in this pass.** GCIP *is* the production platform-operator
  login path, and it is blocked on GCIP tenant provisioning (DevOps).

#### D-interim — local password + mandatory TOTP platform login (landed 2026-06-21)
Because GCIP is blocked on DevOps but operators need a first-class login now, an
interim local login landed behind a provider seam so GCIP can drop in later:
- **`POST /api/v1/platform/auth/login` + `/mfa/verify`** (`api/v1/platform_auth.py`) —
  the tenant-less sibling of the tenant login: no `{tenant_slug}` (the operator belongs
  to no tenant). Credentials + provider config resolve inside a `platform_session`
  (`app.platform='on'`, no tenant GUC) — exactly the RLS context that exposes the
  NULL-tenant `app_user` / `user_identity` / `platform_login_provider` rows and zero PHI.
- **MFA is mandatory.** Login never mints a session directly; it always returns a
  `verify` challenge completed at `/mfa/verify`, which mints the
  `account_type='platform'`, `tenant_id=None` session. Failures are a uniform 401
  (no operator/provider enumeration); outcomes audit to `auth_audit_log` with
  `tenant_id=NULL`.
- **Provider seam:** a single NULL-tenant `platform_login_provider` row
  (`provider_type='password'`, `enforce_mfa=true`), seeded by migration `0011`,
  gates the path — flipping `enabled=false` disables password login globally, and a
  future `google_oidc` row is where GCIP plugs in. `user_identity.tenant_id` was made
  nullable and both tables put on `platform_readable_rls_policy_ddl` (same migration).
- **Operator #1 is bootstrapped, not invited.** `scripts/bootstrap_platform_admin.py`
  (`just bootstrap-platform`) is a run-once, idempotent seed of the FIRST operator
  (NULL-tenant platform `app_user` + password identity + global SUPER_ADMIN grant +
  inline MFA enrollment, printing the `otpauth://` URI once). It no-ops if any platform
  operator already exists; subsequent operators come via the platform invite flow.
- **Two RLS-imposed limitations** (the platform-readable policy's WITH CHECK is strict
  equality, so the RLS-bound app role cannot UPDATE a NULL-tenant row):
  1. **TOTP is the only second factor** — recovery-code consumption mutates the identity
     row and would fail the WITH CHECK; `mfa.verify` accepts a current TOTP as a pure read.
  2. **No `last_login_at` stamping** — login time is recorded via the `LOGIN_SUCCESS`
     auth-audit row instead. Lifting either needs a SECURITY DEFINER write path (see
     `devops-todo.md`), to land with §C-style functions.
- **This interim path is replaced, not extended, by GCIP.** When §D lands, GCIP becomes
  the authority and this local password provider should be disabled in production.
- **Tenant URLs carry a `tenant.slug`, not the UUID** (resolved to the tenant id at
  login via the `resolve_tenant_by_slug` SECURITY DEFINER fn, since the `tenant` RLS
  policy is fail-closed pre-auth). The slug is a convenience layer only — GCIP
  identity binds to UUIDs/GCIP ids (`tenant.gcip_tenant_id`, `app_user.gcip_uid`),
  never to the slug — so a slug rename never breaks an SSO mapping, and the slug is
  the natural pre-auth tenant selector for choosing which GCIP tenant to sign into.
- **⚠ MANDATORY cross-check when §D lands: never trust the URL slug alone for
  identity.** With password login the slug-resolved tenant is the authority and the
  password is checked inside it; with GCIP the *token* is the authority. The SSO
  endpoint MUST verify `verified_token.firebase.tenant == resolved_tenant.gcip_tenant_id`
  (then resolve `app_user` by `gcip_uid` within that tenant). Naively copying the
  password pattern — "slug → tenant → admit" — would let a user authenticated against
  GCIP tenant X into Vera tenant Y by putting Y's slug in the URL (cross-tenant auth
  bug). A pre-auth "GCIP config for this slug" lookup is also needed; apply the same
  uniform-response discipline as the unknown-slug 401 to avoid tenant enumeration.

## Implementation notes (what landed — A/B/C, migration `0002`)

- **Platform visibility uses an `app.platform` GUC, not a catalog policy.** A new
  `platform_readable_rls_policy_ddl` on `app_user`/`user_role` reads a NULL-tenant
  row only when `app.platform='on'`. This preserves the `NullableTenantColumnMixin`
  invariant — a tenant session still never sees platform rows — while
  `platform_session` (flag on, no tenant GUC) resolves SUPER_ADMIN RBAC and
  `elevated_session` (flag on + tenant GUC) sees both the operator's grant and the
  target tenant. No new disclosure to tenants, so no compliance ruling was needed.
- **The four SECURITY DEFINER functions are owned by a `NOLOGIN BYPASSRLS` role
  (`vera_definer_owner`)** — never the app role (§3.5.9 holds). The app role reaches
  them only via EXECUTE; WORM immutability and tenant isolation are intact for every
  non-definer connection.
- **One active grant per operator** is enforced by a partial unique index
  (`WHERE ended_at IS NULL`); the original `UNIQUE(super_admin_user_id, ended_at)`
  was ineffective (NULLs are distinct).
- **Elevation is implicit, re-checked per request.** A platform session reaching
  `/tenants/{id}/...` is elevated iff an active grant `(operator, id)` exists; no
  separate elevated token. `tenant_guard` allows it, the scoped session pins the
  tenant GUC, and the authz/PHI audit rows stamp `audit_log.elevation_session_id`.
- **Platform-tier authz** (`platform:elevations:{create,end,read}`, SUPER_ADMIN-only)
  audits allow/deny to `auth_audit_log` via new `AUTHZ_ALLOW`/`AUTHZ_DENY` events —
  the PHI `audit_log` is tenant-scoped and cannot hold a platform row.

## Consequences
- A second migration adds the SECURITY DEFINER functions + the role that owns
  them; RLS policies on `tenant_elevation` / `auth_audit_log` stay as-is (the
  functions are the write path, not new policies).
- Tooling/tests need a platform-session fixture and a privileged role to own the
  functions, distinct from the `vera_rls_test` role used today.
- With A/B/C landed: the runtime exists and is tested, but stays inert until an
  operator + a grant are seeded. The interim password+MFA login (§D-interim, landed
  2026-06-21) now provides a first-class platform login so an operator *can* be
  bootstrapped and authenticate; GCIP (§D) remains the intended production path and
  replaces it. Scoped elevation still waits on compliance sign-off before production use.

## References
- `vera2-database-design.md` §3.5.4 (scoped elevation), §3.5.7, §3.5.9 (platform tier).
- ADR-0004 (RBAC in tables, not claims) — privilege is an RBAC grant, not an attribute.
- `migrations/versions/0001_initial.py` (the `tenant_elevation` policy comment marks this deferral).
