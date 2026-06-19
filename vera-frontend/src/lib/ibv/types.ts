// Types for the IBV v1-parity schema (src/lib/ibv/ibv-schema.json). We model only
// the rendering subset. Bot-prompt metadata (prompt, verbatim_prompt, prompt_role,
// phase_order, policies) exists in the JSON but is ignored by the form UI — except
// field-level `rules`, which feed conditional-required validation.

export type FieldWidget = "text" | "textarea" | "radio"

export type RuleCondition = {
  comparison: string
  value: string
  field: string
}

export type FieldRule = {
  effect: string
  match?: string
  conditions?: RuleCondition[]
  summary?: string
}

export type IbvField = {
  type: "string" | "object"
  title: string
  description?: string
  ui?: { widget?: FieldWidget }
  required_state?: "required" | "optional"
  enum?: string[]
  constraint_ref?: string
  confirm_only?: boolean
  confirm_value?: unknown
  /** narrative guidance ("prose") is not a data field — not rendered */
  prompt_role?: string
  /** bot prompt; ignored by the UI */
  verbatim_prompt?: string
  /** ICD-10 reference code on a CPT matrix group */
  icd10?: string
  /** conditional-required rules (effect "make this required") */
  rules?: FieldRule[]
  // present when type === "object"
  properties?: Record<string, IbvField>
  required?: string[]
}

export type IbvSection = {
  section_key: string
  title: string
  description?: string
  properties: Record<string, IbvField>
  required?: string[]
  /** override for the matrix table's first-column header */
  row_header?: string
}

export type ConstraintDef = {
  category?: string
  values?: string[]
  description?: string
}

export type IbvSchema = {
  name: string
  constraint_library: Record<string, ConstraintDef>
  sections: IbvSection[]
}

/** A resolved leaf or group field with its full dotted path, ready to render. */
export type FlatField = {
  /** dotted path, e.g. "patient_information.patient_name" */
  path: string
  field: IbvField
  /** nesting depth (0 = direct section child) */
  depth: number
}

/** Form values for a single person, keyed by dotted field path. */
export type FormValues = Record<string, string>

export type InsuredPerson = {
  id: string
  name: string
  relationship: string
}
