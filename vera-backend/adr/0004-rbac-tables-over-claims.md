# ADR-0004: RBAC in database tables, not token claims

Date: 2026-06-10 · Status: Accepted

## Context

GCIP tokens can carry custom claims (roles, permissions) minted at login.
Claims-based authz is attractive: zero DB reads per request. But: claims are
frozen at token issuance (revocation lags up to the token TTL), per-tenant
custom roles don't fit a claim schema, and HIPAA access reviews need a
queryable, auditable source of truth for "who can do what, right now".

## Decision

Authorization lives in real tables — `role`, `permission`, `role_permission`,
`user_role(scope_id nullable)` — resolved server-side per request
(`PermissionResolver`), cached in Memorystore keyed (tenant_id, user_id) with
a short TTL and explicit invalidation on role writes. The ONLY claim the token
contributes is identity + tenant_id. There is no role column on user and no
permissions claim.

## Rationale

- **Immediate revocation**: removing a role takes effect within the cache TTL
  (seconds), not the token lifetime.
- **Tenant-defined roles**: roles are tenant-scoped rows; tenants can grow
  custom roles without auth-server changes.
- **`phi:detokenize` is its own permission** — separable, grantable, and every
  evaluation is audited; impossible to express cleanly as a boolean claim.
- **Reviewability**: access reviews are SQL, joined to the audit log.
- `user_role.scope_id` (nullable) leaves room for resource-scoped grants
  (team/queue) without a schema break.

## Consequences

- One extra (cached) DB read per request inside the tenant-scoped session —
  RLS constrains the resolver's own queries.
- Cache invalidation is a correctness requirement: role/user-role mutations
  MUST call `PermissionResolver.invalidate`.
