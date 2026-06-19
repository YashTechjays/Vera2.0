import { describe, expect, it } from "vitest"

import {
  schema,
  resolveOptions,
  widgetOf,
  flattenSection,
  getSectionMatrix,
  requiredPaths,
  completionPercent,
  sectionPlacement,
} from "./schema"
import type { IbvField } from "./types"

describe("resolveOptions", () => {
  it("prefers an explicit enum", () => {
    const f: IbvField = { type: "string", title: "X", enum: ["A", "B"] }
    expect(resolveOptions(f)).toEqual(["A", "B"])
  })

  it("falls back to the constraint library", () => {
    const f: IbvField = { type: "string", title: "X", constraint_ref: "YES_NO" }
    expect(resolveOptions(f)).toEqual(["Yes", "No"])
  })

  it("returns [] when neither is present", () => {
    expect(resolveOptions({ type: "string", title: "X" })).toEqual([])
  })
})

describe("widgetOf", () => {
  it("defaults to text", () => {
    expect(widgetOf({ type: "string", title: "X" })).toBe("text")
  })
  it("uses the declared widget", () => {
    expect(widgetOf({ type: "string", title: "X", ui: { widget: "radio" } })).toBe(
      "radio"
    )
  })
})

describe("flattenSection", () => {
  it("prefixes paths with section_key and tracks depth", () => {
    const section = schema.sections.find(
      (s) => s.section_key === "patient_information"
    )!
    const rows = flattenSection(section)
    const chart = rows.find((r) => r.path.endsWith("chart_number"))!
    expect(chart.path).toBe("patient_information.chart_number")
    expect(chart.depth).toBe(0)
  })
})

describe("getSectionMatrix", () => {
  it("returns null for a flat field-row section", () => {
    const patient = schema.sections.find(
      (s) => s.section_key === "patient_information"
    )!
    expect(getSectionMatrix(patient)).toBeNull()
  })

  it("models the general_coverage CPT table", () => {
    const gc = schema.sections.find((s) => s.section_key === "general_coverage")!
    const m = getSectionMatrix(gc)
    expect(m).not.toBeNull()
    expect(m!.columns.map((c) => c.key)).toEqual([
      "covered",
      "copay",
      "coinsurance",
      "prior_auth",
    ])
    expect(m!.groups.length).toBeGreaterThanOrEqual(2)
    expect(m!.hasIcd).toBe(true)
    expect(m!.showGroupColumn).toBe(true)
    expect(m!.rowLabelHeader).toBe("CPT Code")
    expect(m!.rowHeader).toBe("Service")
  })
})

describe("requiredPaths / completionPercent", () => {
  it("reports 100 when every required field is filled", () => {
    const filled = Object.fromEntries(requiredPaths().map((p) => [p, "x"]))
    expect(completionPercent(filled)).toBe(100)
  })
  it("reports 0 for an empty form", () => {
    expect(completionPercent({})).toBe(0)
  })
})

describe("sectionPlacement", () => {
  it("puts the reference sections on the right rail", () => {
    expect(sectionPlacement("hospital_information")).toBe("rail")
    expect(sectionPlacement("provider_reference_information")).toBe("rail")
    expect(sectionPlacement("insurance_representative")).toBe("rail")
  })
  it("puts all other sections in the main column", () => {
    expect(sectionPlacement("patient_information")).toBe("main")
    expect(sectionPlacement("appointment_information")).toBe("main")
    expect(sectionPlacement("verification_information")).toBe("main")
    expect(sectionPlacement("insurance_information")).toBe("main")
    expect(sectionPlacement("benefit_coverage")).toBe("main")
    expect(sectionPlacement("general_coverage")).toBe("main")
  })
})
