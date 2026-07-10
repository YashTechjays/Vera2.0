// Typed wrappers over the patient-forms endpoints, mirroring the backend
// contract. Each call rides `apiRequest`, which injects the bearer token,
// unwraps the response envelope, and throws `ApiError` on failure.

import { apiRequest, apiRequestBlob } from "@/lib/api/client"
import type {
  CallAttempt,
  ListPatientFormsParams,
  PaginatedPatientForms,
  PatientFormDetail,
  PatientFormStatus,
  PatientFormStatusResult,
  ResolveDisputesPayload,
  SchemaVersionDetail,
} from "./types"

/** GET /patient-forms — paginated worklist. */
export function listPatientForms(
  params: ListPatientFormsParams = {},
): Promise<PaginatedPatientForms> {
  const { page = 1, page_size = 20, status, q } = params
  const qs = new URLSearchParams({
    page: String(page),
    page_size: String(page_size),
  })
  if (status) qs.set("status", status)
  if (q) qs.set("q", q)
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

/** PUT /patient-forms/{id}/status — change lifecycle status (status only).
 *  Rejects illegal transitions (422) and completing with open disputes (409). */
export function updatePatientFormStatus(
  formId: string,
  status: PatientFormStatus,
): Promise<PatientFormStatusResult> {
  return apiRequest<PatientFormStatusResult>(
    `/patient-forms/${encodeURIComponent(formId)}/status`,
    { method: "PUT", body: { status } },
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
