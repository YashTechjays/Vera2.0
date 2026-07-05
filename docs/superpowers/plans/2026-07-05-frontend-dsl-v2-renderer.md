# Frontend DSL v2.1 Renderer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render the patient (IBV) form dynamically from the compiled form-schema DSL v2.1 artifact, replacing every v1-DSL assumption in the frontend.

**Architecture:** Bundle the compiled `ibv_form_standard_v2.json` verbatim; a typed TS model mirrors spec §4; a pure condition evaluator drives `applicable_when`/`required.when`/`contradictions`; `schema.ts` precomputes flat leaves with their ancestor gate chains and builds the `ui.layout: "table"` matrix model; components render sections in document order by role, graying inapplicable fields.

**Tech Stack:** React + Vite + TypeScript, vitest, Tailwind. No new dependencies.

## Global Constraints

- Field paths are root-anchored: `sections.<section_key>.<field>...` — byte-identical to `field_answer.field_path` (the values-map key from `GET /patient-forms/{id}`).
- `sections` is an OBJECT keyed by section_key; key order = UI order.
- Voice-only constructs to IGNORE: `prompt`, `ask_groups`, `tasks`, `flow_rules`, `codes.speak_cpt`, `tags`, `derive`, `confirm_in_task`.
- Only `role: "readonly"` leaves are display-only; ask/confirm/context/input are editable. Section roles collect/context/ui_only are all editable.
- Never hand-edit `ibv_form_standard_v2.json` (backend freshness test); the frontend copy is a verbatim byte copy.
- PHI rules (vera-frontend/CLAUDE.md): no PHI in logs/URLs/storage; no console.log of values.
- Gates: `npx tsc -b && npx eslint . && npx vitest run && npm run build` in `vera-frontend/`.
- Do not commit until the user asks (harness rule overrides the skill's commit steps).

All paths below are relative to `vera-frontend/` unless prefixed.

---

### Task 1: Bundle the v2 artifact

**Files:**
- Overwrite: `src/lib/ibv/ibv-schema.json` (byte copy of `vera-backend/data/form_schemas/ibv_form_standard_v2.json`)
- Delete: `scripts/transform_ibv_percpt.py`

**Steps:**
- [ ] `cp ../vera-backend/data/form_schemas/ibv_form_standard_v2.json src/lib/ibv/ibv-schema.json`
- [ ] `rm scripts/transform_ibv_percpt.py` (verify nothing references it: `grep -rn transform_ibv_percpt package.json src scripts` → no hits)

### Task 2: Typed DSL model — `types.ts`

**Files:**
- Rewrite: `src/lib/ibv/types.ts`

**Produces (consumed by every later task):**

```ts
export type ConditionOp = "eq" | "ne" | "in" | "not_in"
export type FieldCondition = { field: string; op: ConditionOp; value: string | string[] }
export type Condition =
  | FieldCondition
  | { all: Condition[] }
  | { any: Condition[] }
  | { not: Condition }
  | { ref: string }

export type LeafType = "text" | "enum" | "date" | "currency" | "percent" | "integer" | "phone"
export type FieldRole = "ask" | "confirm" | "context" | "readonly" | "input"
export type SectionRole = "collect" | "context" | "ui_only"
export type Requirement = boolean | { when: Condition }
export type Codes = { cpt?: string[]; icd10?: string[]; speak_cpt?: boolean }
export type Validation = { pattern?: string; range?: { min?: number; max?: number } }

export type LeafField = {
  type: LeafType; title: string; role: FieldRole
  required?: Requirement; values?: string[]; special_values?: string[]
  default?: string; validation?: Validation
  applicable_when?: Condition; inapplicable_value?: string
  tags?: string[]; codes?: Codes; ui?: { widget?: "textarea" }; description?: string
}
export type GroupField = {
  type: "group"; title: string; fields: Record<string, Field>
  applicable_when?: Condition; integrity?: "all" | "any"
  codes?: Codes; description?: string
}
export type Field = LeafField | GroupField

export type Alternative = { members: string[]; ask?: string }
export type Section = {
  title: string; role?: SectionRole; description?: string
  applicable_when?: Condition; codes?: Codes
  alternatives?: Alternative[]; ui?: { layout?: "table" }
  fields: Record<string, Field>
}
export type Contradiction = {
  rule_key: string; when: Condition; fields: string[]; reason: string; clarify?: string
}
export type FormSchema = {
  dsl_version: string; name: string; insurance_type: string; description?: string
  system_fields?: Record<string, string>
  shared_conditions?: Record<string, Condition>
  sections: Record<string, Section>
  contradictions?: Contradiction[]
}

export type FormValues = Record<string, string>
/** A leaf with its root-anchored path and the applicable_when chain (own + ancestors). */
export type FlatLeaf = { path: string; sectionKey: string; field: LeafField; depth: number; gates: Condition[] }
export type InsuredPerson = { id: string; name: string; relationship: string }
```

Voice-only keys (`prompt`, `ask_groups`, `tasks`, `flow_rules`, `derive`, `confirm_in_task`, `prompt` on groups/sections) are intentionally not modeled — the cast in schema.ts drops through them.

- [ ] Write the file; no test (types only). `npx tsc -b` will gate it via later tasks.

### Task 3: Condition evaluator — `conditions.ts` (TDD)

**Files:**
- Create: `src/lib/ibv/conditions.ts`
- Test: `src/lib/ibv/conditions.test.ts`

**Produces:** `evaluateCondition(cond: Condition, values: FormValues, shared?: Record<string, Condition>): boolean`

Semantics: missing value evaluates as `""`; `eq`/`ne` compare strings; `in`/`not_in` take a string[] value; `all`=every, `any`=some, `not`=negation; `ref` resolves into `shared` (unknown ref → false, no logging — PHI).

- [ ] **Write failing tests** covering: eq true/false, eq against missing value, ne with missing value (true), in/not_in, nested all/any/not, ref resolution (incl. the real `male_partner_in_scope` shape: `all[ref, field]`), unknown ref → false.
- [ ] Run: `npx vitest run src/lib/ibv/conditions.test.ts` → FAIL (module missing)
- [ ] **Implement** (~30 lines, recursive switch on shape: `"field" in c`, `"all" in c`, …)
- [ ] Run again → PASS

### Task 4: Schema services — `schema.ts` (TDD)

**Files:**
- Rewrite: `src/lib/ibv/schema.ts`
- Rewrite test: `src/lib/ibv/schema.test.ts`

**Produces:**

```ts
export const schema: FormSchema                       // cast of the bundled JSON
export const sectionEntries: [string, Section][]      // document order
export function isGroup(f: Field): f is GroupField
export type FlatRow = { path: string; key: string; field: Field; depth: number; gates: Condition[] }
export function flattenSection(sectionKey: string, section: Section): FlatRow[]  // groups + leaves, doc order; gates include the section's applicable_when
export function allLeaves(): FlatLeaf[]               // cached; every leaf across the schema
export function isApplicable(gates: Condition[], values: FormValues): boolean
export function isRequired(field: LeafField, values: FormValues): boolean
export function optionsOf(field: LeafField): string[]      // enum: values ∪ special_values
export function suggestionsOf(field: LeafField): string[]  // non-enum: special_values ?? []
export function completionPercent(values: FormValues): number  // required ∧ applicable; filled = non-empty value OR declared default
export function contradictionWarnings(values: FormValues): Contradiction[]

export type TableCell = { path: string; field: LeafField; gates: Condition[] }
export type TableColumn = { key: string; title: string }
export type TableRow = { path: string; label: string; cells: Record<string, TableCell | undefined> }
export type TableGroup = { path: string; label: string; icd10: string; rows: TableRow[]; extras: Record<string, TableCell | undefined> }
export type SectionTable = { columns: TableColumn[]; extraColumns: TableColumn[]; hasIcd: boolean; groups: TableGroup[]; leaves: FlatRow[] }
export function getSectionTable(sectionKey: string, section: Section): SectionTable | null  // null unless ui.layout === "table"
```

Table build rules (no heuristic — `ui.layout` only):
- Section-level leaves (e.g. `infertility_tx_covered`) → `SectionTable.leaves`, rendered as normal field rows above the table.
- Each top-level group = a `TableGroup` (label = title, icd10 = `codes.icd10?.join(", ") ?? ""`).
- Subgroup children (e.g. `cpt_58323`) = rows; row label = subgroup title; cells = its leaf children.
- Group-level leaves whose keys appear under any subgroup-bearing group (`cycle_limit`, `additional_notes`) define `extraColumns`; they render as per-group rowspan cells (`extras`).
- A group with no subgroups (`ovulation_induction`) is itself one row: leaves in `extraColumns` go to `extras`, the rest are its cells; row label = `codes.cpt?.join(", ") ?? "—"`.
- `columns` = union of row-cell keys in first-appearance order.
- Every `TableCell.gates` chains section → group → subgroup → leaf `applicable_when`.

- [ ] **Write failing tests**:
  - `sectionEntries` has 23 entries, first `patient_information`, includes `insurance_representative` and `insurance_reference_information`.
  - `flattenSection` roots paths: chart number row path === `"sections.patient_information.chart_number"`.
  - Gate chaining: the `sections.deductibles.family.total` leaf is inapplicable for `{}` and for coverage_type=Individual, applicable for coverage_type=Family (gates include the group's `ref: family_coverage`).
  - Section gate chaining: `sections.male_partner_coverage.*` leaves applicable only when coverage_type=Family ∧ spouse_gender=Male.
  - `isRequired`: `patient_name` (true) always; `spouse_partner_name` required only under family_coverage.
  - `optionsOf` on `…prior_auth` includes `"Prior auth department"` (special_values merged); `suggestionsOf(plan_type)` = ["PPO","HMO","EPO","POS"].
  - `getSectionTable(general_coverage)`: columns covered/copay/coinsurance/prior_auth, 3 groups × 1 row, no extraColumns, hasIcd true.
  - `getSectionTable(infertility_treatment)`: `leaves` contains infertility_tx_covered; group IUI has 3 rows; extras have cycle_limit + additional_notes; ovulation_induction has 1 row whose cells include covered and whose extras include cycle_limit.
  - `getSectionTable(diagnostic_testing)` → null (no layout hint, despite CPT groups).
  - `completionPercent({})` < 100; filling every required∧applicable leaf (compute from allLeaves + isApplicable against the built map, using `values`-aware fixpoint: fill patient/insurance basics incl. coverage_type=Individual so family branches stay off) → 100.
  - `contradictionWarnings`: `{}` → []; mandate=Yes ∧ infertility_tx_covered=No → 1 warning with the right rule_key.
- [ ] Run: `npx vitest run src/lib/ibv/schema.test.ts` → FAIL
- [ ] **Implement** schema.ts per the interfaces above (drop `resolveOptions`, `widgetOf`, `getSectionMatrix`, `requiredPaths`, `sectionPlacement`, `FlatField`).
- [ ] Run again → PASS. Also `npx vitest run src/lib/ibv` (conditions still green).

### Task 5: Validation — `validation.ts` (TDD)

**Files:**
- Rewrite: `src/lib/ibv/validation.ts`
- Rewrite test: `src/lib/ibv/validation.test.ts`

**Produces:** `validateAll(values): ValidationErrors`, `validateSection(sectionKey, values)` (prefix filter on `sections.<key>.`), `type ValidationErrors = Record<string, string>`.

Per applicable leaf (inapplicable leaves are never flagged):
1. Empty value: error iff `isRequired` and no `default` declared (default counts as filled).
2. Non-empty: legal-by-declaration short-circuit — value ∈ special_values, or equals `default` / `inapplicable_value` → valid.
3. `validation.pattern` (text-family): RegExp full test → "<title> is invalid".
4. `validation.range` (currency/percent/integer): parse numeric by stripping `[$,%\s]`; NaN → "<title> must be a number"; out of bounds → "<title> must be between …". No range → no numeric check (values are transcribed strings).

Drop the zod dependency here (plain loop); zod stays used elsewhere in the app.

- [ ] **Write failing tests**: required empty (patient_name) flagged; chart_number (optional, has default) not flagged; conditional required (spouse_partner_name only when coverage_type=Family — and when Individual it is also *inapplicable*, so never flagged); inapplicable never flagged even when required (family deductible total with Individual); pattern (tax_id "12345" invalid, "123456789" valid); range (a copay cell: "$1,500.50" valid, "-5" invalid, "$0" valid via special_values; coinsurance "150%" invalid, "20%" valid); validateSection prefix filter.
- [ ] Run → FAIL, **implement**, run → PASS.

### Task 6: Mock + disputes path migration

**Files:**
- Modify: `src/lib/ibv/mock.ts` (OVERRIDES keys → v2 root-anchored paths/renames; `resolveOptions` → `optionsOf`)
- Modify: `src/lib/ibv/disputes.ts` (mockDisputes keys → v2 paths; `humanizeLabel` strips the leading `sections.` segment; plan_type dispute values PPO/POS replace Blue Cross/BCBS)
- Check: `src/lib/ibv/disputes.test.ts` (pure helpers — update only if it references old paths/labels)

Key renames: `hospital_information.name/address/tax_id/npi` → `sections.hospital_information.hospital_name/hospital_address/tax_id/npi`; `insurance_information.health_plan/coordination_of_benefits/group_information/home_plan` → `plan_type/cob_status/group_number/policy_situs`; `benefit_coverage.plan_year_information/referrals_telehealth/telehealth` → `renewal_date/pcp_referral_required/telehealth_covered`; `provider_reference_information.location` → `office_location`; `insurance_representative.insurance_rep_name` → `rep_name`; `web_portal_ref_number` → `sections.insurance_reference_information.web_portal_reference_number`; dispute path `general_coverage.office_visits.cpt_1.covered` → `sections.general_coverage.office_visits.cpt_99211.covered`.

- [ ] Update both files; run `npx vitest run src/lib/ibv` → PASS.

### Task 7: Components

**Files:**
- Rewrite: `src/components/ibv/FieldRenderer.tsx`
- Modify: `src/components/ibv/FieldRow.tsx`
- Modify: `src/components/ibv/Section.tsx`
- Rewrite: `src/components/ibv/SectionMatrix.tsx`
- Rewrite: `src/components/ibv/SchemaForm.tsx`

**FieldRenderer** (props: `field: LeafField, path, value, onChange, disabled?, placeholder?, invalid?` + existing visual props):
- `role === "readonly"` → display-only div (reuse the old confirm_only branch).
- enum → `<select>` over `optionsOf(field)` plus the current value if it's missing from the list (bad data must stay visible); empty option label = `field.default ?? "Select…"`.
- `ui.widget === "textarea"` → textarea.
- else input: `type="tel"` for phone; `inputMode="decimal"` for currency/percent, `"numeric"` for integer; `placeholder` = supplied placeholder ?? `field.default` (date fields: "MM/DD/YYYY" fallback — native date inputs can't display the transcribed "MM/DD/YYYY" strings the backend stores, so dates stay text).
- non-enum with `special_values` → attach `<datalist id={path}>` suggestions (combobox).
- `disabled` → `disabled` attr + `opacity-60` + `cursor-not-allowed`; `invalid` → red inset ring.

**FieldRow** (props: `path, field: LeafField, depth, gates: Condition[]`):
- `applicable = isApplicable(gates, values)`; `required = applicable && isRequired(field, values)`.
- Inapplicable → gray label (`text-muted-foreground/60`), renderer `disabled` with `placeholder = field.inapplicable_value`.
- `*` only when required (∧ applicable). `invalid = !!errors[path] && applicable` from context.

**Section** (props: `sectionKey, section, green?`):
- `const table = getSectionTable(sectionKey, section)`; table sections render `table.leaves` as FieldRows then `<SectionMatrix …>`; otherwise `flattenSection(sectionKey, section)` rows — group rows render the header band (also grayed when their gates fail), leaves render FieldRow.
- Section `applicable_when` false → whole content grayed (leaf gates already include the section gate, so rows disable themselves; the header gets `opacity-60`).

**SectionMatrix** (props: `table: SectionTable`): thead = Service | ICD-10 (if hasIcd) | CPT Code | columns | extraColumns. Per group: label + icd rowspan cells; per row: label cell + one MatrixCell per column (`cells[col.key]`; undefined → inert gray td); extras as rowspan cells on the first row. MatrixCell computes applicability from `cell.gates` and disables + placeholders like FieldRow; keeps the dispute controls as today.

**SchemaForm**:
- Delete `HIDDEN`, `INSURANCE_REFERENCE_SECTION`.
- `LEFT_TOP = ["patient_information", "insurance_information"]`; `RIGHT_TOP = ["appointment_information", "verification_information", "benefit_coverage"]`; `RAIL = ["hospital_information", "provider_reference_information", "insurance_reference_information"]` (rail keeps the teal reference box look; insurance_reference_information is a real section now).
- Everything else renders below **in document order**: runs of consecutive non-table sections flow two-up (alternating columns), each `ui.layout: "table"` section breaks the run and renders full-width.
- Contradiction banner at the top: `contradictionWarnings(values)` → amber `role="alert"` banner listing each `reason`.

- [ ] Implement all five; `npx tsc -b && npx eslint .` clean; `npx vitest run` green.

### Task 8: Gates + smoke check

- [ ] `npx tsc -b && npx eslint . && npx vitest run && npm run build` — all green.
- [ ] Smoke-render via the dev server + playwright-cli (demo form from Live Monitoring uses mock values): all 23 sections present, tables render, conditional graying reacts to coverage_type. Best effort — skip gracefully if the app needs backend auth.

### Task 9: Repo rule — simplify + regate

- [ ] Run the code-simplifier agent on the changed frontend files ("simplify code").
- [ ] Re-run all gates from Task 8.

## Self-review notes

- Spec coverage: §4 grammar → Task 2; §4.5 evaluator → Task 3; §5 UI contract (roles, widgets, options, required∧applicable, layout table) → Tasks 4/7; contradictions banner → Tasks 4/7; kill HIDDEN/synthetic section → Task 7; bundle artifact → Task 1; values by prefixed field_path → no provider change needed (keys flow through).
- Deliberately skipped: `alternatives` badge (spec-optional nicety), `integrity` in completion (absent from artifact), serving schema from backend (out of scope per task).
- Type consistency: `FlatLeaf`/`FlatRow`/`SectionTable` names used consistently across Tasks 4–7.
