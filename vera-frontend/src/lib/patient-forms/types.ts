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

/** Non-PHI ack returned by PUT /patient-forms/{id}/status. */
export type PatientFormStatusResult = {
  id: string
  status: PatientFormStatus
}

/** Where a field's current value came from (vera_core AnswerSource enum). */
export type FieldSource = "intake" | "ai_call" | "human"

/** Judge verdict attached to a field's provenance. The judge's evidence quote is NOT
 *  here — it is merged into `PatientFormField.evidence`, the one evidence surface. */
export type FieldJudge = { confidence: number | null; supported: boolean }

/** Which call attempt produced a field's current value and the judge verdict. */
export type FieldProvenance = {
  attempt: number
  mode: "full" | "retry"
  judge: FieldJudge | null
  /** False when the call that produced this value captured no rep call reference
   *  number — nothing ties it to a payer-side record. The value is kept and stays
   *  current; a reviewer may still accept it by hand. Shown, never hidden. */
  authoritative: boolean
}

/** One entry in the call-attempt timeline (GET /patient-forms/{id}/calls). */
export type CallAttempt = {
  id: string
  attempt: number
  mode: "full" | "retry"
  status: string
  created_at: string
  retry_of: string | null
  changed_paths: string[]
  /** True when THIS caller may fetch the attempt's recording (it is AVAILABLE,
   *  the call is owner-or-published visible, and the caller holds
   *  recordings:read) — false means render no play control. */
  recording_available: boolean
  /** Same meaning as `FieldProvenance.authoritative`, at the call level: false when
   *  this call captured no rep call reference number. */
  authoritative: boolean
}

/** JSONB field value — the backend stores/returns `Any`. */
export type FieldValue = string | number | boolean | null

/** A disputed field: the current AI-captured value diverges from the most recent
 *  intake/human baseline. `previous_value` is that baseline, `current_value` the
 *  AI value; `confidence` is the AI answer's own confidence, `reasoning` the optional
 *  judge explanation. The divergence only — evidence lives on the field, because an AI
 *  answer that AGREES with the baseline has no dispute yet still has evidence. */
export type FieldDispute = {
  previous_value: FieldValue
  current_value: FieldValue
  confidence: number | null
  reasoning: string | null
}

/** One extracted data point. `dispute` is null unless the value diverges from the
 *  baseline; `evidence` is the field's single evidence text (the judge's transcript
 *  quote, falling back to the extractor's capture) and is present regardless. */
export type PatientFormField = {
  field_path: string
  value: FieldValue
  source: FieldSource
  confidence: number | null
  evidence: string | null
  dispute: FieldDispute | null
  provenance: FieldProvenance | null
}

/** Worklist row (GET /patient-forms). */
export type PatientFormSummary = {
  id: string
  status: PatientFormStatus
  patient_name: string | null
  chart_number: string | null
  appointment_date: string | null
  /** Promoted columns lifted from the intake snapshot. */
  appointment_type: string | null
  member_id: string | null
  insurance_provider: string | null
  insurance_provider_phone_number: string | null
  completion_pct: number
  created_at: string
  updated_at: string
  review_reason: string | null
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
  /** The form's current insurance provider — the send-to-queue picker pre-selects
   *  the matching catalog provider from this string. */
  insurance_provider: string | null
  fields: PatientFormField[]
  /** Stored queue-time choice: run the IVR navigator on this form's calls. */
  ivr_navigation_enabled: boolean
}

/** Active insurance-provider option for the send-to-queue picker
 *  (GET /patient-forms/insurance-providers). Non-PHI catalog reference. */
export type ProviderOption = {
  id: string
  name: string
}

/** Paginated worklist envelope `data`. */
export type PaginatedPatientForms = {
  items: PatientFormSummary[]
  page: number
  page_size: number
  total: number
}

/** Server-side sort columns the worklist endpoint whitelists. */
export type PatientFormSortKey =
  | "appointment_date"
  | "appointment_type"
  | "patient_name"
  | "member_id"
  | "insurance_provider"
  | "status"
  | "created_at"

export type ListPatientFormsParams = {
  page?: number
  page_size?: number
  /** exact-match status filter */
  status?: PatientFormStatus
  /** case-insensitive substring match on patient_name */
  q?: string
  /** server-side sort; the backend defaults to created_at desc, nulls last */
  sort_by?: PatientFormSortKey
  sort_dir?: "asc" | "desc"
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

/** GET /schema-versions/{id} — the stored form-schema document a patient form
 *  is pinned to (via its `schema_version_id`). Not PHI: the form template. */
export type SchemaVersionDetail = {
  id: string
  schema_id: string
  version: number
  status: string
  insurance_type: string
  name: string
  /** the raw DSL document (schema_version.schema_json); parse with `parseSchema` */
  document: unknown
}

/** GET /patient-forms/schemas — a form family selectable for in-app intake
 *  (only families with a published version are returned). Catalog data, not PHI. */
export type IntakeSchemaOption = {
  schema_id: string
  name: string
  insurance_type: string
  published_version_id: string
  published_version: number
}

/** Non-PHI ack returned by POST /patient-forms:create. */
export type PatientFormCreateResult = {
  id: string
  status: PatientFormStatus
  insurance_type: string
  schema_version_id: string
  completion_pct: number
  created_at: string
}
