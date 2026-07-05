import { allLeaves, isApplicable, isRequired } from "./schema"
import type { FlatLeaf, FormSchema, FormValues } from "./types"

/** Errors keyed by root-anchored field path (absent = valid). */
export type ValidationErrors = Record<string, string>

const NUMERIC_TYPES = new Set(["currency", "percent", "integer"])

/** Parse a transcribed money/percent string ("$1,500.50", "20%") to a number. */
function parseNumeric(value: string): number {
  return Number(value.replace(/[$,%\s]/g, ""))
}

/**
 * Values legal by declaration (spec §4.4): special_values plus the declared
 * default / inapplicable_value — they bypass pattern and range checks.
 */
function isDeclaredLegal(leaf: FlatLeaf, value: string): boolean {
  const f = leaf.field
  return (
    (f.special_values ?? []).includes(value) ||
    value === f.default ||
    value === f.inapplicable_value
  )
}

function validateLeaf(
  schema: FormSchema,
  leaf: FlatLeaf,
  values: FormValues
): string | undefined {
  const f = leaf.field
  const value = (values[leaf.path] ?? "").trim()

  if (value === "") {
    // A declared default counts as filled (completion/export assume it).
    if (isRequired(schema, f, values) && f.default === undefined) {
      return `${f.title} is required`
    }
    return undefined
  }
  if (isDeclaredLegal(leaf, value)) return undefined

  if (f.validation?.pattern && !new RegExp(f.validation.pattern).test(value)) {
    return `${f.title} is invalid`
  }

  const range = f.validation?.range
  if (range && NUMERIC_TYPES.has(f.type)) {
    const n = parseNumeric(value)
    if (Number.isNaN(n)) return `${f.title} must be a number`
    if (range.min !== undefined && n < range.min) {
      return range.max !== undefined
        ? `${f.title} must be between ${range.min} and ${range.max}`
        : `${f.title} must be at least ${range.min}`
    }
    if (range.max !== undefined && n > range.max) {
      return range.min !== undefined
        ? `${f.title} must be between ${range.min} and ${range.max}`
        : `${f.title} must be at most ${range.max}`
    }
  }
  return undefined
}

/**
 * Validate the whole form: requiredness (required ∧ applicable), pattern and
 * range checks. Inapplicable fields are never flagged.
 */
export function validateAll(schema: FormSchema, values: FormValues): ValidationErrors {
  const errors: ValidationErrors = {}
  for (const leaf of allLeaves(schema)) {
    if (!isApplicable(schema, leaf.gates, values)) continue
    const message = validateLeaf(schema, leaf, values)
    if (message) errors[leaf.path] = message
  }
  return errors
}

/** Validate only the fields belonging to one section. */
export function validateSection(
  schema: FormSchema,
  sectionKey: string,
  values: FormValues
): ValidationErrors {
  const prefix = `sections.${sectionKey}.`
  return Object.fromEntries(
    Object.entries(validateAll(schema, values)).filter(([p]) => p.startsWith(prefix))
  )
}
