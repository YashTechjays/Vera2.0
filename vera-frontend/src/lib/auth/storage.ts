// Where the session lives in the browser: held in memory and mirrored to
// sessionStorage so a page refresh doesn't force re-login, while still clearing
// when the tab closes. We persist the opaque session token plus the tenant slug
// (the URL handle every API call needs). The token carries no data — who it
// belongs to is fetched from /auth/me on demand. Session timeouts also come from
// /auth/me, so no session start time is stored client-side.

const TOKEN_KEY = "vera.session_token"
const TENANT_SLUG_KEY = "vera.tenant_slug"
// Non-sensitive hint (no token) for which login plane an in-flight MFA challenge is on,
// so a refresh bounces back to the right login.
const AUTH_PLANE_KEY = "vera.auth_plane"

// sessionStorage only exists in a browser. Unit tests import this module through
// the api-client chain under plain Node, so degrade to memory-only there (reads
// start empty, writes skip the mirror).
const storage: Storage | null = typeof sessionStorage === "undefined" ? null : sessionStorage

let token: string | null = storage?.getItem(TOKEN_KEY) ?? null
let tenantSlug: string | null = storage?.getItem(TENANT_SLUG_KEY) ?? null
let authPlane: string | null = storage?.getItem(AUTH_PLANE_KEY) ?? null

export function getToken(): string | null {
  return token
}

export function getTenantSlug(): string | null {
  return tenantSlug
}

export function getAuthPlane(): string | null {
  return authPlane
}

export function setAuthPlane(plane: "platform" | "tenant"): void {
  authPlane = plane
  storage?.setItem(AUTH_PLANE_KEY, plane)
}

export function setSession(nextToken: string, nextTenantSlug: string): void {
  token = nextToken
  tenantSlug = nextTenantSlug
  storage?.setItem(TOKEN_KEY, nextToken)
  storage?.setItem(TENANT_SLUG_KEY, nextTenantSlug)
  clearAuthPlane()  // challenge complete — the pending-plane hint is moot
}

export function clearSession(): void {
  token = null
  tenantSlug = null
  storage?.removeItem(TOKEN_KEY)
  storage?.removeItem(TENANT_SLUG_KEY)
  clearAuthPlane()
}

function clearAuthPlane(): void {
  authPlane = null
  storage?.removeItem(AUTH_PLANE_KEY)
}
