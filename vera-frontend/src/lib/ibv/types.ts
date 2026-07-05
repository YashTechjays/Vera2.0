// Typed model of the form-schema DSL v2.1 (spec §4:
// docs/superpowers/specs/2026-07-02-form-schema-dsl-v2-design.md). We model the
// UI-rendering subset; voice-only constructs (`prompt`, `ask_groups`, `tasks`,
// `flow_rules`, `derive`, `confirm_in_task`, `tags`, `codes.speak_cpt`) exist in
// the JSON but are intentionally absent here — the cast in schema.ts drops them.

export type ConditionOp = "eq" | "ne" | "in" | "not_in"

/** `value` is a string for eq/ne and a string[] for in/not_in. */
export type FieldCondition = {
  field: string
  op: ConditionOp
  value: string | string[]
}

export type Condition =
  | FieldCondition
  | { all: Condition[] }
  | { any: Condition[] }
  | { not: Condition }
  /** resolves into the document's `shared_conditions` */
  | { ref: string }

export type LeafType =
  | "text"
  | "enum"
  | "date"
  | "currency"
  | "percent"
  | "integer"
  | "phone"

/** Explicit on every compiled leaf. Only `readonly` is display-only in the UI. */
export type FieldRole = "ask" | "confirm" | "context" | "readonly" | "input"

/** collect (default) | context | ui_only — all editable in the UI. */
export type SectionRole = "collect" | "context" | "ui_only"

export type Requirement = boolean | { when: Condition }

export type Codes = { cpt?: string[]; icd10?: string[]; speak_cpt?: boolean }

export type Validation = {
  /** regex for text-family values (NPI, tax ID) */
  pattern?: string
  /** numeric bounds for currency/percent/integer */
  range?: { min?: number; max?: number }
  /** entry/display format for date fields (e.g. "M/D/YYYY") */
  date_format?: string
}

export type LeafField = {
  type: LeafType
  title: string
  role: FieldRole
  /** default false */
  required?: Requirement
  /** enum only — the option vocabulary */
  values?: string[]
  /** extra verbatim-legal answers; on text fields also the combobox suggestions */
  special_values?: string[]
  /** value the form assumes when nothing was recorded (shown as placeholder) */
  default?: string
  validation?: Validation
  applicable_when?: Condition
  /** what a field skipped as inapplicable displays */
  inapplicable_value?: string
  codes?: Codes
  ui?: { widget?: "textarea" }
  description?: string
}

export type GroupField = {
  type: "group"
  title: string
  fields: Record<string, Field>
  applicable_when?: Condition
  /** completion semantics: all (default) | any */
  integrity?: "all" | "any"
  codes?: Codes
  description?: string
}

export type Field = LeafField | GroupField

/** Either/or sets — optional UI nicety (badge members); members are full paths. */
export type Alternative = { members: string[]; ask?: string }

export type Section = {
  title: string
  /** default collect */
  role?: SectionRole
  description?: string
  applicable_when?: Condition
  codes?: Codes
  alternatives?: Alternative[]
  ui?: { layout?: "table" }
  fields: Record<string, Field>
}

/** Cross-field consistency rule; the UI shows a warning banner while `when` holds. */
export type Contradiction = {
  rule_key: string
  when: Condition
  fields: string[]
  reason: string
  clarify?: string
}

export type FormSchema = {
  dsl_version: string
  name: string
  insurance_type: string
  description?: string
  /** well-known system handles → field paths */
  system_fields?: Record<string, string>
  shared_conditions?: Record<string, Condition>
  /** object keyed by section_key; key order = UI order */
  sections: Record<string, Section>
  contradictions?: Contradiction[]
}

/**
 * Form values keyed by root-anchored field path
 * (`sections.<section_key>.<field>...` — identical to `field_answer.field_path`).
 */
export type FormValues = Record<string, string>

/** A leaf with its full path and applicability gate chain (own + ancestors). */
export type FlatLeaf = {
  path: string
  sectionKey: string
  field: LeafField
  /** nesting depth (0 = direct section child) */
  depth: number
  /** every applicable_when from the section down to the leaf itself */
  gates: Condition[]
}

export type InsuredPerson = {
  id: string
  name: string
  relationship: string
}
