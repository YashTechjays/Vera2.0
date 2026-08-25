import {
  allLeaves,
  createRequiredPaths,
  isApplicable,
  isRequired,
  isSatisfied,
  titleOf,
} from "./schema"
import { phonePaths, E164_RE } from "./phone"
import type { FlatLeaf, FormSchema, FormValues } from "./types"

/** Errors keyed by root-anchored field path (absent = valid). */
export type ValidationErrors = Record<string, string>

/** How loudly a field's error is drawn — see `invalidSeverity`. */
export type InvalidSeverity = "error" | "missing"

/** A required value never filled in reads calmer than one that is actually wrong (VR2-162). */
export function invalidSeverity(
  message: string | undefined,
  value: string
): InvalidSeverity | undefined {
  if (!message) return undefined
  return value.trim() === "" ? "missing" : "error"
}

const NUMERIC_TYPES = new Set(["currency", "percent", "integer"])

const CURRENCY_STRIP_RE = /[$,%\s]/g

/** Parse a transcribed money/percent string ("$1,500.50", "20%") to a number. */
function parseNumeric(value: string): number {
  return Number(value.replace(CURRENCY_STRIP_RE, ""))
}

function formatMoney(n: number): string {
  return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

/**
 * Cross-field money-triplet checks (`numeric_consistencies`): met/remaining must
 * not exceed total and met + remaining must equal total (±$0.01, compared in
 * whole cents). Mirrors vera_core.forms.consistency — keep semantics in sync.
 */
function numericConsistencyErrors(schema: FormSchema, values: FormValues): ValidationErrors {
  const errors: ValidationErrors = {}
  const parse = (path: string): number | undefined => {
    // Mirror backend parse_currency: strip symbols, treat an empty result
    // (symbol-only like "$") as absent — Number("") is 0 — and reject non-finite.
    const cleaned = (values[path] ?? "").replace(CURRENCY_STRIP_RE, "")
    if (cleaned === "") return undefined
    const n = Number(cleaned)
    return Number.isFinite(n) ? n : undefined
  }
  const title = (path: string): string => titleOf(schema, path)
  for (const rule of schema.numeric_consistencies ?? []) {
    const totalPath = `${rule.triplet}.total`
    const metPath = `${rule.triplet}.met_amount`
    const remainingPath = `${rule.triplet}.remaining`
    const total = parse(totalPath)
    const met = parse(metPath)
    const remaining = parse(remainingPath)

    const clauses: string[] = []
    const flagged = new Set<string>()
    if (total !== undefined && met !== undefined && met > total) {
      clauses.push(
        `${title(metPath)} (${formatMoney(met)}) exceeds ${title(totalPath)} (${formatMoney(total)})`
      )
      flagged.add(metPath).add(totalPath)
    }
    if (total !== undefined && remaining !== undefined && remaining > total) {
      clauses.push(
        `${title(remainingPath)} (${formatMoney(remaining)}) exceeds ${title(totalPath)} (${formatMoney(total)})`
      )
      flagged.add(remainingPath).add(totalPath)
    }
    if (
      clauses.length === 0 &&
      total !== undefined &&
      met !== undefined &&
      remaining !== undefined &&
      Math.abs(Math.round((met + remaining - total) * 100)) > 1
    ) {
      clauses.push(
        `${title(metPath)} (${formatMoney(met)}) plus ${title(remainingPath)} ` +
          `(${formatMoney(remaining)}) must equal ${title(totalPath)} (${formatMoney(total)})`
      )
      flagged.add(totalPath).add(metPath).add(remainingPath)
    }
    if (clauses.length > 0) {
      const message = clauses.join("; ")
      for (const path of flagged) errors[path] ??= message
    }
  }
  return errors
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

// An exact-digit-count pattern (^[0-9]{9}$ / ^\d{10}$) gets a message naming the count.
const DIGIT_COUNT_PATTERN_RE = /^\^(?:\[0-9\]|\\d)\{(\d+)\}\$$/

function patternMessage(title: string, pattern: string): string {
  const digits = DIGIT_COUNT_PATTERN_RE.exec(pattern)?.[1]
  return digits
    ? `Enter a valid ${title}. Please enter a ${digits}-digit ${title}.`
    : `${title} is invalid`
}

// Single-digit tokens accept 1-2 digits but are shown padded, so users type the padded shape.
const DISPLAY_DATE_TOKENS: Record<string, string> = { M: "MM", D: "DD" }

/** "M/D/YYYY" shown to users as "MM/DD/YYYY" — 1-digit entries still validate. */
function displayDateFormat(format: string): string {
  return format.replace(DATE_TOKEN_ONLY_RE, (token) => DISPLAY_DATE_TOKENS[token] ?? token)
}

/** Parse a value in the DSL date_format to a Date; null unless it round-trips exactly. */
function parseDateInFormat(value: string, format: string): Date | null {
  const tokens = format.match(DATE_TOKEN_ONLY_RE) ?? []
  const numbers = value.match(/\d+/g) ?? []
  if (tokens.length !== numbers.length) return null

  const parts: { year?: number; month?: number; day?: number } = {}
  tokens.forEach((token, i) => {
    const n = Number(numbers[i])
    if (token.startsWith("Y")) parts.year = n
    else if (token.startsWith("M")) parts.month = n
    else parts.day = n
  })

  const { year, month, day } = parts
  if (!year || !month || !day) return null
  const date = new Date(year, month - 1, day)
  const roundTrips =
    date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day
  return roundTrips ? date : null
}

/** Valid only when the value matches the format's shape and is a real calendar date. */
function isValidDateInFormat(value: string, format: string): boolean {
  // Shape alone lets "13/45/2026" through — the parse rejects non-calendar dates.
  return dateFormatRegex(format).test(value) && parseDateInFormat(value, format) !== null
}

/** VR2-206: the patient must be at least 18. Only fires on a parseable DOB. */
function underageError(schema: FormSchema, values: FormValues): ValidationErrors {
  const path = schema.system_fields?.["patient_dob"]
  if (!path) return {}
  const value = (values[path] ?? "").trim()
  if (value === "") return {}
  const leaf = allLeaves(schema).find((candidate) => candidate.path === path)
  const format = leaf?.field.validation?.date_format
  if (!format) return {}
  const dob = parseDateInFormat(value, format)
  if (!dob) return {}
  const cutoff = new Date()
  cutoff.setFullYear(cutoff.getFullYear() - 18)
  return dob > cutoff ? { [path]: "Patient must be 18 years of age or older." } : {}
}

/** Add cross-field errors without overwriting a leaf's own message. */
function mergeErrors(into: ValidationErrors, from: ValidationErrors): void {
  for (const [path, message] of Object.entries(from)) into[path] ??= message
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
    // A declared default counts as filled (completion/export assume it), and so does a
    // sibling answering this leaf's either/or — one reply satisfies the pair.
    const owed = isRequired(schema, f, values) && f.default === undefined
    if (owed && !isSatisfied(schema, leaf, values)) {
      return `${f.title} is required`
    }
    return undefined
  }
  if (isDeclaredLegal(leaf, value)) return undefined

  if (f.validation?.pattern && !new RegExp(f.validation.pattern).test(value)) {
    return patternMessage(f.title, f.validation.pattern)
  }

  // The dialed phone must be E.164 — the backend 422s on it at intake and dispute-resolve.
  if (phonePaths(schema).has(leaf.path) && !E164_RE.test(value)) {
    return `Enter a valid ${f.title} including the country code.`
  }

  const dateFormat = f.validation?.date_format
  if (dateFormat && f.type === "date" && !isValidDateInFormat(value, dateFormat)) {
    return `Enter ${f.title} in ${displayDateFormat(dateFormat)} format.`
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
  mergeErrors(errors, numericConsistencyErrors(schema, values))
  mergeErrors(errors, underageError(schema, values))
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
  mergeErrors(errors, underageError(schema, values))
  if (!includeRequired) return errors
  for (const leaf of missingCreateLeaves(schema, values)) {
    errors[leaf.path] = `${leaf.field.title} is required`
  }
  return errors
}

/** The create-required leaves still blank, in document order. */
export function missingCreateLeaves(schema: FormSchema, values: FormValues): FlatLeaf[] {
  const required = createRequiredPaths(schema)
  return allLeaves(schema).filter(
    (leaf) => required.has(leaf.path) && (values[leaf.path] ?? "").trim() === "",
  )
}
