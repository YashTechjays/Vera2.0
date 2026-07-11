import rawDemoSchema from "../../../../vera-backend/data/form_schemas/ibv_form_standard_v2.json"
import { mockDisputes, type DisputeMap } from "./disputes"
import { allLeaves, optionsOf, parseSchema } from "./schema"
import type { FormValues, InsuredPerson } from "./types"
import type { SavePayload } from "./disputes"

/**
 * Dev fixture ONLY (demo form + tests): the backend's compiled artifact,
 * imported from its source of truth (same monorepo) so no copy can drift.
 * Real forms never use it — they fetch the exact document their
 * `schema_version_id` pins from the backend (see IbvProvider).
 */
export const demoSchema = parseSchema(rawDemoSchema)

/** Mock insured members. Swap for backend `GET /case/:id/people` later. */
export const mockPeople: InsuredPerson[] = [
  { id: "p1", name: "Sarah Johnson", relationship: "Patient" },
  { id: "p2", name: "Michael Johnson", relationship: "Spouse" },
  { id: "p3", name: "Emma Johnson", relationship: "Dependent" },
  { id: "p4", name: "David Martinez", relationship: "Patient" },
]

/** Disputes seeded for the first person only (demo). */
export const disputesByPerson: Record<string, DisputeMap> = {
  [mockPeople[0].id]: mockDisputes,
}

/** Realistic overrides for prominent fields, keyed by root-anchored path. */
const OVERRIDES: FormValues = {
  "sections.patient_information.chart_number": "CH-4503",
  "sections.patient_information.patient_name": "Ava Davis",
  "sections.patient_information.patient_dob": "02/15/1990",
  "sections.patient_information.patient_gender": "Female",
  "sections.patient_information.spouse_partner_name": "Noah Davis",
  "sections.patient_information.spouse_partner_dob": "07/09/1991",
  "sections.patient_information.spouse_gender": "Male",
  "sections.appointment_information.appointment_type": "New Patient",
  "sections.appointment_information.appointment_date": "06/21/2026",
  "sections.verification_information.verified_by": "Alex Morgan",
  "sections.verification_information.verified_at": "06/16/2026",
  "sections.verification_information.callback_number": "+1 555 0203",
  "sections.hospital_information.hospital_name": "Demo Health Partners",
  "sections.hospital_information.hospital_address": "123 Demo St, Austin, TX",
  "sections.hospital_information.tax_id": "987654313",
  "sections.hospital_information.npi": "1234567893",
  "sections.insurance_information.doctor_inside_network": "Yes",
  "sections.insurance_information.facility_inside_network": "Yes",
  "sections.insurance_information.out_of_network_coverage": "No",
  "sections.insurance_information.plan_type": "PPO",
  "sections.insurance_information.cob_status": "Primary",
  "sections.insurance_information.policy_number": "POL-550411",
  "sections.insurance_information.group_number": "GRP-2039",
  "sections.insurance_information.group_name": "Umbrella Health",
  "sections.insurance_information.policy_situs": "TX",
  "sections.benefit_coverage.benefit_year_type": "Calendar Year",
  "sections.benefit_coverage.plan_effective_date": "01/01/2026",
  "sections.benefit_coverage.plan_year_information": "01/01/2026 - 12/31/2026",
  "sections.benefit_coverage.coverage_type": "Family",
  "sections.benefit_coverage.pcp_referral_required": "No",
  "sections.benefit_coverage.telehealth_covered": "Yes",
  "sections.benefit_coverage.plan_fund_type": "Fully Funded",
  "sections.benefit_coverage.employer_support_size": "Large Group",
  "sections.benefit_coverage.infertility_plan_mandate": "Yes",
  "sections.provider_reference_information.provider_name": "Dr. Jane Smith",
  "sections.provider_reference_information.npi": "1982736450",
  "sections.provider_reference_information.office_location": "Austin Fertility Center",
  "sections.insurance_representative.rep_name": "Taylor Reed",
  "sections.insurance_representative.call_reference_number": "REF-99381",
  "sections.insurance_reference_information.insurance_provider_name": "Demo Health Plan",
  "sections.insurance_reference_information.insurance_phone_number": "+1 555 0100",
  "sections.insurance_reference_information.web_portal": "demo-portal.example.com",
}

/** Sensible per-field value when there's no explicit override. */
function defaultFor(path: string, options: string[]): string {
  const last = path.split(".").pop() ?? ""
  if (options.length > 0) {
    const meaningful = options.filter((o) => o !== "N/A")
    return meaningful[0] ?? options[0] ?? ""
  }
  if (/dob|date/.test(last)) return "01/01/2026"
  if (/copay/.test(last)) return "$30"
  if (/coinsurance/.test(last)) return "20%"
  if (/cycle|used/.test(last)) return "3"
  if (/notes/.test(last)) return "—"
  if (/npi/.test(last)) return "1234567893"
  if (/tax_id/.test(last)) return "98-7654313"
  if (/phone|callback|number/.test(last)) return "+1 555 0203"
  if (/address|location/.test(last)) return "123 Demo St, Austin, TX"
  if (/email/.test(last)) return "demo@example.com"
  if (/name/.test(last)) return "Demo Value"
  if (/amount|total|remaining|maximum|met/.test(last)) return "$1,000"
  if (/portal/.test(last)) return "demo-portal.example.com"
  return "Demo"
}

/** A fully-populated demo form (every leaf field), keyed by root-anchored path. */
export const mockValues: FormValues = (() => {
  const out: FormValues = {}
  for (const { path, field } of allLeaves(demoSchema)) {
    out[path] = OVERRIDES[path] ?? defaultFor(path, optionsOf(field))
  }
  return out
})()

export type SaveResult = { ok: true; savedAt: string }

/**
 * Mock save. Same shape the backend will return from
 * `POST {VITE_API_URL}/ibv/forms` — swapping is a one-line change here.
 */
export async function saveIbvForms(
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  _payload: Record<string, SavePayload>
): Promise<SaveResult> {
  await new Promise((r) => setTimeout(r, 600))
  return { ok: true, savedAt: new Date().toISOString() }
}
