import {
  allLeaves,
  isApplicable,
  isRequired,
  leafByPath,
  systemFieldPaths,
} from "./schema"
import type { FlatLeaf, FormSchema, FormValues } from "./types"

/** Errors keyed by root-anchored field path (absent = valid). */
export type ValidationErrors = Record<string, string>

const NUMERIC_TYPES = new Set(["currency", "percent", "integer"])

/** Parse a transcribed money/percent string ("$1,500.50", "20%") to a number. */
function parseNumeric(value: string): number {
  return Number(value.replace(/[$,%\s]/g, ""))
}

// The DSL date_format token vocabulary (M/D allow 1-2 digits; MM/DD demand 2).
const DATE_TOKEN_RE: Record<string, string> = {
  YYYY: "\\d{4}",
  YY: "\\d{2}",
  MM: "\\d{2}",
  M: "\\d{1,2}",
  DD: "\\d{2}",
  D: "\\d{1,2}",
}

// Longest-first alternation over the token vocabulary (key order above).
const DATE_TOKEN_ALTERNATION = Object.keys(DATE_TOKEN_RE).join("|")
const DATE_TOKEN_ONLY_RE = new RegExp(DATE_TOKEN_ALTERNATION, "g")
const DATE_TOKEN_OR_CHAR_RE = new RegExp(`${DATE_TOKEN_ALTERNATION}|.`, "g")

const ISO_DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/

/**
 * Reformat an ISO date ("1982-02-23") into the DSL date_format ("M/D/YYYY" →
 * "2/23/1982"). Machine intake stores dates as ISO while the review UI displays,
 * validates, and submits the schema's declared format — convert on the way in.
 * Anything that isn't a bare ISO date passes through unchanged.
 */
export function isoToDateFormat(value: string, format: string): string {
  const match = ISO_DATE_RE.exec(value.trim())
  if (!match) return value
  const [, year, month, day] = match
  const part: Record<string, string> = {
    YYYY: year,
    YY: year.slice(2),
    MM: month,
    M: String(Number(month)),
    DD: day,
    D: String(Number(day)),
  }
  return format.replace(DATE_TOKEN_ONLY_RE, (token) => part[token])
}

/** Compile a DSL date_format ("M/D/YYYY") into an anchored value regex. */
function dateFormatRegex(format: string): RegExp {
  const source = format.replace(
    DATE_TOKEN_OR_CHAR_RE,
    (token) => DATE_TOKEN_RE[token] ?? token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
  )
  return new RegExp(`^${source}$`)
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

  const dateFormat = f.validation?.date_format
  if (dateFormat && f.type === "date" && !dateFormatRegex(dateFormat).test(value)) {
    return `${f.title} must match ${dateFormat}`
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

/**
 * Create-mode validation (new patient form): requiredness comes from the
 * schema's `system_fields` block — the backend's `required_intake_fields` rule
 * (targets without a declared default), NOT a leaf's own `required`, which
 * governs voice collection. Filled fields still get the pattern / date-format /
 * range checks. Backend parity: `missing_required` ignores applicability for
 * system fields, so the required pass here does too. `includeRequired: false`
 * yields format errors only (shown live before the first submit attempt).
 */
export function validateCreate(
  schema: FormSchema,
  values: FormValues,
  { includeRequired = true }: { includeRequired?: boolean } = {},
): ValidationErrors {
  const errors: ValidationErrors = {}
  for (const leaf of allLeaves(schema)) {
    if (!isApplicable(schema, leaf.gates, values)) continue
    if ((values[leaf.path] ?? "").trim() === "") continue
    const message = validateLeaf(schema, leaf, values)
    if (message) errors[leaf.path] = message
  }
  if (!includeRequired) return errors
  const byPath = leafByPath(schema)
  for (const path of systemFieldPaths(schema)) {
    const leaf = byPath.get(path)
    if (!leaf || leaf.field.default !== undefined) continue
    if ((values[path] ?? "").trim() === "") {
      errors[path] = `${leaf.field.title} is required`
    }
  }
  return errors
}
