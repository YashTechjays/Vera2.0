import { z } from "zod"

import { allLeafFields, schema } from "./schema"
import type { FieldRule, FormValues, IbvField } from "./types"

/** Errors keyed by dotted field path (absent = valid). */
export type ValidationErrors = Record<string, string>

/** A field is conditionally required if a "make this required" rule matches. */
function conditionallyRequired(field: IbvField, values: FormValues): boolean {
  const rules: FieldRule[] = field.rules ?? []
  for (const rule of rules) {
    if (!/required/i.test(rule.effect)) continue
    const conds = rule.conditions ?? []
    if (conds.length === 0) continue
    const allMatch = conds.every(
      (c) => values[conditionScope(c.field)] === c.value
    )
    if (allMatch) return true
  }
  return false
}

let scopeMap: Map<string, string> | null = null
/**
 * Resolve a rule's bare field key (e.g. "coverage_type") to its dotted path.
 * Built once from the schema's leaves; first match wins (rule keys in this
 * schema are unique by last segment — see NOTE below).
 */
function conditionScope(fieldKey: string): string {
  if (!scopeMap) {
    scopeMap = new Map()
    for (const f of allLeafFields()) {
      const last = f.path.includes(".") ? f.path.slice(f.path.lastIndexOf(".") + 1) : f.path
      // first match wins — do not overwrite if a later leaf shares the last segment
      if (!scopeMap.has(last)) scopeMap.set(last, f.path)
    }
  }
  return scopeMap.get(fieldKey) ?? fieldKey
}
// NOTE: this maps by last path segment; safe because every rule-referenced key
// in the current schema is unique by last segment. If a future rule references
// a shared segment (e.g. "npi", "covered"), resolution would pick the first leaf.

/**
 * Validate the whole form. Required fields (static `required_state` or matched
 * conditional rule) must be non-empty strings. Returns a path→message map.
 */
export function validateAll(values: FormValues): ValidationErrors {
  const shape: Record<string, z.ZodTypeAny> = {}
  const leaves = allLeafFields()

  for (const { path, field } of leaves) {
    const required =
      field.required_state === "required" ||
      conditionallyRequired(field, values)
    shape[path] = required
      ? z.string().trim().min(1, { message: `${field.title} is required` })
      : z.string().optional()
  }

  // zod treats dotted keys as flat keys here (we pass a flat object), so build a
  // record schema and validate the flat values object directly.
  const result = z.object(shape).safeParse(
    Object.fromEntries(leaves.map(({ path }) => [path, values[path] ?? ""]))
  )

  const errors: ValidationErrors = {}
  if (!result.success) {
    for (const issue of result.error.issues) {
      const key = String(issue.path[0])
      if (key && !(key in errors)) errors[key] = issue.message
    }
  }
  return errors
}

/** Validate only the required fields belonging to one section. */
export function validateSection(
  sectionKey: string,
  values: FormValues
): ValidationErrors {
  const all = validateAll(values)
  const prefix = `${sectionKey}.`
  return Object.fromEntries(
    Object.entries(all).filter(([p]) => p.startsWith(prefix))
  )
}

export { schema as validationSchema }
