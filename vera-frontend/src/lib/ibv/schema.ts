import rawSchema from "./ibv-schema.json"
import type { FlatField, FormValues, IbvField, IbvSchema, IbvSection } from "./types"

export const schema = rawSchema as unknown as IbvSchema

/** Options for a select/radio field: explicit enum wins, else constraint_library. */
export function resolveOptions(field: IbvField): string[] {
  if (field.enum?.length) return field.enum
  if (field.constraint_ref) {
    return schema.constraint_library[field.constraint_ref]?.values ?? []
  }
  return []
}

/** Effective widget for a leaf field (defaults to text). */
export function widgetOf(field: IbvField): "text" | "textarea" | "radio" {
  return field.ui?.widget ?? "text"
}

/**
 * Flatten a section's properties into ordered render rows. Object fields are
 * emitted as a group header followed by their (indented) children.
 */
export function flattenSection(section: IbvSection): FlatField[] {
  const out: FlatField[] = []
  const walk = (props: Record<string, IbvField>, prefix: string, depth: number) => {
    for (const [key, field] of Object.entries(props)) {
      if (field.prompt_role === "prose") continue // narrative guidance, not data
      const path = prefix ? `${prefix}.${key}` : key
      out.push({ path, field, depth })
      if (field.type === "object" && field.properties) {
        walk(field.properties, path, depth + 1)
      }
    }
  }
  walk(section.properties, section.section_key, 0)
  return out
}

export type MatrixColumn = { key: string; title: string; field: IbvField }
export type MatrixGroupRow = { path: string; rowLabel: string }
export type MatrixGroup = {
  label: string
  icd10: string
  path: string
  rows: MatrixGroupRow[]
}
export type SectionMatrix = {
  rowHeader: string
  showGroupColumn: boolean
  hasIcd: boolean
  rowLabelHeader: string
  columns: MatrixColumn[]
  groupColumns: MatrixColumn[]
  groups: MatrixGroup[]
}

/** Strip a leading "<Group> — " prefix so row labels stay short. */
function shortLabel(title: string): string {
  const parts = title.split(/\s[—–-]\s/)
  return parts[parts.length - 1].trim()
}

/** Object-typed child entries of a field (i.e. its nested sub-objects). */
function objectEntries(field: IbvField): [string, IbvField][] {
  return Object.entries(field.properties ?? {}).filter(
    ([, f]) => f.type === "object" && f.properties
  )
}

/** True if every child field is a leaf (no further nesting) — a real CPT row. */
function allLeafChildren(field: IbvField): boolean {
  const children = Object.values(field.properties ?? {})
  return children.length > 0 && children.every((c) => c.type !== "object")
}

/**
 * If a section is a grouped CPT matrix — ≥1 group object, each containing ≥1
 * CPT-row sub-object whose children are all leaves, all rows sharing the same
 * coverage columns, and ≥2 rows in total — return its table model, else null.
 */
export function getSectionMatrix(section: IbvSection): SectionMatrix | null {
  const groups = Object.entries(section.properties).filter(
    ([, f]) => f.type === "object" && f.properties
  )
  if (groups.length === 0) return null

  const firstRows = objectEntries(groups[0][1])
  if (firstRows.length === 0) return null

  // NOTE: column-key comparison is insertion-order sensitive (Object.keys order).
  // Safe against the current schema (all groups share column order); if a future
  // schema reorders columns within a group, switch to a sorted/Set comparison.
  const colKeys = Object.keys(firstRows[0][1].properties ?? {}).join("|")
  if (colKeys === "") return null

  let totalRows = 0
  for (const [, g] of groups) {
    const rows = objectEntries(g)
    if (rows.length === 0) return null
    for (const [, r] of rows) {
      if (!allLeafChildren(r)) return null
      if (Object.keys(r.properties ?? {}).join("|") !== colKeys) return null
    }
    totalRows += rows.length
  }
  if (totalRows < 2) return null

  const columns: MatrixColumn[] = Object.entries(
    firstRows[0][1].properties ?? {}
  ).map(([key, field]) => ({ key, title: field.title, field }))

  const groupColumns: MatrixColumn[] = Object.entries(groups[0][1].properties ?? {})
    .filter(([, f]) => f.type !== "object" && f.prompt_role !== "prose")
    .map(([key, field]) => ({ key, title: field.title, field }))

  const groupsOut: MatrixGroup[] = groups.map(([gKey, g]) => ({
    label: shortLabel(g.title),
    icd10: g.icd10 ?? "",
    path: `${section.section_key}.${gKey}`,
    rows: objectEntries(g).map(([rKey, r]) => ({
      path: `${section.section_key}.${gKey}.${rKey}`,
      rowLabel: r.title,
    })),
  }))

  const hasIcd = groupsOut.some((g) => g.icd10)
  const rowLabelHeader = hasIcd ? "CPT Code" : ""
  const showGroupColumn = groups.length > 1 || hasIcd
  const rowHeader =
    section.row_header ||
    (showGroupColumn
      ? groups[0][1].title.split(/\s[—–-]\s/)[0].trim()
      : groupsOut[0].label) ||
    section.title

  return {
    rowHeader,
    showGroupColumn,
    hasIcd,
    rowLabelHeader,
    columns,
    groupColumns,
    groups: groupsOut,
  }
}

// The schema is a static import that never changes at runtime, so these
// whole-schema derivations are memoized once (same pattern as `scopeMap` in
// validation.ts). They run several times per keystroke — via validateAll and
// the per-person completion map — so recomputing them each call is wasteful.
// Callers must treat the returned arrays as read-only (they share the cache).
let _leafFields: FlatField[] | null = null
let _requiredPaths: string[] | null = null

/** All leaf (non-group) fields across the whole schema. */
export function allLeafFields(): FlatField[] {
  if (!_leafFields) {
    _leafFields = schema.sections.flatMap((s) =>
      flattenSection(s).filter((f) => f.field.type !== "object")
    )
  }
  return _leafFields
}

/** Required leaf field paths — used for completion %. */
export function requiredPaths(): string[] {
  if (!_requiredPaths) {
    _requiredPaths = allLeafFields()
      .filter((f) => f.field.required_state === "required")
      .map((f) => f.path)
  }
  return _requiredPaths
}

/** 0–100 completion based on filled required fields. */
export function completionPercent(values: FormValues): number {
  const req = requiredPaths()
  if (req.length === 0) return 100
  const filled = req.filter((p) => (values[p] ?? "").trim() !== "").length
  return Math.round((filled / req.length) * 100)
}

/**
 * Sections shown in the right reference rail (teal box, green headers) — exactly
 * Hospital / Provider Reference / Insurance Reference, matching the reference.
 * Everything else renders full-width in the main (left) column.
 */
const RAIL_SECTIONS = new Set([
  "hospital_information",
  "provider_reference_information",
  "insurance_representative",
])

export type SectionPlacement = "rail" | "main"

/** Where a section renders: the right reference rail or the main column. */
export function sectionPlacement(sectionKey: string): SectionPlacement {
  return RAIL_SECTIONS.has(sectionKey) ? "rail" : "main"
}

