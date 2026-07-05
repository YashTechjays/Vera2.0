import { evaluateCondition } from "./conditions"
import type {
  Condition,
  Contradiction,
  Field,
  FlatLeaf,
  FormSchema,
  FormValues,
  GroupField,
  LeafField,
  Section,
} from "./types"

// The schema is NOT bundled: real forms fetch the exact document their
// `schema_version_id` pins via GET /schema-versions/{id} (IbvProvider), so the
// renderer and the backend can never disagree about the field set. Every helper
// here therefore takes the parsed document as a parameter. The demo/mock form
// uses a dev fixture copy (see mock.ts).

/** Parse + guard a fetched schema_version document. Throws on non-2.x docs. */
export function parseSchema(document: unknown): FormSchema {
  const schema = document as FormSchema
  if (typeof schema?.dsl_version !== "string" || !schema.dsl_version.startsWith("2.")) {
    throw new Error(
      `unsupported form-schema dsl_version ${schema?.dsl_version ?? "<missing>"}; expected 2.x`
    )
  }
  return schema
}

/** Sections in document order (object key order = UI order). */
export function sectionEntriesOf(schema: FormSchema): [string, Section][] {
  return Object.entries(schema.sections)
}

export function isGroup(f: Field): f is GroupField {
  return f.type === "group"
}

/** Append a node's own `applicable_when` (if any) to an inherited gate chain. */
function extendGates(gates: Condition[], node: { applicable_when?: Condition }): Condition[] {
  return node.applicable_when ? [...gates, node.applicable_when] : gates
}

/** A schema node (group or leaf) resolved to its path, depth and gate chain. */
export type FlatRow = {
  path: string
  key: string
  field: Field
  /** nesting depth (0 = direct section child) */
  depth: number
  /** every applicable_when from the section down to this node */
  gates: Condition[]
}

/**
 * Flatten a section into ordered render rows (groups emitted before their
 * children). Paths are root-anchored: `sections.<section_key>.<field>...`.
 */
export function flattenSection(sectionKey: string, section: Section): FlatRow[] {
  const out: FlatRow[] = []
  const sectionGates = section.applicable_when ? [section.applicable_when] : []
  const walk = (
    fields: Record<string, Field>,
    prefix: string,
    depth: number,
    gates: Condition[]
  ) => {
    for (const [key, field] of Object.entries(fields)) {
      const path = `${prefix}.${key}`
      const own = extendGates(gates, field)
      out.push({ path, key, field, depth, gates: own })
      if (isGroup(field)) walk(field.fields, path, depth + 1, own)
    }
  }
  walk(section.fields, `sections.${sectionKey}`, 0, sectionGates)
  return out
}

// Whole-schema derivations are memoized per document (schemas are immutable per
// version). Callers must treat the returned array as read-only (it is shared).
const _leavesBySchema = new WeakMap<FormSchema, FlatLeaf[]>()

/** Every leaf field across the schema, with root-anchored paths and gates. */
export function allLeaves(schema: FormSchema): FlatLeaf[] {
  let leaves = _leavesBySchema.get(schema)
  if (!leaves) {
    leaves = sectionEntriesOf(schema).flatMap(([sectionKey, section]) =>
      flattenSection(sectionKey, section)
        .filter((r) => !isGroup(r.field))
        .map((r) => ({ ...r, field: r.field as LeafField, sectionKey }))
    )
    _leavesBySchema.set(schema, leaves)
  }
  return leaves
}

/** True when every applicable_when in the chain holds against current values. */
export function isApplicable(
  schema: FormSchema,
  gates: Condition[],
  values: FormValues
): boolean {
  return gates.every((g) => evaluateCondition(g, values, schema.shared_conditions))
}

/** Resolve `required: true | {when}` against current values. */
export function isRequired(
  schema: FormSchema,
  field: LeafField,
  values: FormValues
): boolean {
  const req = field.required
  if (req === undefined || typeof req === "boolean") return req ?? false
  return evaluateCondition(req.when, values, schema.shared_conditions)
}

/** Select options for an enum field: `values` plus verbatim-legal extras. */
export function optionsOf(field: LeafField): string[] {
  if (field.type !== "enum") return []
  return [...(field.values ?? []), ...(field.special_values ?? [])]
}

/** Combobox suggestions for a non-enum field (e.g. plan_type's PPO/HMO/…). */
export function suggestionsOf(field: LeafField): string[] {
  return field.type === "enum" ? [] : field.special_values ?? []
}

/**
 * How a leaf participates in the voice call — drives the UI color coding:
 * - `system`: bound in `system_fields` — the platform itself reads/writes it
 *   (worklists, integrations, call setup); takes precedence over the role.
 * - `context`: fed to the voice agent as known background (role context).
 * - `noop`: UI-only — never in the prompt, never asked (role input/readonly).
 * - `asked`: collected on the call (role ask/confirm).
 */
export type FieldUsage = "system" | "context" | "noop" | "asked"

const _systemPathsBySchema = new WeakMap<FormSchema, Set<string>>()

/** The field paths bound to well-known system handles (`system_fields`). */
export function systemFieldPaths(schema: FormSchema): Set<string> {
  let paths = _systemPathsBySchema.get(schema)
  if (!paths) {
    paths = new Set(Object.values(schema.system_fields ?? {}))
    _systemPathsBySchema.set(schema, paths)
  }
  return paths
}

export function fieldUsageOf(
  schema: FormSchema,
  path: string,
  field: LeafField
): FieldUsage {
  if (systemFieldPaths(schema).has(path)) return "system"
  // A ui_only SECTION is never voice-touched, whatever its leaves' roles say.
  if (schema.sections[path.split(".")[1]]?.role === "ui_only") return "noop"
  if (field.role === "context") return "context"
  if (field.role === "input" || field.role === "readonly") return "noop"
  return "asked"
}

/** A field counts as filled when it has a value or a declared default. */
function isFilled(leaf: FlatLeaf, values: FormValues): boolean {
  return (values[leaf.path] ?? "").trim() !== "" || leaf.field.default !== undefined
}

/** 0–100 completion over required ∧ applicable leaves (defaults count filled). */
export function completionPercent(schema: FormSchema, values: FormValues): number {
  const relevant = allLeaves(schema).filter(
    (l) => isApplicable(schema, l.gates, values) && isRequired(schema, l.field, values)
  )
  if (relevant.length === 0) return 100
  const filled = relevant.filter((l) => isFilled(l, values)).length
  return Math.round((filled / relevant.length) * 100)
}

/**
 * Contradiction rules whose `when` currently holds — shown as a warning banner.
 * Conditions compare recorded answers, so a rule stays dormant until every
 * referenced field has a value (missing values compare as "").
 */
export function contradictionWarnings(
  schema: FormSchema,
  values: FormValues
): Contradiction[] {
  return (schema.contradictions ?? []).filter((c) =>
    evaluateCondition(c.when, values, schema.shared_conditions)
  )
}

// --- ui.layout: "table" — group-per-row matrix model ------------------------

export type TableCell = { path: string; field: LeafField; gates: Condition[] }
export type TableColumn = { key: string; title: string }
export type TableRow = {
  path: string
  label: string
  cells: Record<string, TableCell | undefined>
}
export type TableGroup = {
  path: string
  label: string
  icd10: string
  /** the group's own gate chain (section + group applicable_when) */
  gates: Condition[]
  rows: TableRow[]
  /** group-level rowspan cells (e.g. cycle_limit, additional_notes) */
  extras: Record<string, TableCell | undefined>
}
export type SectionTable = {
  columns: TableColumn[]
  extraColumns: TableColumn[]
  hasIcd: boolean
  groups: TableGroup[]
  /** section-level leaves, rendered as plain field rows above the table */
  leaves: FlatRow[]
}

function leafEntries(g: GroupField): [string, LeafField][] {
  return Object.entries(g.fields).filter(([, f]) => !isGroup(f)) as [string, LeafField][]
}
function groupEntries(g: GroupField | Section): [string, GroupField][] {
  return Object.entries(g.fields).filter(([, f]) => isGroup(f)) as [string, GroupField][]
}

/**
 * Build the matrix model for a `ui.layout: "table"` section (spec §5) — the
 * layout hint alone decides; there is no structural guessing.
 *
 * Top-level groups are the table's bands; their group children (per-CPT groups)
 * are rows and their leaf children (cycle_limit, additional_notes) are per-group
 * rowspan "extras". A group with no subgroups (ovulation_induction) is itself
 * one row, its leaves split between row cells and extras by key.
 */
export function getSectionTable(
  sectionKey: string,
  section: Section
): SectionTable | null {
  if (section.ui?.layout !== "table") return null

  const rows = flattenSection(sectionKey, section)
  const leaves = rows.filter((r) => r.depth === 0 && !isGroup(r.field))
  const topGroups = rows.filter(
    (r): r is FlatRow & { field: GroupField } => r.depth === 0 && isGroup(r.field)
  )

  // Extra columns = leaf keys sitting beside subgroups inside any group
  // (Map insertion order = first-seen order = column order).
  const extraTitles = new Map<string, string>()
  for (const g of topGroups) {
    if (groupEntries(g.field).length === 0) continue
    for (const [key, f] of leafEntries(g.field)) {
      if (!extraTitles.has(key)) extraTitles.set(key, f.title)
    }
  }

  const cell = (
    parentPath: string,
    parentGates: Condition[],
    key: string,
    field: LeafField
  ): TableCell => ({
    path: `${parentPath}.${key}`,
    field,
    gates: extendGates(parentGates, field),
  })

  const columnTitles = new Map<string, string>()
  const groups: TableGroup[] = topGroups.map((g) => {
    const subgroups = groupEntries(g.field)
    const extras: Record<string, TableCell | undefined> = {}
    const rowsOut: TableRow[] = []

    const collectColumns = (entries: [string, LeafField][]) => {
      for (const [key, f] of entries) {
        if (!columnTitles.has(key)) columnTitles.set(key, f.title)
      }
    }

    if (subgroups.length > 0) {
      for (const [key, f] of leafEntries(g.field)) {
        extras[key] = cell(g.path, g.gates, key, f)
      }
      for (const [rowKey, row] of subgroups) {
        const rowPath = `${g.path}.${rowKey}`
        const rowGates = extendGates(g.gates, row)
        const entries = leafEntries(row)
        collectColumns(entries)
        rowsOut.push({
          path: rowPath,
          label: row.title,
          cells: Object.fromEntries(
            entries.map(([key, f]) => [key, cell(rowPath, rowGates, key, f)])
          ),
        })
      }
    } else {
      // Leaf-only group: the group itself is the row; extras split out by key.
      const entries = leafEntries(g.field)
      const rowEntries = entries.filter(([key]) => !extraTitles.has(key))
      collectColumns(rowEntries)
      for (const [key, f] of entries) {
        if (extraTitles.has(key)) extras[key] = cell(g.path, g.gates, key, f)
      }
      rowsOut.push({
        path: g.path,
        label: g.field.codes?.cpt?.join(", ") ?? "—",
        cells: Object.fromEntries(
          rowEntries.map(([key, f]) => [key, cell(g.path, g.gates, key, f)])
        ),
      })
    }

    return {
      path: g.path,
      label: g.field.title,
      icd10: g.field.codes?.icd10?.join(", ") ?? "",
      gates: g.gates,
      rows: rowsOut,
      extras,
    }
  })

  return {
    columns: [...columnTitles].map(([key, title]) => ({ key, title })),
    extraColumns: [...extraTitles].map(([key, title]) => ({ key, title })),
    hasIcd: groups.some((g) => g.icd10 !== ""),
    groups,
    leaves,
  }
}
