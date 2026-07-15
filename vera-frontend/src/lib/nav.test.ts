import { describe, expect, it, vi } from "vitest"

// nav.ts now also exports useNavContext, which pulls in the Redux store chain
// (authSlice -> auth/storage) purely via import graph. That module touches
// sessionStorage at import time, which isn't defined in this file's (node) test
// environment, so stub it out the same way Sidebar.test.ts does.
vi.mock("@/lib/auth/storage", () => ({
  getToken: () => null,
  setSession: vi.fn(),
  clearSession: vi.fn(),
}))

import { defaultRouteFor, isRouteVisible, visibleNavFor } from "@/lib/nav"

const ALL_PERMS = ["forms:read", "calls:read", "users:read"]
// A super admin's /auth/me carries the SUPER_ADMIN role's platform grants.
const PLATFORM_PERMS = [
  "platform:elevations:read",
  "platform:prompts:read",
  "platform:insurance_providers:read",
  "platform:ivr_playbooks:read",
  "platform:form_schemas:read",
]
const SUPER_PERMS = [...PLATFORM_PERMS, ...ALL_PERMS]

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
    expect(titles).not.toContain("Insurance Providers")
    expect(titles).not.toContain("IVR Playbooks")
    expect(titles).not.toContain("Form Schemas")
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
      permissions: SUPER_PERMS,
      isSuperAdmin: true,
      isElevated: false,
    }).map((i) => i.title)
    expect(titles).toEqual([
      "Tenant Access",
      "Agent Prompt",
      "Insurance Providers",
      "IVR Playbooks",
      "Form Schemas",
    ])
  })

  it("platform items are permission-gated: a super admin without the grant loses the item", () => {
    const titles = visibleNavFor({
      permissions: PLATFORM_PERMS.filter((p) => p !== "platform:form_schemas:read"),
      isSuperAdmin: true,
      isElevated: false,
    }).map((i) => i.title)
    expect(titles).not.toContain("Form Schemas")
    expect(titles).toContain("IVR Playbooks")
  })

  it("a tenant user holding a platform permission still never sees platform items", () => {
    // Account type is the backstop: the grant alone must not surface platform UI.
    const titles = visibleNavFor({
      permissions: ["platform:form_schemas:read", "users:read"],
      isSuperAdmin: false,
      isElevated: false,
    }).map((i) => i.title)
    expect(titles).not.toContain("Form Schemas")
    expect(titles).toContain("Users")
  })

  it("super admin, elevated: platform items first, then tenant items", () => {
    const titles = visibleNavFor({
      permissions: SUPER_PERMS,
      isSuperAdmin: true,
      isElevated: true,
    }).map((i) => i.title)
    expect(titles.slice(0, 5)).toEqual([
      "Tenant Access",
      "Agent Prompt",
      "Insurance Providers",
      "IVR Playbooks",
      "Form Schemas",
    ])
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
