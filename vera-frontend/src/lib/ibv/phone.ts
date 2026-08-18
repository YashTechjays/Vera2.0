// E.164 helpers for the schema's phone handles — mirrors vera_core/forms/intake.py.

import { getCountryCallingCode } from "react-phone-number-input"
import type { Country } from "react-phone-number-input"

import type { FormSchema } from "./types"

/** Same regex the backend enforces at intake and dispute-resolve. */
export const E164_RE = /^\+[1-9]\d{1,14}$/

// The dialed number plus the supervisor callback — the two phones read out on calls.
const E164_HANDLES = ["insurance_provider_phone_number", "callback_number"]

/** Paths bound to the well-known phone handles that must hold E.164 values. */
export function phonePaths(schema: FormSchema): Set<string> {
  const paths = new Set<string>()
  for (const handle of E164_HANDLES) {
    const path = schema.system_fields?.[handle]
    if (path) paths.add(path)
  }
  return paths
}

/** Country + typed digits → E.164 ("" when no digits, so requiredness still applies). */
export function composePhoneValue(country: Country, national: string): string {
  const digits = national.replace(/\D/g, "")
  return digits ? `+${getCountryCallingCode(country)}${digits}` : ""
}
