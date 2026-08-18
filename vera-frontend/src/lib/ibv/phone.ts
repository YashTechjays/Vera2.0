// E.164 helpers for the dialed insurance phone — mirrors vera_core/forms/intake.py.

import { getCountryCallingCode } from "react-phone-number-input"
import type { Country } from "react-phone-number-input"

import type { FormSchema } from "./types"

/** Same regex the backend enforces at intake and dispute-resolve. */
export const E164_RE = /^\+[1-9]\d{1,14}$/

/** The path bound to the promoted, dialed phone column's well-known handle. */
export function dialedPhonePath(schema: FormSchema): string | undefined {
  return schema.system_fields?.["insurance_provider_phone_number"]
}

/** Country + typed digits → E.164 ("" when no digits, so requiredness still applies). */
export function composePhoneValue(country: Country, national: string): string {
  const digits = national.replace(/\D/g, "")
  return digits ? `+${getCountryCallingCode(country)}${digits}` : ""
}
