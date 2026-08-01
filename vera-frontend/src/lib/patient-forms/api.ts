// Typed wrappers over the patient-forms endpoints, mirroring the backend
// contract. Each call rides `apiRequest`, which injects the bearer token,
// unwraps the response envelope, and throws `ApiError` on failure.

import { apiRequest, apiRequestBlob } from "@/lib/api/client"
import type {
  CallAttempt,
  IntakeSchemaOption,
  ListPatientFormsParams,
  PaginatedPatientForms,
  PatientFormCreateResult,
  PatientFormDetail,
  PatientFormStatus,
  PatientFormStatusResult,
  ProviderOption,
  ResolveDisputesPayload,
  SchemaVersionDetail,
} from "./types"

/** GET /patient-forms — paginated worklist. */
export function listPatientForms(
  params: ListPatientFormsParams = {},
): Promise<PaginatedPatientForms> {
  const { page = 1, page_size = 20, status, q, sort_by, sort_dir } = params
  const qs = new URLSearchParams({
    page: String(page),
    page_size: String(page_size),
  })
  if (status) qs.set("status", status)
  if (q) qs.set("q", q)
  if (sort_by) qs.set("sort_by", sort_by)
  if (sort_dir) qs.set("sort_dir", sort_dir)
  return apiRequest<PaginatedPatientForms>(`/patient-forms?${qs}`)
}

/** GET /patient-forms/{id} — full review detail. */
export function getPatientForm(formId: string): Promise<PatientFormDetail> {
  return apiRequest<PatientFormDetail>(
    `/patient-forms/${encodeURIComponent(formId)}`,
  )
}

/** GET /schema-versions/{id} — the schema document a form is pinned to. */
export function getSchemaVersion(versionId: string): Promise<SchemaVersionDetail> {
  return apiRequest<SchemaVersionDetail>(
    `/schema-versions/${encodeURIComponent(versionId)}`,
  )
}

/** POST /patient-forms/{id}/disputes:resolve — returns the refreshed detail. */
export function resolveDisputes(
  formId: string,
  payload: ResolveDisputesPayload,
): Promise<PatientFormDetail> {
  return apiRequest<PatientFormDetail>(
    `/patient-forms/${encodeURIComponent(formId)}/disputes:resolve`,
    { method: "POST", body: payload },
  )
}

/** GET /patient-forms/insurance-providers — active providers for the send-to-queue
 *  picker (non-PHI catalog reference data). */
export function listInsuranceProviders(): Promise<ProviderOption[]> {
  return apiRequest<ProviderOption[]>(`/patient-forms/insurance-providers`)
}

/** PUT /patient-forms/{id}/status — change lifecycle status (status only).
 *  Rejects illegal transitions (422) and completing with open disputes (409).
 *  `enableIvrNavigation` and `insuranceProviderId` ride only with an in_queue
 *  change: the toggle picks the navigator (voice-lab-style; omitted → the backend
 *  keeps the form's stored choice), and the provider id canonicalizes the form's
 *  insurance_provider so dispatch resolves the right playbook. */
export function updatePatientFormStatus(
  formId: string,
  status: PatientFormStatus,
  opts?: { enableIvrNavigation?: boolean; insuranceProviderId?: string },
): Promise<PatientFormStatusResult> {
  return apiRequest<PatientFormStatusResult>(
    `/patient-forms/${encodeURIComponent(formId)}/status`,
    {
      method: "PUT",
      body: {
        status,
        ...(opts?.enableIvrNavigation !== undefined
          ? { enable_ivr_navigation: opts.enableIvrNavigation }
          : {}),
        ...(opts?.insuranceProviderId
          ? { insurance_provider_id: opts.insuranceProviderId }
          : {}),
      },
    },
  )
}

/** GET /patient-forms/{id}/calls — the attempt timeline. */
export function getPatientFormCalls(formId: string): Promise<CallAttempt[]> {
  return apiRequest<CallAttempt[]>(
    `/patient-forms/${encodeURIComponent(formId)}/calls`,
  )
}

/** POST /patient-forms/{id}/export — streamed XLSX (forms:export). */
export function exportPatientForm(formId: string): Promise<Blob> {
  return apiRequestBlob(`/patient-forms/${encodeURIComponent(formId)}/export`, {
    method: "POST",
  })
}

/** GET /patient-forms/schemas — form families with a published version. */
export function listIntakeSchemas(): Promise<IntakeSchemaOption[]> {
  return apiRequest<IntakeSchemaOption[]>("/patient-forms/schemas")
}

/** POST /patient-forms:create — create a patient form from a family's published
 *  schema version. The server resolves the version; `publishedVersionId` is the one
 *  this client rendered, sent so a version published mid-flow 409s instead of
 *  silently binding a document the user never filled. `idempotencyKey` must be
 *  stable across retries of one submit — a fresh key per call de-dups nothing. */
export function createPatientForm(
  schemaId: string,
  publishedVersionId: string,
  intakePayload: Record<string, unknown>,
  idempotencyKey: string,
): Promise<PatientFormCreateResult> {
  return apiRequest<PatientFormCreateResult>("/patient-forms:create", {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: {
      schema_id: schemaId,
      published_version_id: publishedVersionId,
      intake_payload: intakePayload,
    },
  })
}
