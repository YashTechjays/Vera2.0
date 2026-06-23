// DTOs for the patient-forms API. These mirror the backend response shapes
// EXACTLY (snake_case), so they map 1:1 onto the JSON the control-plane returns.
// The list/detail/resolve endpoints all ride the standard response envelope,
// which `apiRequest` unwraps — so these types describe the unwrapped `data`.

/** Workflow status of a patient form (vera_core FormStatus enum). */
export type PatientFormStatus =
  | "ready_for_processing"
  | "in_queue"
  | "in_call"
  | "ai_processing"
  | "exception_review"
  | "completed"
  | "call_failed"

/** Where a field's current value came from (vera_core AnswerSource enum). */
export type FieldSource = "intake" | "ivr" | "ai_call" | "human"

/** JSONB field value — the backend stores/returns `Any`. */
export type FieldValue = string | number | boolean | null

/** A field flagged by the LLM judge: the captured value disagrees with a prior.
 *  `previous_value`/`current_value` are JSONB (`Any`); `evidence` is what was
 *  captured, `reasoning` is why the judge disputes it (both nullable). */
export type FieldDispute = {
  previous_value: FieldValue
  current_value: FieldValue
  confidence: number | null
  evidence: string | null
  reasoning: string | null
}

/** One extracted data point. `dispute` is null unless the judge flagged it. */
export type PatientFormField = {
  field_path: string
  value: FieldValue
  source: FieldSource
  confidence: number | null
  dispute: FieldDispute | null
}

/** Worklist row (GET /patient-forms). */
export type PatientFormSummary = {
  id: string
  status: PatientFormStatus
  patient_name: string | null
  chart_number: string | null
  appointment_date: string | null
  completion_pct: number
  dispute_count: number
  created_at: string
  updated_at: string
}

/** Full review payload (GET /patient-forms/{id} and the resolve response). */
export type PatientFormDetail = {
  id: string
  status: PatientFormStatus
  insurance_type: string
  schema_version_id: string
  completion_pct: number
  created_at: string
  updated_at: string
  patient_name: string | null
  chart_number: string | null
  appointment_date: string | null
  member_id: string | null
  fields: PatientFormField[]
}

/** Paginated worklist envelope `data`. */
export type PaginatedPatientForms = {
  items: PatientFormSummary[]
  page: number
  page_size: number
  total: number
}

export type ListPatientFormsParams = {
  page?: number
  page_size?: number
  /** exact-match status filter */
  status?: PatientFormStatus
  /** case-insensitive substring match on patient_name */
  q?: string
}

/** Request body for POST /patient-forms/{id}/disputes:resolve. Mirrors the
 *  reviewer's SavePayload: edited values keyed by dotted path, the disputes the
 *  reviewer accepted, and the fields they corrected + re-queued for a re-ask. */
export type ResolveDisputesPayload = {
  /** edited field values, keyed by dotted field path */
  form_data: Record<string, string>
  /** dotted paths whose dispute the reviewer accepted (dispute cleared) */
  dispute_fields: string[]
  /** dotted paths the reviewer corrected and re-queued for a re-ask */
  reasked_fields: string[]
}
