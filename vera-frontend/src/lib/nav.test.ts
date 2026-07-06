import { describe, expect, it } from "vitest"

import { visibleNavFor } from "@/lib/nav"

const ALL_PERMS = ["forms:read", "calls:read", "users:read"]

describe("visibleNavFor", () => {
  it("tenant user sees permission-gated tenant items, never platform items", () => {
    const titles = visibleNavFor({
      permissions: ["users:read"],
      isSuperAdmin: false,
      isElevated: false,
    }).map((i) => i.title)
    expect(titles).toContain("Live Monitoring") // no permission required
    expect(titles).toContain("Users") // has users:read
    expect(titles).not.toContain("Data Management") // forms:read missing
    expect(titles).not.toContain("Tenant Access") // platform-only
    expect(titles).not.toContain("Agent Prompt")
    expect(titles).not.toContain("IVR Playbooks")
  })

  it("super admin, NOT elevated: only platform items, tenant items hidden", () => {
    // Even with every tenant permission, the tenant menus stay hidden until elevated.
    const titles = visibleNavFor({
      permissions: ALL_PERMS,
      isSuperAdmin: true,
      isElevated: false,
    }).map((i) => i.title)
    expect(titles).toEqual(["Tenant Access", "Agent Prompt", "IVR Playbooks"])
  })

  it("super admin, elevated: platform items first, then tenant items", () => {
    const titles = visibleNavFor({
      permissions: ALL_PERMS,
      isSuperAdmin: true,
      isElevated: true,
    }).map((i) => i.title)
    expect(titles.slice(0, 3)).toEqual(["Tenant Access", "Agent Prompt", "IVR Playbooks"])
    expect(titles).toContain("Live Monitoring")
    expect(titles).toContain("Users")
  })
})
