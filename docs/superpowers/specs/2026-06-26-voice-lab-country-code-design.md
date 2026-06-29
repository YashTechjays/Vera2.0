# Voice Lab — Country Code Dropdown + Error Management

**Date:** 2026-06-26
**Branch:** `feat/voice-lab-country-code`
**Status:** Design approved, pending spec review

## Problem

The Voice Lab outbound-call form (`vera-frontend/src/pages/VoiceLab.tsx`) has a
single freeform phone-number input that requires the operator to hand-type a full
E.164 string (e.g. `+15551234567`). This is error-prone: it is easy to omit the
`+`, forget the country code, or include spaces/dashes. The only feedback today is
a single generic red line at the bottom of the card, shown after a failed
round-trip to the backend.

We want two improvements:

1. **Country code dropdown** — pick the country (which supplies the `+NN` dial
   code) and type only the national number; the form composes the E.164 string.
2. **Better error management** — validate the number client-side before submit
   with an inline field error, and surface request/session failures in a clearer,
   more visible alert.

## Backend contract (unchanged — source of truth for validation)

`POST /voice-lab/sessions` (`vera-backend/.../api/v1/voice_lab.py`) validates the
outbound number against E.164:

```
_E164 = re.compile(r"^\+[1-9]\d{1,14}$")   # leading +, 1–15 digits, first non-zero
```

It returns:
- **409** `outbound SIP is not configured` — when the SIP trunk env is unset.
- **422** `phone_number must be E.164 for an outbound call` — when the number is
  missing or fails the regex.

This design does **not** change the backend. The frontend mirrors the E.164 shape
for pre-submit validation, but the backend regex remains authoritative.

## Scope

### In scope
- Curated static country list (no new dependencies, no per-country length rules).
- Country `Select` + national-number `Input` in the outbound form.
- Client-side inline validation of the composed E.164 number (generic shape only).
- A reusable inline alert for form/session-level errors.
- Unit tests for the pure compose/validate helpers.

### Out of scope (YAGNI)
- `libphonenumber-js` or any per-country length/format validation.
- Full ISO (~240) country list — a curated ~25 is enough for a dev/QA harness.
- Distinct 409-vs-422-vs-network message mapping — the existing single
  `ApiError.message` passthrough is retained, just rendered in the new alert.
- Any change to browser-mode session start, the transcript stream, or backend code.

## Design

### 1. Country data + pure helpers — `src/lib/phone/countries.ts`

```ts
export type Country = { code: string; name: string; dial: string; flag: string }

// ~25 curated countries (ISO code, display name, dial code, flag emoji).
export const COUNTRIES: Country[] = [
  { code: "US", name: "United States", dial: "+1",  flag: "🇺🇸" },
  { code: "IN", name: "India",         dial: "+91", flag: "🇮🇳" },
  // … GB, CA, AU, DE, FR, ES, IT, NL, IE, SG, AE, SA, ZA, BR, MX, JP, …
]

export const DEFAULT_COUNTRY = "US"

/** Strip everything but digits from an operator-typed national number. */
export function digitsOnly(national: string): string

/** Compose the E.164 string from a dial code + national digits. */
export function composeE164(dial: string, national: string): string

/** True iff `dial + digitsOnly(national)` matches ^\+[1-9]\d{1,14}$. */
export function validateNational(dial: string, national: string): boolean
```

These pure functions hold all the phone logic so it is unit-testable without a DOM.
`composeE164` concatenates the dial code (already `+NN`) with the stripped national
digits. `validateNational` runs the composed result through the same E.164 regex
the backend uses.

### 2. UI — outbound form in `VoiceLab.tsx`

The single freeform input is replaced by two adjacent fields:

- **Country `Select`** (native `src/components/ui/select.tsx`) — each option renders
  `🇺🇸 United States (+1)`. Default value `DEFAULT_COUNTRY` (US). State: `country`
  (the selected ISO code; the dial code is derived by lookup).
- **National-number `Input`** — `type="tel"`, placeholder `5551234567`. State:
  `national`. The operator types only the local number; the `+NN` comes from the
  dropdown.

On submit, the form composes `composeE164(dial, national)` and sends it as
`phone_number` to `startVoiceSession`. `BASE_URL`/API wrapper unchanged.

Component-state only — no PHI persistence (a callee phone number is handled in
session-scoped React state exactly as the existing `phone` state is today).

### 3. Inline field validation

A `touched` flag tracks whether the operator has interacted with the national
input (set on first change / blur). Validation derives from `validateNational`:

- The **"Start outbound call"** button is `disabled` while the composed number is
  invalid (replacing today's `phone.trim() === ""` check).
- When `touched` **and** invalid, a field-level error renders directly under the
  input, and the input gets `aria-invalid` (the native `Select`/`Input` already
  style `aria-invalid`). An untouched empty field shows no error.
- Validation re-runs on every change, so the message clears itself once the number
  becomes valid. Changing the country re-validates too.

### 4. Visible error styling — `src/components/ui/alert.tsx` (new)

There is no `alert` component yet. Add a minimal shadcn-style one (a bordered
banner with an icon slot and a `destructive` variant), matching the existing
component conventions in `src/components/ui/`. Use it for **form/session-level**
errors — the `start()`/`endSession()`/`LiveKitRoom onError` failures currently held
in the `error` state — replacing the bare `<p className="text-sm text-destructive">`.

Field-level validation messages stay attached to their field (small destructive
text); the alert is reserved for request/session failures so the two error classes
are visually distinct.

### 5. Data flow

```
country (ISO) ──lookup──▶ dial (+NN) ─┐
                                       ├─▶ composeE164 ─▶ phone_number ─▶ startVoiceSession
national (typed) ──digitsOnly────────┘
                                       └─▶ validateNational ─▶ button.disabled + field error
```

## Error handling

| Failure | Where | Presentation |
|---|---|---|
| Empty / malformed national number | client, pre-submit | Field-level error under the input + disabled button. No round-trip. |
| Backend 409 (SIP not configured) | server | `ApiError.message` rendered in the inline alert. |
| Backend 422 (not E.164) | server | `ApiError.message` rendered in the inline alert (should be rare — client pre-validates the same shape). |
| Network / unknown | client | Generic "Could not start the session." in the inline alert (existing fallback). |
| LiveKit room error / end-session failure | client | Existing messages, now rendered in the inline alert. |

## Testing

`src/lib/phone/countries.test.ts` (Vitest — the repo already uses `vitest run`):

- `composeE164` — dial + national concatenation.
- `digitsOnly` — strips spaces, dashes, parens, `+` from the national part.
- `validateNational`:
  - valid US number → true,
  - too long (>15 total digits) → false,
  - empty national → false,
  - non-digit junk that reduces to empty → false,
  - national entered with a leading `+`/country code stripped to digits → still
    composes one `+NN` prefix (no double `+`).

Pure functions only — no component/DOM harness required. Manual smoke test:
`npm run dev`, pick a country, type a number, confirm the composed E.164 reaches
the backend and that an invalid number blocks submit with an inline message.

## Files touched

| File | Change |
|---|---|
| `src/lib/phone/countries.ts` | **new** — data + pure helpers |
| `src/lib/phone/countries.test.ts` | **new** — unit tests |
| `src/components/ui/alert.tsx` | **new** — inline alert component |
| `src/pages/VoiceLab.tsx` | edit — country `Select` + national `Input`, inline validation, alert |

No backend changes. No new npm dependencies.
