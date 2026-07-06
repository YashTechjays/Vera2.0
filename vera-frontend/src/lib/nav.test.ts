import { describe, expect, it } from "vitest"

import { defaultRouteFor, isRouteVisible, visibleNavFor } from "@/lib/nav"

const ALL_PERMS = ["forms:read", "calls:read", "users:read"]

describe("visibleNavFor", () => {
  it("tenant user sees permission-gated tenant items, never platform items", () => {
    const titles = visibleNavFor({
      permissions: ["users:read"],
      isSuperAdmin: false,
      isElevated: false,
    }).map((i) => i.title)
    expect(titles).not.toContain("Live Monitoring") // calls:read missing
    expect(titles).toContain("Users") // has users:read
    expect(titles).toContain("Settings") // no permission required
    expect(titles).not.toContain("Data Management") // forms:read missing
    expect(titles).not.toContain("Voice Lab") // voice_lab:sandbox missing
    expect(titles).not.toContain("Tenant Access") // platform-only
    expect(titles).not.toContain("Agent Prompt")
    expect(titles).not.toContain("IVR Playbooks")
  })

  it("virtual_assistant-shaped permission set sees only Voice Lab and Settings", () => {
    const titles = visibleNavFor({
      permissions: ["voice_lab:sandbox"],
      isSuperAdmin: false,
      isElevated: false,
    }).map((i) => i.title)
    expect(titles).toEqual(["Voice Lab", "Settings"])
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

describe("defaultRouteFor", () => {
  it("sends a virtual_assistant-shaped user to Voice Lab", () => {
    expect(
      defaultRouteFor({ permissions: ["voice_lab:sandbox"], isSuperAdmin: false, isElevated: false })
    ).toBe("/voice-lab")
  })

  it("falls back to Settings when no other item is visible", () => {
    expect(
      defaultRouteFor({ permissions: [], isSuperAdmin: false, isElevated: false })
    ).toBe("/settings")
  })
})

describe("isRouteVisible", () => {
  it("hides a gated route the user lacks the permission for", () => {
    expect(
      isRouteVisible("/", { permissions: ["voice_lab:sandbox"], isSuperAdmin: false, isElevated: false })
    ).toBe(false)
  })

  it("shows a route with no matching nav entry (nothing to gate)", () => {
    expect(
      isRouteVisible("/mfa-enroll", { permissions: [], isSuperAdmin: false, isElevated: false })
    ).toBe(true)
  })
})
