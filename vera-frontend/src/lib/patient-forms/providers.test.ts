import { describe, expect, it } from "vitest"

import { matchProvider } from "./providers"
import type { ProviderOption } from "./types"

const PROVIDERS: ProviderOption[] = [
  { id: "p-cigna", name: "Cigna" },
  { id: "p-uhc", name: "UnitedHealthcare" },
]

describe("matchProvider (send-to-queue auto-select)", () => {
  it("matches an exact name", () => {
    expect(matchProvider(PROVIDERS, "Cigna")).toBe("p-cigna")
  })

  it("matches case-insensitively and ignores surrounding whitespace", () => {
    expect(matchProvider(PROVIDERS, "  cigna  ")).toBe("p-cigna")
    expect(matchProvider(PROVIDERS, "UNITEDHEALTHCARE")).toBe("p-uhc")
  })

  it("returns '' when the intake string matches no catalog provider", () => {
    expect(matchProvider(PROVIDERS, "Cigna Health")).toBe("")
  })

  it("returns '' for a null/empty provider string", () => {
    expect(matchProvider(PROVIDERS, null)).toBe("")
    expect(matchProvider(PROVIDERS, "")).toBe("")
  })
})
