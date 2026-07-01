// Where the session lives in the browser: held in memory and mirrored to
// sessionStorage so a page refresh doesn't force re-login, while still clearing
// when the tab closes. We persist the opaque session token plus the tenant slug
// (the URL handle every API call needs). The token carries no data — who it
// belongs to is fetched from /auth/me on demand. Session timeouts also come from
// /auth/me, so no session start time is stored client-side.

const TOKEN_KEY = "vera.session_token"
const TENANT_SLUG_KEY = "vera.tenant_slug"

let token: string | null = sessionStorage.getItem(TOKEN_KEY)
let tenantSlug: string | null = sessionStorage.getItem(TENANT_SLUG_KEY)

export function getToken(): string | null {
  return token
}

export function getTenantSlug(): string | null {
  return tenantSlug
}

export function setSession(nextToken: string, nextTenantSlug: string): void {
  token = nextToken
  tenantSlug = nextTenantSlug
  sessionStorage.setItem(TOKEN_KEY, nextToken)
  sessionStorage.setItem(TENANT_SLUG_KEY, nextTenantSlug)
}

export function clearSession(): void {
  token = null
  tenantSlug = null
  sessionStorage.removeItem(TOKEN_KEY)
  sessionStorage.removeItem(TENANT_SLUG_KEY)
}
