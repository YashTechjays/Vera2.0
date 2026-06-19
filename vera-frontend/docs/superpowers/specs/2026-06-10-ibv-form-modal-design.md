# IBV Form Modal — Design

**Date:** 2026-06-10
**Status:** Approved (design) — pending implementation plan
**Target app:** `vera-frontend` (Vite + React 19 + shadcn/Tailwind)

## Goal

Replace the legacy `smart-caller-fe/IBVForm.tsx` (8,442 lines) with a clean,
schema-driven IBV (Insurance Benefit Verification) form rendered in a modal,
where **each insured member (2–5 people) gets their own form instance** that can
be filled and switched between without leaving the modal.

## Why a rewrite, not a restyle

The legacy component fuses five concerns in one file: JSONForms + ~3,200 lines of
custom cell/table renderers + dispute resolution across 9 React contexts + 4
path-normalization variants + presentation via 2,167 lines of `!important`-laden
CSS. Styling is coupled to form structure, and it is a **singleton** with no
multi-person concept. Extending it for per-person forms would mean threading an
instance id through 1,200+ renderer references. A clean layered rebuild is
cheaper and produces a maintainable result.

## Schema (source of truth)

Use `vera-schema-builder/ibv-form-v1-parity-new.json`:

- Top level: `name`, `constraint_library`, `sections[]` (**21 sections**), plus
  bot-only metadata (`global_policies`, `field_list_order`, `phase_order`).
- Each section: `{ section_key, title, description, properties{}, required[] }`
  (some also carry `section_policies` — bot-only).
- Each field declares its own renderer hint:
  `{ type, title, description, ui: { widget }, required_state, enum?,
  constraint_ref?, confirm_only? }`.
- **115 fields total** — widgets: **82 text, 12 radio, 1 textarea, ~20 nested
  groups** (objects). **No tables/arrays.** `constraint_ref` resolves to
  `constraint_library` for enum options.

**The form UI consumes only the rendering subset.** The schema also carries
voice-bot prompt-generation metadata — `prompt`, `verbatim_prompt`,
`prompt_role`, `phase_order` (note: `phase_order` is *bot prompt-assembly* order,
e.g. `AGENT_PERSONA`/`TURN_TAKING_RULES`/`<SECTIONS>`, **not** form layout),
`global_policies`/`section_policies`, and field-level `rules`. The renderer
**ignores all of it** in v1.

This schema is purpose-built for rendering (explicit `ui.widget` per field), so a
small in-house renderer is the right engine — not JSONForms, not hand-coded TSX.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Where | Build fresh in `vera-frontend` |
| Engine | Lightweight schema-driven renderer over **react-hook-form + zod** + shadcn |
| Multi-person | 2–5 insured members, **person switcher inside one modal** |
| Schema source | **Bundle** `ibv-form-v1-parity-new.json` into the app now |
| People source | **Mock** the 2–5 members now |
| Save | **Mock** `apiClient.saveIbvForms(...)` now; one-line swap to backend later |
| Disputes / swap | **Out of scope** for v1 (not in v2 schema) |
| Validation | `required_state`-driven required checks now; richer rules later |

## Architecture — three layers

### 1. Schema layer — `src/lib/ibv/`
- `schema.types.ts` — `IbvSchema`, `IbvSection`, `IbvField`, `ConstraintLibrary`.
- `schema.ts` — load bundled JSON; `resolveOptions(field)` (enum or
  `constraint_ref` → values); `buildZodForSection(section)` from `required_state`.
- Keep the renderer ignorant of where the schema came from, so a later backend
  `GET /schema/ibv` is a loader swap only.

### 2. Data layer — per-person, keyed
- `InsuredPerson = { id, name, relationship }`; `people: InsuredPerson[]`.
- `valuesByPerson: Record<personId, FormValues>` — one RHF form; switching the
  active person `reset()`s to that person's values and persists edits back into
  the map on change.
- Derived per person: **dirty flag** and **completion %** (filled required ÷ total
  required) → drives the switcher badges.
- `apiClient.saveIbvForms(payload)` — mock returns success now; later
  `POST {VITE_API_URL}/ibv/forms`. Same `{ ok, savedAt }` shape in/out.

### 3. UI layer — shadcn
- `IbvFormModal` — large shadcn `Dialog`:
  - **Header:** title + **person switcher** (segmented tabs, 2–5 members, each
    with a completion badge).
  - **Body:** scrollable list of **collapsible sections** (one per schema
    section), each a table of field rows — the earlier smart-caller-fe layout.
  - **Footer:** sticky **Save / Cancel** with a dirty indicator; Save disabled
    when clean.
- `SchemaForm` — renders `schema.sections` → `Section`.
- `Section` — a **collapsible** block: header (`title`, toggle) over a table-style
  body of field rows.
- `FieldRow` — the earlier table-row layout: fixed ~180px **label cell**
  (light right border) + **input cell** filling the rest; thin row borders.
- `FieldRenderer` — switches on `ui.widget` (renders the input cell):
  - `text` → shadcn `Input`
  - `textarea` → shadcn `Textarea`
  - `radio` → shadcn `RadioGroup` (options from `resolveOptions`)
  - `object` (no widget) → nested group (its child fields as indented rows)
  - `confirm_only` fields → read-only value with a confirm affordance
  - Label = `title`; required marker from `required_state`.
- New shadcn primitives to add: `dialog`, `input`, `textarea`, `label`,
  `radio-group`, `tabs` (or a segmented control), `tooltip`, `scroll-area`.

## Data flow

```
open modal(caseId)
  → load bundled schema  +  mock people
  → activePersonId = people[0].id
  → RHF form = valuesByPerson[activePersonId]

switch person
  → persist current RHF values → valuesByPerson[prev]
  → reset RHF ← valuesByPerson[next]

edit field → RHF state → onChange persists → recompute dirty + completion%

Save → apiClient.saveIbvForms({ caseId, people, valuesByPerson })  [mock]
  → clear dirty flags
```

## Styling

**Reproduce the earlier smart-caller-fe form look** — a dense, table-style form:

- Each field is a row: fixed ~180px label cell (left, light right border) + input
  cell filling the remaining width.
- Thin row borders (`#EAECF0`-equivalent token), white field background, Inter
  ~12px, compact ~34px row height.
- Sections are collapsible blocks with a header toggle.

Implemented cleanly: Tailwind utilities + theme tokens, **no `!important`**, no
hardcoded hex (port the old hex values into the theme). Same visual result, none
of the legacy CSS debt.

## Out of scope (v1)

- Dispute / swap (bot value vs corrected value).
- Service-coverage tables (not present in v2 schema).
- Real backend schema/people/save endpoints (mocked, swap later).
- Cross-field conditional visibility and the schema's operational logic
  (`global_policies`, `section_policies`, field-level `rules`) — render all fields
  for now; wire rules in a later pass.
- All voice-bot prompt metadata (`prompt`, `verbatim_prompt`, `prompt_role`,
  `phase_order`) — not part of the data-entry UI.

## Verification

- `tsc` typecheck + `npm run build`.
- Manual: open modal, fill fields for person A, switch to B (A's values persist),
  completion badges update, Save resolves via mock, dirty clears.
