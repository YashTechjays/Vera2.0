# VR2-201 — country-code dropdown for the dialed insurance phone

**Date:** 2026-08-18
**Status:** Approved (approach A, chat)

## Problem

`sections.insurance_reference_information.insurance_phone_number` ("Insurance Provider
Phone") is the number Vera dials. The backend requires E.164 on it at intake and
dispute-resolve (2026-07-15 design: prepend `+`, then reject non-E.164 with a 422), but the
form renders it as a bare `tel` input with no guidance — a user typing `555-010-0100` only
learns about the format from a server error.

## Design

One new cell widget, scoped to the dialed phone field only (the five context-only phone
leaves stay plain inputs, matching the backend's scope decision):

- **`lib/ibv/phone.ts`** — pure helpers, backend parity:
  - `E164_RE = /^\+[1-9]\d{1,14}$/` (same regex as `vera_core/forms/intake.py`).
  - `dialedPhonePath(schema)` — the path bound to the well-known `system_fields` handle
    `insurance_provider_phone_number` (schema-driven; no hardcoded section path).
  - `composePhoneValue(country, national)` — country + digits → E.164, via
    `react-phone-number-input`'s exported `getCountryCallingCode` (already a
    dependency; root export uses the small metadata set). The reverse split lives in
    `PhoneCell` itself (prefix-strip against the selected country).
- **`PhoneCell.tsx`** — country `<select>` (all countries, default **US**, names via
  `Intl.DisplayNames`) + digits input, styled as one spreadsheet cell. Composes the two
  into a single E.164 string through the existing `onChange(value)` — stored value, SSE
  answers, export, and the backend contract are all unchanged. Legacy/free-text values
  display as-is and normalize on first edit.
- **`FieldRenderer`** — new `countrySelect` prop; `type === "phone"` + `countrySelect`
  renders `PhoneCell`. `FieldRow` sets it from `dialedPhonePath(schema)`. (The matrix
  renderer never hosts this field — not wired there.)
- **`validation.ts`** — dialed-phone leaves get an `E164_RE` check in `validateLeaf`
  (create + review, mirroring where the backend enforces it). Deliberately the backend's
  loose regex, not libphonenumber validity, so the frontend never rejects a value the
  backend would accept.

## Out of scope

- The other five `"phone"`-typed context leaves (callback number, PBM phone, …).
- Voice Lab (already has its own full phone input).
- Backend changes (already shipped 2026-07-15).

## Testing

- `phone.test.ts` — path resolution from `system_fields`, compose cases.
- `validation.test.ts` — dialed phone: bad value flagged, valid E.164 passes, other
  phone leaves untouched.
- `FieldRenderer.phone.test.tsx` — dropdown renders for the dialed path only; digit edits
  and country switches emit composed E.164.
