import { mockDisputes, type DisputeMap } from "./disputes"
import { allLeafFields, resolveOptions } from "./schema"
import type { FormValues, InsuredPerson } from "./types"
import type { SavePayload } from "./disputes"

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

/** Realistic overrides for prominent fields, keyed by dotted path. */
const OVERRIDES: FormValues = {
  "patient_information.chart_number": "CH-4503",
  "patient_information.patient_name": "Noah Davis",
  "patient_information.patient_dob": "02/15/1990",
  "patient_information.patient_gender": "Male",
  "patient_information.spouse_partner_name": "Mia Davis",
  "patient_information.spouse_partner_dob": "07/09/1991",
  "patient_information.spouse_gender": "Female",
  "appointment_information.appointment_type": "New Patient",
  "appointment_information.appointment_date": "06/21/2026",
  "verification_information.verified_by": "Alex Morgan",
  "verification_information.verified_at": "06/16/2026",
  "verification_information.callback_number": "+1 555 0203",
  "hospital_information.name": "Demo Health Partners",
  "hospital_information.address": "123 Demo St, Austin, TX",
  "hospital_information.tax_id": "98-7654313",
  "hospital_information.npi": "1234567893",
  "insurance_information.doctor_inside_network": "Yes",
  "insurance_information.facility_inside_network": "Yes",
  "insurance_information.out_of_network_coverage": "N/A",
  "insurance_information.health_plan": "Blue Cross",
  "insurance_information.coordination_of_benefits": "Primary",
  "insurance_information.policy_number": "POL-550411",
  "insurance_information.group_information": "GRP-2039",
  "insurance_information.group_name": "Umbrella Health",
  "insurance_information.home_plan": "BCBS TX",
  "benefit_coverage.benefit_year_type": "Calendar Year",
  "benefit_coverage.plan_effective_date": "01/01/2026",
  "benefit_coverage.plan_year_information": "Jan–Dec 2026",
  "benefit_coverage.coverage_type": "Family",
  "benefit_coverage.referrals_telehealth": "No",
  "benefit_coverage.telehealth": "Yes",
  "benefit_coverage.plan_fund_type": "Fully Funded",
  "benefit_coverage.employer_support_size": "Large Group",
  "benefit_coverage.infertility_plan_mandate": "Yes",
  "provider_reference_information.provider_name": "Dr. Jane Smith",
  "provider_reference_information.npi": "1982736450",
  "provider_reference_information.location": "Austin Fertility Center",
  "insurance_representative.insurance_rep_name": "Taylor Reed",
  "insurance_representative.call_reference_number": "REF-99381",
  "insurance_representative.web_portal_ref_number": "WP-22107",
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
  if (/cycle_limit/.test(last)) return "3"
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

/** A fully-populated demo form (every leaf field), keyed by dotted path. */
export const mockValues: FormValues = (() => {
  const out: FormValues = {}
  for (const { path, field } of allLeafFields()) {
    out[path] = OVERRIDES[path] ?? defaultFor(path, resolveOptions(field))
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
