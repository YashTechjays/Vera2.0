# control_plane — PHI-returning HTTP endpoints (scoped)

Inherits the repo root `vera-backend/CLAUDE.md`. Rules for any endpoint that returns or
accepts PHI. The existing chain to copy is `auth/identity.py → deps.tenant_context →
auth/rbac.py (require) → tenant-scoped session → audit` (see `api/v1/calls.py`).

## Platform vs tenant identity — `account_type` is the source of truth

`app_user.account_type` (`'tenant'` | `'platform'`) is the **definitive** signal for whether
a caller is a tenant member or a platform operator (SUPER_ADMIN). When you branch
platform-vs-tenant, read `account_type` — carry it on `VerifiedIdentity` / `SessionData` so
it's available without a DB hit.

**`tenant_id IS NULL` is only a *derived proxy*, NOT the signal.** The DB CHECK
`tenant_binding` pairs `account_type='platform'` with `tenant_id IS NULL` *at rest*, but a
session is minted in code and round-tripped through Redis — the two can diverge on a minting
bug (and slug-less / partial sessions already exist). Treat `tenant_id` nullability as an
invariant to **assert** alongside `account_type`, and **fail closed (401) on a mismatch** —
never as the thing you branch on. A tenant user momentarily missing `tenant_id` that gets
classified as a platform operator is a privilege-escalation-shaped misroute into the elevation
path. Do not write `if identity.tenant_id is None:` to mean "is a platform operator."

## Display-path chain — in this exact order, every PHI-returning endpoint

1. **Authenticate** — verified GCIP identity (`auth/identity.py`).
2. **Authorize** — RBAC `require("phi:read")` (or the specific permission) via `auth/rbac.py`.
   No PHI returned without this in front of it.
3. **Set tenant context** — `tenant_context` + tenant-scoped session (`SET LOCAL app.tenant_id`,
   RLS applied).
4. **Query** — fetch rows (CMEK decrypts transparently at the storage layer; the app receives
   plaintext).
5. **Audit the disclosure** — call `emit_phi_read_audit()` (`api/v1/common.py`) with the
   field **names** (never values), before returning. **Never construct `AuditRecord(...)`
   inline at a new call site** — hand-rolled construction is exactly how `request_id` and
   `elevation_session_id` (the link back to a superadmin's active elevation grant) went
   missing at several endpoints before `emit_phi_read_audit` existed. If a shape doesn't
   fit the helper (e.g. an SSE endpoint folding an authz decision into the same record,
   like `calls.py::stream_call_events`), that's a signal to extend the helper or add a
   sibling one — not to reach for `AuditRecord(...)` directly.
6. **Serialize minimized plaintext** — only the fields the purpose needs, with
   `Cache-Control: no-store`.

## Response & error contract (every endpoint, §7.1)

- Return `ResponseModel[T]` via `ok(payload)` (`responses.py`) — never a bare model/dict.
  Declare `response_model=ResponseModel[T]` + `responses=CustomAPIResponse.custom(...)` on the route.
- Errors: `raise CustomAPIException` / its subclasses (`exceptions.py`); never `HTTPException`.
  Each `ExceptionCode` / `DefaultExceptionCode` maps to a fixed HTTP status — add new codes there.
  `register_exception_handlers(app)` serializes everything into the FAIL envelope.
- PHI: `message`/`description` are developer-authored, non-PHI; handlers never echo raw exception
  text or submitted input (validation errors expose field paths only). Read the correlation id
  with `current_request_id(request)`, not the raw header.
- Mutating ingress: gate with `Depends(require_idempotency_key)` + `claim_or_conflict(...)` (Redis
  in-flight lock); durable de-dup is a UNIQUE constraint on the resource, not Redis
  (`idempotency.py`, ADR vera2-database-design §707).

## Identifiers in URLs

Path and query identifiers are opaque UUIDv7 (ADR-0002) — never PHI, never a guessable
sequential id, never a raw identifier in a route template. `/patients/{ssn}` is forbidden;
use `/patients/{patient_id}` where `patient_id` is an opaque UUID.

**Exception — the tenant `{tenant_slug}`** (ADR-0006 §D): pre-auth routes (`login`,
`mfa/verify`, `invitations/*`) accept a human-readable `tenant.slug` in the URL. It's an
organizational handle, not PHI / a per-patient id. Resolve it with `auth.tenant_slug`
(→ `resolve_tenant_by_slug` SECURITY DEFINER fn) — a plain `SELECT … WHERE slug=…` returns
0 rows pre-auth (tenant RLS is fail-closed). `tenant_context` derives the UUID from the
verified session; downstream code stays UUID-keyed. Unknown/malformed slug → the uniform
401/403, never a distinct shape (no tenant enumeration).

## Token-scoped self-session endpoints

`/auth/me`, `/auth/logout`, `/auth/session/keepalive` are the exception to the display-path
chain: they act on the caller's **own** opaque session token, carry **no `{tenant_slug}`**.
Authenticate with `current_identity` only, then derive any DB scope from the verified identity
via `self_scoped_session` (`tenant_session` for a tenant user, `platform_session` for a platform
operator) — never from request input. They return non-PHI session metadata, so **no `phi:read`
gate and no PHI-access audit** — but still `Cache-Control: no-store`. Don't route these through
`tenant_context`: a null-tenant (platform) caller would be denied, locking platform operators
out of their own session (ADR-0006 §A). Keepalive slides the idle TTL, capped at the absolute
max (`vera2-database-design.md` §3.5.2). Pre-auth routes (`login`, `mfa/verify`,
`invitations/*`) stay tenant-slug-scoped.

## Masking

Mask PHI by default in responses. Unmasking is a **separate, separately-audited** action
behind its own permission — not a query flag that silently widens disclosure.

## Minimum necessary

Return the narrowest set of fields the caller's purpose requires (ADR-0005). Adding a field
to a PHI response is a disclosure decision — justify it; do not default to "return the whole row."

## KMS dep injection

`app.state.kms` holds the process-wide `KeyManagementService`. `build_kms(settings)` picks the
implementation from `settings.kms_key_name`. Tests always inject `LocalDevKMS(master_key=b"a"*32)`
directly into `create_app(kms=...)` — never rely on the env var in tests. Never construct a KMS
instance outside of `build_kms` or test fixtures.
