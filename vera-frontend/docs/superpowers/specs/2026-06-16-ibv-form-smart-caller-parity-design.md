# IBV Form (smart-caller-fe parity) — Design

**Date:** 2026-06-16
**Status:** Approved (design) — pending implementation plan
**Target app:** `vera-frontend` (Vite + React 19 + react-router + Tailwind v4 + shadcn/ui)
**Reference UI:** `../smart-caller-fe` (`/ibv-form` route, `src/components/IBVForm.tsx`)

## Goal

Recreate the smart-caller-fe IBV (Insurance Benefit Verification) form's **look and
behavior** inside `vera-frontend`, but built around the project's own schema
(`src/lib/ibv/ibv-schema.json`) and conventions. The form is a schema-driven
data-entry surface presented in a large modal, reproducing smart-caller-fe's dense
table layout and its **full inline dispute workflow** (apply / swap / Re-Ask /
resolve-all / confidence highlighting) on **mocked** dispute data.

This is a **full rebuild from scratch**: the existing `src/components/ibv/*` and
`src/lib/ibv/*` (modal + collapsible sections + separate dispute-review panel) are
**replaced**. The bundled schema JSON (`src/lib/ibv/ibv-schema.json`) is retained
as the source of truth.

## Why a rebuild (not a restyle of the existing vera form)

The existing vera IBV form is a modal of collapsible sections with a *separate*
dispute-review panel — a different interaction model from smart-caller-fe's
*inline, per-field* dispute controls. And we are deliberately **not** porting
smart-caller-fe's engine: its 8,442-line `IBVForm.tsx` fuses JSONForms + ~3,200
lines of custom cell/table renderers + dispute state across 9 contexts + 2,167
lines of `!important` CSS. We reproduce the *result* (look + behavior) on a clean,
layered, hand-rolled renderer that matches vera conventions.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Scope of existing code | **Full rebuild** — replace `src/components/ibv/*` and `src/lib/ibv/*` (keep `ibv-schema.json`) |
| Presentation | **Modal** (large shadcn `Dialog`), reference styling from smart-caller-fe |
| Engine | In-house schema-driven renderer over **react-hook-form + zod** + shadcn — **not** JsonForms |
| Layout inside modal | **Two-column**: left main sections + right sticky reference rail |
| Disputes | **Full dispute UX**, **mocked** data (apply / swap / Re-Ask / resolve-all / confidence highlight / tooltip). No live polling, no backend. |
| Multi-person | **Keep person switcher** (2–5 insured members, per-person form instances). The one intentional divergence from smart-caller-fe (single-form), justified by the schema's spouse/dependent fields and the existing vera capability. |
| Validation | `required_state`-driven (zod) + conditional `rules` (e.g. spouse required when `coverage_type = Family`) |
| Schema source | **Bundle** `src/lib/ibv/ibv-schema.json` now; later backend `GET /schema/ibv` is a loader swap |
| People source | **Mock** now |
| Save | **Mock** `saveIbvForms(...)` returning `{ ok, savedAt }`; one-line backend swap later |

## Schema (source of truth)

`src/lib/ibv/ibv-schema.json`. Top level: `name`, `constraint_library`,
`sections[]`. Each section: `{ section_key, title, description, properties{},
required[] }`. Each field: `{ type, title, description, ui:{widget},
required_state, enum?, constraint_ref?, confirm_only?, rules? }`.

The schema mixes two structural shapes:

1. **Field-row sections** — flat `properties` of leaf fields. Widgets: `text`,
   `radio`, `textarea`. e.g. Patient Information, Insurance Information, Benefit
   Coverage.
2. **CPT matrix sections** — `properties` of group objects, each containing
   CPT-code row objects whose leaves are the columns
   **Covered / Copay / Coinsurance / Auth Req** (Infertility Treatment adds
   **Cycle Limit / Additional Notes**). e.g. General Coverage, Diagnostic Testing
   (Labs/X-ray/Ultrasound), Male Partner Coverage, Infertility Treatment.

`constraint_ref` resolves to `constraint_library` for enum options. The renderer
**ignores** all bot-only metadata: `prompt`, `verbatim_prompt`, `prompt_role`,
`policies`, `section_policies`, `phase_order`, `global_policies`.

## Architecture — three layers

### Layer 1 — Schema (`src/lib/ibv/`), pure / UI-agnostic
- `types.ts` — `IbvSchema`, `IbvSection`, `IbvField`, `FlatField`, `FormValues`,
  `InsuredPerson`, `Dispute`, `DisputeMeta`, `SectionMatrix`/`MatrixColumn`/`MatrixGroup`.
- `schema.ts`:
  - `resolveOptions(field)` — `enum` wins, else `constraint_ref` → `constraint_library.values`.
  - `widgetOf(field)` — defaults to `text`.
  - `flattenSection(section)` — ordered leaf/group rows for field-row sections.
  - `getSectionMatrix(section)` — detect + model CPT tables (groups × CPT rows × shared columns).
  - `requiredPaths()`, `completionPercent(values)`.
  - `buildZodSchema(section | whole)` — required from `required_state`; conditional
    required from field-level `rules` (`effect: "make this required"` when a
    referenced field equals a value).
- `disputes.ts` — confidence-score → level (`high/medium/low/very-low`) → color
  token; `humanizeLabel`; prior/current accessors; path normalization.
- `mock.ts` — `mockPeople` (2–5), `mockDisputes` (seed a handful of field paths
  with `previousValue/currentValue/confidence/evidence/reasoning`),
  `saveIbvForms(payload)` → `{ ok, savedAt }`.

Keep the renderer ignorant of where schema/people/disputes come from — backend
swap is a loader change only.

### Layer 2 — State (`IbvProvider` + `useIbv`)
- **Field values:** one `react-hook-form` instance bound to the **active person**.
  `valuesByPerson: Record<personId, FormValues>`. Switching person persists current
  RHF values → `valuesByPerson[prev]`, then `reset()` ← `valuesByPerson[next]`.
- **Dispute state (parallel context):** `disputeValues`, `currentValues`,
  `appliedSet`, `swapStates`, `reaskedSet`, `metadata`, `resolveAll` — all keyed by
  person + dotted field path.
- **Derived per person:** dirty flag (RHF), completion % (filled required ÷ required).
- **Modal control:** `modalOpen`, `openForm(personId?)`, `closeForm()` with an
  unsaved-changes guard.
- **Save:** `saveIbvForms({ caseId, people, valuesByPerson, disputeFieldsByPerson,
  reaskedByPerson })` (mock) → clears dirty.

### Layer 3 — UI (`src/components/ibv/`)
- `IbvFormModal` — large `Dialog`:
  - **Header:** title + **person switcher** (segmented tabs, 2–5, each a completion badge).
  - **Body (two-column):** left = scrollable main collapsible sections; right =
    **sticky reference rail**.
  - **Footer:** sticky — **"Resolve all disputes"** checkbox + Save / Cancel +
    dirty indicator (Save disabled when clean & nothing to resolve).
- `SchemaForm` — classifies each schema section into left column vs right rail, renders `Section`s.
- `Section` — collapsible block: header (`title` + toggle) over a table-style body.
- `FieldRow` — fixed **~180px label cell** (light right border) + **input cell**
  filling the rest; thin row borders; required marker from `required_state`;
  inline dispute controls.
- `FieldRenderer` — switch on `ui.widget`: `text`→`Input`, `textarea`→`Textarea`,
  `radio`→`RadioGroup` (`resolveOptions`), `object`→nested group / CPT table;
  `confirm_only` → read-only value with confirm affordance.
- `SectionMatrix` — CPT table: group label column (+ ICD-10 when present), CPT-code
  row column, shared value columns; each cell carries dispute controls.
- **Dispute primitives:** `ApplyButton` (✓ → ↶ when applied), `SwapButton` (⇄),
  `ReAskButton`, `DisputeBadge` (truncated prior value, click-to-expand),
  `DisputeTooltip` (confidence score / evidence / reasoning on hover).
- **New shadcn primitives to add:** `tabs` (or segmented control), `tooltip`,
  `scroll-area`, `checkbox`. (`dialog`, `input`, `textarea`, `label`,
  `radio-group`, `collapsible`, `card`, `badge`, `button` already exist.)

## Section placement (left main vs right reference rail)

- **Right reference rail** (schema marks these context-only / "do not ask"):
  Patient Information, Appointment Information, VA Information (verification),
  Hospital Information.
- **Left main column** (collected from the rep — disputes live here): Insurance
  Information, Benefit Coverage, General Coverage, and all CPT matrix sections
  (Diagnostic Testing, Male Partner Coverage, Infertility Treatment, …).

## Dispute behavior (full UX, mocked)

Per disputed field:
- Confidence-colored border + background (level from `confidence_score`).
- **Prior-value badge** showing the previous value (truncated, click-to-expand).
- **✓ Apply** — writes the current/disputed value into the form; toggles to **↶**
  (unapply restores the badge). Tracked in `appliedSet`.
- **⇄ Swap** — toggles input ↔ prior value (`swapStates`).
- **Re-Ask** — flags the field for re-verification (`reaskedSet`); excluded for
  the reference-rail/context sections.
- **Tooltip** on hover — confidence score, evidence, reasoning.

Footer **"Resolve all disputes"** applies every current value before save. Save
payload per person: `{ formData, disputeFields[], reaskedFields[] }`.

## Styling

Reproduce smart-caller-fe's dense table look with Tailwind utilities + theme
tokens — **no `!important`, no raw hex in components** (port hex → theme tokens):
- Row border `#EAECF0`-equivalent; fixed ~180px label cell with light right
  border; ~34px row height; ~12px Inter; white field background.
- Dispute highlight amber `#FFA500` border / `#FFFAED` bg; confidence colors
  teal `#34B2B2` (high) / amber `#F59E0B` (medium) / red `#EF4444`/`#DC2626` (low).
- Apply button teal `#2D9B9B` → green `#10B981` (applied); swap button blue
  `#003e64` → cyan `#34B2B2` (swapped).

## Out of scope (v1)

- Live dispute polling and real backend (schema / people / save / disputes) — all mocked.
- The schema's bot-only operational metadata (`prompt`, `verbatim_prompt`,
  `policies`, `section_policies`, `phase_order`, `global_policies`).
- Cross-field conditional **visibility** (render all fields); only conditional
  **required** rules are wired into validation.

## Verification

- `tsc` typecheck + `npm run build`.
- Manual: open modal → fill person A → switch to B (A's values persist) →
  completion badges update → apply / swap / Re-Ask a dispute → toggle resolve-all →
  Save resolves via mock and clears dirty.
