import { useState } from "react"
import { getCountries, getCountryCallingCode, parsePhoneNumber } from "react-phone-number-input"
import type { Country } from "react-phone-number-input"

import { cn } from "@/lib/utils"
import { composePhoneValue } from "@/lib/ibv/phone"

const DEFAULT_COUNTRY: Country = "US"

type CountryOption = { code: Country; label: string }

/** The dialed default first, then alphabetical by ISO code. */
function byDefaultThenCode(a: CountryOption, b: CountryOption): number {
  if (a.code === DEFAULT_COUNTRY) return -1
  if (b.code === DEFAULT_COUNTRY) return 1
  return a.code.localeCompare(b.code)
}

const COUNTRIES: CountryOption[] = getCountries()
  .map((code) => ({ code, label: `${code} +${getCountryCallingCode(code)}` }))
  .sort(byDefaultThenCode)

type Props = {
  value: string
  onChange: (value: string) => void
  disabled?: boolean
  placeholder?: string
  /** cell look from FieldRenderer (borders/background/disabled/invalid) */
  className?: string
}

/** Country-code dropdown + digits input composing one E.164 value (VR2-201). */
export function PhoneCell({ value, onChange, disabled, placeholder, className }: Props) {
  const parsedCountry = parsePhoneNumber(value)?.country
  const [country, setCountry] = useState<Country>(parsedCountry ?? DEFAULT_COUNTRY)
  const [seenCountry, setSeenCountry] = useState(parsedCountry)
  // An external edit (live answer, dispute swap) can move the value to another country —
  // follow it during render, mirroring LiveCallModal (an effect trips the hooks lint rule).
  if (parsedCountry !== seenCountry) {
    setSeenCountry(parsedCountry)
    if (parsedCountry) setCountry(parsedCountry)
  }

  const prefix = `+${getCountryCallingCode(country)}`
  const national = value.startsWith(prefix) ? value.slice(prefix.length) : value

  function selectCountry(next: Country) {
    setCountry(next)
    if (national) onChange(composePhoneValue(next, national))
  }

  return (
    <span className={cn("flex h-full w-full min-w-0 items-stretch", className)}>
      <select
        aria-label="Country code"
        value={country}
        disabled={disabled}
        onChange={(e) => selectCountry(e.target.value as Country)}
        className="w-[70px] shrink-0 cursor-pointer border-r border-ibv-input-border bg-transparent px-[3px] font-ibv text-[12px] font-bold text-black outline-none disabled:cursor-not-allowed"
      >
        {COUNTRIES.map((c) => (
          <option key={c.code} value={c.code}>
            {c.label}
          </option>
        ))}
      </select>
      <input
        type="tel"
        inputMode="tel"
        value={national}
        onChange={(e) => onChange(composePhoneValue(country, e.target.value))}
        disabled={disabled}
        placeholder={placeholder}
        className="min-h-[24px] w-0 min-w-0 flex-1 truncate border-0 bg-transparent px-[3px] py-0 font-ibv text-[13.3px] font-bold text-black outline-none focus:bg-white focus:shadow-[inset_0_0_0_2px_rgba(59,130,246,0.2)]"
      />
    </span>
  )
}
