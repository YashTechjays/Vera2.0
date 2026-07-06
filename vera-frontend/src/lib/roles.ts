// Typed wrappers over the RBAC endpoints (all gated by roles:manage server-side).
// Mirror the backend contract (snake_case), like api-keys.ts.

import { apiRequest, randomId } from "@/lib/api/client"

/** One catalog entry (GET /permissions). Read-only — permissions are code-defined. */
export type Permission = {
  id: string
  code: string
  description: string
}

/** A role as listed (GET /roles). `is_system` roles are read-only for tenants. */
export type Role = {
  id: string
  name: string
  description: string
  is_system: boolean
}

/** Role detail including its permission set (GET /roles/{id}). */
export type RoleDetail = Role & { permissions: Permission[] }

export function listPermissions(): Promise<Permission[]> {
  return apiRequest<Permission[]>("/permissions")
}

export function listRoles(): Promise<Role[]> {
  return apiRequest<Role[]>("/roles")
}

export function getRole(roleId: string): Promise<RoleDetail> {
  return apiRequest<RoleDetail>(`/roles/${encodeURIComponent(roleId)}`)
}

export function createRole(
  name: string,
  description: string,
  permissionIds: string[],
): Promise<Role> {
  return apiRequest<Role>("/roles", {
    method: "POST",
    body: { name, description, permission_ids: permissionIds },
    headers: { "Idempotency-Key": randomId() },
  })
}

/** PATCH semantics: omitted fields stay unchanged; permission_ids replaces the set. */
export function updateRole(
  roleId: string,
  patch: { name?: string; description?: string; permission_ids?: string[] },
): Promise<RoleDetail> {
  return apiRequest<RoleDetail>(`/roles/${encodeURIComponent(roleId)}`, {
    method: "PATCH",
    body: patch,
  })
}

/** 409 while users still hold the role — the message carries the holder count. */
export function deleteRole(roleId: string): Promise<null> {
  return apiRequest<null>(`/roles/${encodeURIComponent(roleId)}`, { method: "DELETE" })
}

export function listUserRoles(userId: string): Promise<Role[]> {
  return apiRequest<Role[]>(`/users/${encodeURIComponent(userId)}/roles`)
}

export function assignRole(userId: string, roleId: string): Promise<null> {
  return apiRequest<null>(`/users/${encodeURIComponent(userId)}/roles`, {
    method: "POST",
    body: { role_id: roleId },
  })
}

/** 409 when revoking your own last roles:manage source (self-lockout guard). */
export function revokeRole(userId: string, roleId: string): Promise<null> {
  return apiRequest<null>(
    `/users/${encodeURIComponent(userId)}/roles/${encodeURIComponent(roleId)}`,
    { method: "DELETE" },
  )
}

export type PermissionGroup = { prefix: string; permissions: Permission[] }

/** Group the catalog by code prefix (calls:*, users:*, …) for readable checkbox lists. */
export function groupPermissionsByPrefix(permissions: Permission[]): PermissionGroup[] {
  const groups = new Map<string, Permission[]>()
  const sorted = [...permissions].sort((a, b) => a.code.localeCompare(b.code))
  for (const p of sorted) {
    const prefix = p.code.split(":")[0]
    const bucket = groups.get(prefix)
    if (bucket) bucket.push(p)
    else groups.set(prefix, [p])
  }
  return [...groups.entries()].map(([prefix, perms]) => ({ prefix, permissions: perms }))
}
