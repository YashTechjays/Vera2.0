import { describe, expect, it } from "vitest"

import { lastPageOf, slicePage } from "./pagination"

describe("lastPageOf", () => {
  it("rounds up to whole pages", () => expect(lastPageOf(41, 20)).toBe(3))
  it("is exact on a full last page", () => expect(lastPageOf(40, 20)).toBe(2))
  it("never drops below page 1, even empty", () => expect(lastPageOf(0, 20)).toBe(1))
})

describe("slicePage", () => {
  const rows = ["a", "b", "c", "d", "e"]
  it("returns the requested window", () => expect(slicePage(rows, 2, 2)).toEqual(["c", "d"]))
  it("returns a short final page", () => expect(slicePage(rows, 3, 2)).toEqual(["e"]))
  it("returns empty past the end", () => expect(slicePage(rows, 9, 2)).toEqual([]))
})
