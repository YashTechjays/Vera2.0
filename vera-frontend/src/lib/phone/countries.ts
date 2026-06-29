// Country dial-code data + pure phone helpers for the Voice Lab outbound form.
// Deliberately a small curated list (this is a dev/QA harness, not a consumer
// signup): no `libphonenumber-js`, no per-country length rules. The composed
// number is validated only against the generic E.164 shape — the backend regex
// (`^\+[1-9]\d{1,14}$` in voice_lab.py) stays the source of truth.

export type Country = { code: string; name: string; dial: string; flag: string }

/** Curated set of common calling destinations. ISO 3166-1 alpha-2 `code`,
 *  display `name`, E.164 `dial` prefix, and `flag` emoji. */
export const COUNTRIES: Country[] = [
  { code: "US", name: "United States", dial: "+1", flag: "🇺🇸" },
  { code: "CA", name: "Canada", dial: "+1", flag: "🇨🇦" },
  { code: "GB", name: "United Kingdom", dial: "+44", flag: "🇬🇧" },
  { code: "IE", name: "Ireland", dial: "+353", flag: "🇮🇪" },
  { code: "IN", name: "India", dial: "+91", flag: "🇮🇳" },
  { code: "AU", name: "Australia", dial: "+61", flag: "🇦🇺" },
  { code: "NZ", name: "New Zealand", dial: "+64", flag: "🇳🇿" },
  { code: "DE", name: "Germany", dial: "+49", flag: "🇩🇪" },
  { code: "FR", name: "France", dial: "+33", flag: "🇫🇷" },
  { code: "ES", name: "Spain", dial: "+34", flag: "🇪🇸" },
  { code: "IT", name: "Italy", dial: "+39", flag: "🇮🇹" },
  { code: "NL", name: "Netherlands", dial: "+31", flag: "🇳🇱" },
  { code: "SE", name: "Sweden", dial: "+46", flag: "🇸🇪" },
  { code: "CH", name: "Switzerland", dial: "+41", flag: "🇨🇭" },
  { code: "PT", name: "Portugal", dial: "+351", flag: "🇵🇹" },
  { code: "SG", name: "Singapore", dial: "+65", flag: "🇸🇬" },
  { code: "AE", name: "United Arab Emirates", dial: "+971", flag: "🇦🇪" },
  { code: "SA", name: "Saudi Arabia", dial: "+966", flag: "🇸🇦" },
  { code: "ZA", name: "South Africa", dial: "+27", flag: "🇿🇦" },
  { code: "JP", name: "Japan", dial: "+81", flag: "🇯🇵" },
  { code: "BR", name: "Brazil", dial: "+55", flag: "🇧🇷" },
  { code: "MX", name: "Mexico", dial: "+52", flag: "🇲🇽" },
  { code: "AR", name: "Argentina", dial: "+54", flag: "🇦🇷" },
  { code: "PH", name: "Philippines", dial: "+63", flag: "🇵🇭" },
  { code: "BD", name: "Bangladesh", dial: "+880", flag: "🇧🇩" },
  { code: "PK", name: "Pakistan", dial: "+92", flag: "🇵🇰" },
  { code: "LK", name: "Sri Lanka", dial: "+94", flag: "🇱🇰" },
  { code: "NP", name: "Nepal", dial: "+977", flag: "🇳🇵" },
  { code: "ID", name: "Indonesia", dial: "+62", flag: "🇮🇩" },
  { code: "MY", name: "Malaysia", dial: "+60", flag: "🇲🇾" },
  { code: "TH", name: "Thailand", dial: "+66", flag: "🇹🇭" },
  { code: "VN", name: "Vietnam", dial: "+84", flag: "🇻🇳" },
  { code: "CN", name: "China", dial: "+86", flag: "🇨🇳" },
  { code: "HK", name: "Hong Kong", dial: "+852", flag: "🇭🇰" },
  { code: "KR", name: "South Korea", dial: "+82", flag: "🇰🇷" },
  { code: "TR", name: "Turkey", dial: "+90", flag: "🇹🇷" },
  { code: "IL", name: "Israel", dial: "+972", flag: "🇮🇱" },
  { code: "QA", name: "Qatar", dial: "+974", flag: "🇶🇦" },
  { code: "KW", name: "Kuwait", dial: "+965", flag: "🇰🇼" },
  { code: "EG", name: "Egypt", dial: "+20", flag: "🇪🇬" },
  { code: "NG", name: "Nigeria", dial: "+234", flag: "🇳🇬" },
  { code: "KE", name: "Kenya", dial: "+254", flag: "🇰🇪" },
  { code: "BE", name: "Belgium", dial: "+32", flag: "🇧🇪" },
  { code: "AT", name: "Austria", dial: "+43", flag: "🇦🇹" },
  { code: "NO", name: "Norway", dial: "+47", flag: "🇳🇴" },
  { code: "DK", name: "Denmark", dial: "+45", flag: "🇩🇰" },
  { code: "PL", name: "Poland", dial: "+48", flag: "🇵🇱" },
]

/** Default country selection for the outbound form. */
export const DEFAULT_COUNTRY = "US"

// Mirrors the backend E.164 contract: a leading + then 1–15 digits, first non-zero.
const E164 = /^\+[1-9]\d{1,14}$/

/** The E.164 dial prefix for an ISO country code, falling back to the default
 *  country's dial if `code` isn't in the curated list (guards against list drift). */
export function dialFor(code: string): string {
  const match = COUNTRIES.find((c) => c.code === code)
  if (match) return match.dial
  return COUNTRIES.find((c) => c.code === DEFAULT_COUNTRY)!.dial
}

/** Strip everything but digits from an operator-typed national number. */
export function digitsOnly(national: string): string {
  return national.replace(/\D/g, "")
}

/** Compose the E.164 string from a dial code (already `+NN`) + national digits. */
export function composeE164(dial: string, national: string): string {
  return `${dial}${digitsOnly(national)}`
}

/** True iff `value` is a well-formed E.164 number. */
export function isE164(value: string): boolean {
  return E164.test(value)
}
