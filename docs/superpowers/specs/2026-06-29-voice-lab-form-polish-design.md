# Voice Lab pre-call form polish — design

**Date:** 2026-06-29
**Scope:** `vera-frontend/src/pages/VoiceLab.tsx` (pre-session form only) + scoped phone-input CSS.

## Goal

Make the Voice Lab outbound-call form look polished and consistent with the
shadcn design system. The `react-phone-number-input` widget currently renders
with its stock `style.css`, so the country/flag dropdown and number box don't
match the app's `Input` (height, border radius, focus ring). The buttons and
surrounding layout also read as a bare form rather than a guided action.

The live session and transcript panels (`SessionPanel`, `TranscriptPanel`) are
**out of scope** and stay exactly as they are.

## Decisions

- **Phone field:** one unified bordered field — flag/country selector as a left
  segment with a thin divider, number input borderless to the right, sharing a
  single `focus-within` ring. Looks like a single native input.
- **Width:** form/field constrained to `max-w-lg` (was `max-w-md`).

## Changes

### 1. Unified phone field (Tailwind arbitrary variants)

> **Implementation note (shipped):** an earlier draft of this spec used a scoped
> stylesheet `vera-frontend/src/components/phone-input.css`. The shipped code
> instead keeps the library's stock `react-phone-number-input/style.css` import
> and does every override inline in `VoiceLab.tsx` via Tailwind arbitrary
> variants on the wrapper (`[&_.PhoneInput]:…`, `[&_.PhoneInputInput::placeholder]:…`).
> No `phone-input.css` is created. The target styles below are unchanged — only
> the delivery mechanism differs (Tailwind utilities, not a `.css` file).

Wrap the existing `<PhoneInput>` in a container and style the library's emitted
classes with Tailwind arbitrary variants on that wrapper's `className`. The
component's props/API are unchanged — only presentation.

Target the library's emitted classes (selectors scoped under the wrapper):

- `.PhoneInput` → the bordered field: `border border-input`, `rounded-md`,
  `h-9`, `bg-background`, `shadow-xs`, `transition-[color,box-shadow]`.
- `:focus-within` → shadcn focus ring: `border-ring` + `ring-[3px] ring-ring/50`
  (matches `Input`'s `focus-visible` treatment). Driven by `focus-within` since
  focus lands on the inner `<input>`.
- `.PhoneInputCountry` → left segment: horizontal padding, a right divider
  (`border-r border-input`), flag + caret aligned.
- `.PhoneInputInput` → borderless, transparent, no outline/shadow, fills
  remaining width, `text-sm`, placeholder uses `text-muted-foreground`.
- Invalid state: read off the inner input's own `aria-invalid` via
  `:has(input[aria-invalid=true])`, switching border + ring to
  `border-destructive` / `ring-destructive/20` (mirrors `Input`'s `aria-invalid`
  styles; no duplicated wrapper state).
- Disabled state: `opacity-50`, `pointer-events-none` via `:has(input:disabled)`
  (the inner input is disabled while a session is pending — see below).

The utilities use the same design tokens the shadcn components use (`border-input`,
`ring-ring/50`, `bg-background`, `rounded-md`, etc.), so the field tracks
light/dark theme automatically. No hard-coded colors.

The wrapper carries the invalid hook so the field border reacts to
`showPhoneError`, consistent with how `Input` reacts to `aria-invalid`.

### 2. Layout polish

Inside the existing form `Card` / `CardContent`:

- Add a compact section header row: `PhoneOutgoing` icon + "Outbound call"
  title (`text-sm font-medium`) + one-line muted subtext explaining what the
  form does.
- Keep the `Label` ("Phone number") above the unified field.
- Keep the swap-in-place helper/error line below the field:
  - valid/neutral: muted hint showing the resolved E.164 number
    (`we'll dial +1…`).
  - invalid + touched: destructive "Enter a valid phone number…" text.
- Constrain the field and content column to `max-w-lg`.
- Tighten vertical rhythm (consistent `space-y`).

No copy/PHI changes; the dialed number is session-scoped component state only
(no storage), per the frontend PHI guardrails.

### 3. Buttons & states

- **Start outbound call** becomes the primary CTA (`variant="default"`), since
  it's the only visible button by default.
- While `pending === "outbound"`: show a `Loader2` spinner (animate-spin) with
  "Starting call…" and keep the button disabled. The phone field also goes to
  its disabled style while pending.
- Disabled-until-valid is unchanged: button stays disabled when
  `!phoneValid || pending !== null`.
- The in-browser button (only shown when `SHOW_IN_BROWSER_SESSION`) stays
  secondary (`variant="outline"`) and gets the same spinner treatment for
  `pending === "browser"`.

## Non-goals

- No changes to validation logic, API calls, or the LiveKit session flow.
- No changes to `SessionPanel` / `TranscriptPanel`.
- No new runtime dependency.

## Testing / verification

- `npm run build` (or typecheck) passes.
- Manual: field matches shadcn inputs at rest, on focus (ring), when invalid
  (red border/ring), and when disabled; country dropdown still opens and the
  selected dial code shows; spinner appears while dialing; layout holds at
  `max-w-lg` and in dark mode.

> Note: the phone logic already moved to `react-phone-number-input`
> (libphonenumber-js) in commit `70ed4e4`, which deleted the hand-rolled
> `src/lib/phone/countries.ts` + `countries.test.ts` and the
> `composeE164`/`dialFor`/`isE164` helpers. This spec is presentation-only and
> adds no validation logic of its own, so there is no phone unit test to re-run;
> a typecheck/build plus the manual checks above are the verification.
