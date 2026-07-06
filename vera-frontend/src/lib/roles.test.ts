import { describe, expect, it, vi } from "vitest"

// Factory mock (not auto-mock): the real client imports auth/storage, which
// touches sessionStorage at module load — undefined in the node test env.
vi.mock("@/lib/api/client", () => ({ apiRequest: vi.fn(), randomId: vi.fn(() => "id") }))

import { groupPermissionsByPrefix, type Permission } from "./roles"

const perm = (id: string, code: string): Permission => ({ id, code, description: "" })

describe("groupPermissionsByPrefix", () => {
  it("groups by the code's first segment, sorted within and across groups", () => {
    const groups = groupPermissionsByPrefix([
      perm("1", "users:manage"),
      perm("2", "calls:write"),
      perm("3", "calls:read"),
      perm("4", "users:read"),
    ])
    expect(groups.map((g) => g.prefix)).toEqual(["calls", "users"])
    expect(groups[0].permissions.map((p) => p.code)).toEqual(["calls:read", "calls:write"])
    expect(groups[1].permissions.map((p) => p.code)).toEqual(["users:manage", "users:read"])
  })

  it("uses the whole code when there is no colon, and handles empty input", () => {
    expect(groupPermissionsByPrefix([])).toEqual([])
    const groups = groupPermissionsByPrefix([perm("1", "standalone")])
    expect(groups).toEqual([{ prefix: "standalone", permissions: [perm("1", "standalone")] }])
  })
})
