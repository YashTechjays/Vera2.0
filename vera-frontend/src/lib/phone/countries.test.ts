import { describe, expect, it } from "vitest"

import {
  COUNTRIES,
  DEFAULT_COUNTRY,
  composeE164,
  dialFor,
  digitsOnly,
  isE164,
} from "./countries"

describe("COUNTRIES", () => {
  it("includes the default country", () => {
    expect(COUNTRIES.some((c) => c.code === DEFAULT_COUNTRY)).toBe(true)
  })

  it("has unique ISO codes and well-formed dial codes", () => {
    const codes = COUNTRIES.map((c) => c.code)
    expect(new Set(codes).size).toBe(codes.length)
    for (const c of COUNTRIES) {
      expect(c.dial).toMatch(/^\+[1-9]\d{0,3}$/)
    }
  })
})

describe("digitsOnly", () => {
  it("strips spaces, dashes, parens and plus signs", () => {
    expect(digitsOnly(" (555) 123-4567 ")).toBe("5551234567")
    expect(digitsOnly("+1 555")).toBe("1555")
  })

  it("returns empty string for non-digit junk", () => {
    expect(digitsOnly("abc--")).toBe("")
  })
})

describe("composeE164", () => {
  it("concatenates the dial code with the national digits", () => {
    expect(composeE164("+1", "5551234567")).toBe("+15551234567")
  })

  it("strips formatting from the national part before composing", () => {
    expect(composeE164("+91", " 98765 43210 ")).toBe("+919876543210")
  })
})

describe("dialFor", () => {
  it("returns the dial code for a known country", () => {
    expect(dialFor("IN")).toBe("+91")
  })

  it("falls back to the default country's dial for an unknown code", () => {
    const defaultDial = COUNTRIES.find((c) => c.code === DEFAULT_COUNTRY)!.dial
    expect(dialFor("ZZ")).toBe(defaultDial)
  })
})

describe("isE164", () => {
  it("accepts a well-formed E.164 number", () => {
    expect(isE164(composeE164("+1", "5551234567"))).toBe(true)
  })

  it("rejects an empty national part", () => {
    expect(isE164(composeE164("+1", ""))).toBe(false)
    expect(isE164(composeE164("+1", "   "))).toBe(false)
  })

  it("rejects non-digit junk that reduces to empty", () => {
    expect(isE164(composeE164("+1", "abc"))).toBe(false)
  })

  it("rejects a number longer than 15 total digits", () => {
    // +1 (1 digit) + 15 national digits = 16 total → too long
    expect(isE164(composeE164("+1", "123456789012345"))).toBe(false)
  })

  it("accepts a number at the 15-digit E.164 ceiling", () => {
    // +1 + 14 national digits = 15 total
    expect(isE164(composeE164("+1", "12345678901234"))).toBe(true)
  })
})
