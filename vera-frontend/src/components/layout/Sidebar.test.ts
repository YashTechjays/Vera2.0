import { describe, expect, it, vi } from "vitest"

vi.mock("@/lib/auth/storage", () => ({
  getToken: () => null,
  setSession: vi.fn(),
  clearSession: vi.fn(),
}))

import { initialsFor } from "@/components/layout/Sidebar"

describe("initialsFor", () => {
  it("uses the first letter of the first two words for a full name", () => {
    expect(initialsFor("Jane Doe")).toBe("JD")
  })

  it("falls back to the first two characters for a single word", () => {
    expect(initialsFor("jane@example.com")).toBe("JA")
  })

  it("uppercases the result", () => {
    expect(initialsFor("jane doe")).toBe("JD")
  })
})
