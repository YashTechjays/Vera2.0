import { describe, expect, it } from "vitest"

import { packTwoColumns } from "./layout"

type Item = { name: string; h: number }

function item(name: string, h: number): Item {
  return { name, h }
}

function names(column: Item[]): string[] {
  return column.map((i) => i.name)
}

describe("packTwoColumns", () => {
  it("stacks short sections beside a tall one instead of alternating", () => {
    // Alternation would pair tall(6) with short(1) and leave [mid(2), mid(3)] under tall —
    // packing puts every short section into the second column.
    const [left, right] = packTwoColumns(
      [item("tall", 6), item("one", 1), item("two", 2), item("three", 3)],
      (i) => i.h
    )
    expect(names(left)).toEqual(["tall"])
    expect(names(right)).toEqual(["one", "two", "three"])
  })

  it("preserves relative order within each column", () => {
    const [left, right] = packTwoColumns(
      [item("a", 2), item("b", 2), item("c", 2), item("d", 2)],
      (i) => i.h
    )
    expect(names(left)).toEqual(["a", "c"])
    expect(names(right)).toEqual(["b", "d"])
  })

  it("handles empty and single-item runs", () => {
    expect(packTwoColumns([], () => 1)).toEqual([[], []])
    const [left, right] = packTwoColumns([item("only", 4)], (i) => i.h)
    expect(names(left)).toEqual(["only"])
    expect(right).toEqual([])
  })
})
