# IBV Form (smart-caller-fe parity) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the vera-frontend IBV form so it reproduces smart-caller-fe's dense table look and full inline dispute workflow, driven by `src/lib/ibv/ibv-schema.json`, presented in a two-column modal with a person switcher.

**Architecture:** Three layers. (1) Pure schema/validation/dispute logic in `src/lib/ibv/` (unit-tested with vitest). (2) State in a single `IbvProvider` React context holding a per-person flat value store (`Record<dottedPath, string>`), inline dispute flags, and zod validation errors. (3) A shadcn/Tailwind UI layer: a large `Dialog` with a person switcher, a left column of collapsible data-collection sections (field rows + CPT matrix tables) carrying inline dispute controls, and a right sticky reference rail.

**Tech Stack:** Vite + React 19 + TypeScript, Tailwind v4, shadcn/ui over the unified `radix-ui` package, `zod` for validation, vitest for tests. **No react-hook-form** (the flat dotted-path value store fits plain context better; zod handles validation). **No JsonForms.**

---

## Engine note (refinement vs spec)

The approved spec (`docs/superpowers/specs/2026-06-16-ibv-form-smart-caller-parity-design.md`) said "react-hook-form + zod." This plan keeps **zod** but **omits react-hook-form**: the value store is a flat `Record<dottedPath, string>`, and RHF interprets dotted names as nested objects, forcing flatten/unflatten throughout. Plain context (already proven in the existing provider) + zod validation is simpler and equally schema-driven. All other spec decisions are unchanged.

## File structure

**Replace / rewrite (Layer 1 — `src/lib/ibv/`)**
- `types.ts` — schema + person + form value types.
- `schema.ts` — `schema` load, `resolveOptions`, `widgetOf`, `flattenSection`, `getSectionMatrix`, `requiredPaths`, `completionPercent`, `sectionPlacement`.
- `validation.ts` (new) — `buildZodObject`, `validateSection`, `validateAll`.
- `disputes.ts` — dispute model + inline-flag helpers/reducers + confidence colors + `humanizeLabel` + `mockDisputes`.
- `mock.ts` — `mockPeople`, `disputesByPerson`, `saveIbvForms` (new payload shape).
- Tests: `schema.test.ts`, `validation.test.ts`, `disputes.test.ts`.

**Replace (Layer 2 — `src/components/ibv/`)**
- `IbvProvider.tsx` — context: values, dispute flags, validation, save, modal control, person switching.

**Replace (Layer 3 — `src/components/ibv/`)**
- `DisputeControls.tsx` (new) — `ApplyButton`, `SwapButton`, `ReAskButton`, `DisputeBadge`, `DisputeTooltip`.
- `FieldRenderer.tsx` — widget switch (text / textarea / radio).
- `FieldRow.tsx` (new) — label cell + input cell + inline dispute controls.
- `SectionMatrix.tsx` (new) — CPT matrix table with dispute cells.
- `Section.tsx` — collapsible section (rows or matrix).
- `PersonSwitcher.tsx` (new) — segmented person tabs with completion badges.
- `SchemaForm.tsx` (new) — left/right section split.
- `IbvFormModal.tsx` — assembled modal (header / two-column body / footer).

**Delete (no longer used)**
- `src/components/ibv/SectionTable.tsx`, `src/components/ibv/DisputeReviewPanel.tsx`.

**New shadcn primitives (`src/components/ui/`)**
- `tooltip.tsx`, `checkbox.tsx`, `tabs.tsx`.

**Unchanged wiring (already correct)**
- `src/components/layout/AppShell.tsx` already mounts `<IbvProvider>` and `<IbvFormModal />`.
- `src/pages/LiveMonitoring.tsx` already triggers `openForm()`.

---

## Task 1: Install zod and add missing shadcn primitives

**Files:**
- Modify: `package.json` (via npm)
- Create: `src/components/ui/tooltip.tsx`
- Create: `src/components/ui/checkbox.tsx`
- Create: `src/components/ui/tabs.tsx`

- [ ] **Step 1: Install zod**

Run: `npm install zod`
Expected: `package.json` gains `zod` under dependencies; install succeeds.

- [ ] **Step 2: Create the tooltip primitive**

Create `src/components/ui/tooltip.tsx`:

```tsx
import { Tooltip as TooltipPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

function TooltipProvider({
  delayDuration = 150,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Provider>) {
  return (
    <TooltipPrimitive.Provider
      data-slot="tooltip-provider"
      delayDuration={delayDuration}
      {...props}
    />
  )
}

function Tooltip(props: React.ComponentProps<typeof TooltipPrimitive.Root>) {
  return (
    <TooltipProvider>
      <TooltipPrimitive.Root data-slot="tooltip" {...props} />
    </TooltipProvider>
  )
}

function TooltipTrigger(
  props: React.ComponentProps<typeof TooltipPrimitive.Trigger>
) {
  return <TooltipPrimitive.Trigger data-slot="tooltip-trigger" {...props} />
}

function TooltipContent({
  className,
  sideOffset = 4,
  children,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Content>) {
  return (
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Content
        data-slot="tooltip-content"
        sideOffset={sideOffset}
        className={cn(
          "z-50 max-w-xs rounded-md border border-border bg-popover px-3 py-2 text-xs text-popover-foreground shadow-md data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0",
          className
        )}
        {...props}
      >
        {children}
      </TooltipPrimitive.Content>
    </TooltipPrimitive.Portal>
  )
}

export { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider }
```

- [ ] **Step 3: Create the checkbox primitive**

Create `src/components/ui/checkbox.tsx`:

```tsx
import { Checkbox as CheckboxPrimitive } from "radix-ui"
import { Check } from "lucide-react"

import { cn } from "@/lib/utils"

function Checkbox({
  className,
  ...props
}: React.ComponentProps<typeof CheckboxPrimitive.Root>) {
  return (
    <CheckboxPrimitive.Root
      data-slot="checkbox"
      className={cn(
        "peer size-4 shrink-0 rounded-[4px] border border-input shadow-xs outline-none transition-shadow",
        "focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50",
        "data-[state=checked]:border-primary data-[state=checked]:bg-primary data-[state=checked]:text-primary-foreground",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className
      )}
      {...props}
    >
      <CheckboxPrimitive.Indicator
        data-slot="checkbox-indicator"
        className="flex items-center justify-center text-current"
      >
        <Check className="size-3.5" />
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  )
}

export { Checkbox }
```

- [ ] **Step 4: Create the tabs primitive**

Create `src/components/ui/tabs.tsx`:

```tsx
import { Tabs as TabsPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

function Tabs({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Root>) {
  return (
    <TabsPrimitive.Root
      data-slot="tabs"
      className={cn("flex flex-col gap-2", className)}
      {...props}
    />
  )
}

function TabsList({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.List>) {
  return (
    <TabsPrimitive.List
      data-slot="tabs-list"
      className={cn(
        "inline-flex h-9 w-fit items-center justify-center gap-1 rounded-lg bg-muted p-1 text-muted-foreground",
        className
      )}
      {...props}
    />
  )
}

function TabsTrigger({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      data-slot="tabs-trigger"
      className={cn(
        "inline-flex items-center justify-center gap-1.5 rounded-md px-3 py-1 text-sm font-medium whitespace-nowrap transition-[color,box-shadow] outline-none",
        "focus-visible:ring-[3px] focus-visible:ring-ring/50",
        "disabled:pointer-events-none disabled:opacity-50",
        "data-[state=active]:bg-background data-[state=active]:text-foreground data-[state=active]:shadow-sm",
        className
      )}
      {...props}
    />
  )
}

function TabsContent({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return (
    <TabsPrimitive.Content
      data-slot="tabs-content"
      className={cn("flex-1 outline-none", className)}
      {...props}
    />
  )
}

export { Tabs, TabsList, TabsTrigger, TabsContent }
```

- [ ] **Step 5: Verify typecheck**

Run: `npx tsc -b`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add package.json package-lock.json src/components/ui/tooltip.tsx src/components/ui/checkbox.tsx src/components/ui/tabs.tsx
git commit -m "feat(ibv): add zod + tooltip/checkbox/tabs primitives"
```

---

## Task 2: Rewrite schema/dispute/person types

**Files:**
- Modify (rewrite): `src/lib/ibv/types.ts`

- [ ] **Step 1: Rewrite the types file**

Replace the entire contents of `src/lib/ibv/types.ts`:

```ts
// Types for the IBV v1-parity schema (src/lib/ibv/ibv-schema.json). We model only
// the rendering subset. Bot-prompt metadata (prompt, verbatim_prompt, prompt_role,
// phase_order, policies) exists in the JSON but is ignored by the form UI — except
// field-level `rules`, which feed conditional-required validation.

export type FieldWidget = "text" | "textarea" | "radio"

export type RuleCondition = {
  comparison: string
  value: string
  field: string
}

export type FieldRule = {
  effect: string
  match?: string
  conditions?: RuleCondition[]
  summary?: string
}

export type IbvField = {
  type: "string" | "object"
  title: string
  description?: string
  ui?: { widget?: FieldWidget }
  required_state?: "required" | "optional"
  enum?: string[]
  constraint_ref?: string
  confirm_only?: boolean
  confirm_value?: unknown
  /** narrative guidance ("prose") is not a data field — not rendered */
  prompt_role?: string
  /** bot prompt; ignored by the UI */
  verbatim_prompt?: string
  /** ICD-10 reference code on a CPT matrix group */
  icd10?: string
  /** conditional-required rules (effect "make this required") */
  rules?: FieldRule[]
  // present when type === "object"
  properties?: Record<string, IbvField>
  required?: string[]
}

export type IbvSection = {
  section_key: string
  title: string
  description?: string
  properties: Record<string, IbvField>
  required?: string[]
  /** override for the matrix table's first-column header */
  row_header?: string
}

export type ConstraintDef = {
  category?: string
  values?: string[]
  description?: string
}

export type IbvSchema = {
  name: string
  constraint_library: Record<string, ConstraintDef>
  sections: IbvSection[]
}

/** A resolved leaf or group field with its full dotted path, ready to render. */
export type FlatField = {
  /** dotted path, e.g. "patient_information.patient_name" */
  path: string
  field: IbvField
  /** nesting depth (0 = direct section child) */
  depth: number
}

/** Form values for a single person, keyed by dotted field path. */
export type FormValues = Record<string, string>

export type InsuredPerson = {
  id: string
  name: string
  relationship: string
}
```

- [ ] **Step 2: Verify typecheck**

Run: `npx tsc -b`
Expected: errors only in files that import removed names (these are fixed in later tasks). The `types.ts` file itself must compile. If `tsc -b` is noisy, instead run `npx tsc --noEmit -p tsconfig.app.json` and confirm no error originates inside `src/lib/ibv/types.ts`.

- [ ] **Step 3: Commit**

```bash
git add src/lib/ibv/types.ts
git commit -m "feat(ibv): rewrite schema/person types with rules"
```

---

## Task 3: Schema helpers + section placement (TDD)

**Files:**
- Modify (rewrite): `src/lib/ibv/schema.ts`
- Create: `src/lib/ibv/schema.test.ts`

- [ ] **Step 1: Write the failing test**

Create `src/lib/ibv/schema.test.ts`:

```ts
import { describe, expect, it } from "vitest"

import {
  schema,
  resolveOptions,
  widgetOf,
  flattenSection,
  getSectionMatrix,
  requiredPaths,
  completionPercent,
  sectionPlacement,
} from "./schema"
import type { IbvField } from "./types"

describe("resolveOptions", () => {
  it("prefers an explicit enum", () => {
    const f: IbvField = { type: "string", title: "X", enum: ["A", "B"] }
    expect(resolveOptions(f)).toEqual(["A", "B"])
  })

  it("falls back to the constraint library", () => {
    const f: IbvField = { type: "string", title: "X", constraint_ref: "YES_NO" }
    expect(resolveOptions(f)).toEqual(["Yes", "No"])
  })

  it("returns [] when neither is present", () => {
    expect(resolveOptions({ type: "string", title: "X" })).toEqual([])
  })
})

describe("widgetOf", () => {
  it("defaults to text", () => {
    expect(widgetOf({ type: "string", title: "X" })).toBe("text")
  })
  it("uses the declared widget", () => {
    expect(widgetOf({ type: "string", title: "X", ui: { widget: "radio" } })).toBe(
      "radio"
    )
  })
})

describe("flattenSection", () => {
  it("prefixes paths with section_key and tracks depth", () => {
    const section = schema.sections.find(
      (s) => s.section_key === "patient_information"
    )!
    const rows = flattenSection(section)
    const chart = rows.find((r) => r.path.endsWith("chart_number"))!
    expect(chart.path).toBe("patient_information.chart_number")
    expect(chart.depth).toBe(0)
  })
})

describe("getSectionMatrix", () => {
  it("returns null for a flat field-row section", () => {
    const patient = schema.sections.find(
      (s) => s.section_key === "patient_information"
    )!
    expect(getSectionMatrix(patient)).toBeNull()
  })

  it("models the general_coverage CPT table", () => {
    const gc = schema.sections.find((s) => s.section_key === "general_coverage")!
    const m = getSectionMatrix(gc)
    expect(m).not.toBeNull()
    expect(m!.columns.map((c) => c.key)).toEqual([
      "covered",
      "copay",
      "coinsurance",
      "prior_auth",
    ])
    expect(m!.groups.length).toBeGreaterThanOrEqual(2)
  })
})

describe("requiredPaths / completionPercent", () => {
  it("reports 100 when every required field is filled", () => {
    const filled = Object.fromEntries(requiredPaths().map((p) => [p, "x"]))
    expect(completionPercent(filled)).toBe(100)
  })
  it("reports 0 for an empty form", () => {
    expect(completionPercent({})).toBe(0)
  })
})

describe("sectionPlacement", () => {
  it("puts context-only sections on the right rail", () => {
    expect(sectionPlacement("patient_information")).toBe("rail")
    expect(sectionPlacement("appointment_information")).toBe("rail")
    expect(sectionPlacement("verification_information")).toBe("rail")
    expect(sectionPlacement("hospital_information")).toBe("rail")
  })
  it("puts collected sections in the main column", () => {
    expect(sectionPlacement("insurance_information")).toBe("main")
    expect(sectionPlacement("benefit_coverage")).toBe("main")
    expect(sectionPlacement("general_coverage")).toBe("main")
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npx vitest run src/lib/ibv/schema.test.ts`
Expected: FAIL — `sectionPlacement` (and possibly others) not exported.

- [ ] **Step 3: Rewrite `schema.ts`**

Replace the entire contents of `src/lib/ibv/schema.ts`:

```ts
import rawSchema from "./ibv-schema.json"
import type { FlatField, FormValues, IbvField, IbvSchema, IbvSection } from "./types"

export const schema = rawSchema as unknown as IbvSchema

/** Options for a select/radio field: explicit enum wins, else constraint_library. */
export function resolveOptions(field: IbvField): string[] {
  if (field.enum?.length) return field.enum
  if (field.constraint_ref) {
    return schema.constraint_library[field.constraint_ref]?.values ?? []
  }
  return []
}

/** Effective widget for a leaf field (defaults to text). */
export function widgetOf(field: IbvField): "text" | "textarea" | "radio" {
  return field.ui?.widget ?? "text"
}

/**
 * Flatten a section's properties into ordered render rows. Object fields are
 * emitted as a group header followed by their (indented) children.
 */
export function flattenSection(section: IbvSection): FlatField[] {
  const out: FlatField[] = []
  const walk = (props: Record<string, IbvField>, prefix: string, depth: number) => {
    for (const [key, field] of Object.entries(props)) {
      if (field.prompt_role === "prose") continue // narrative guidance, not data
      const path = prefix ? `${prefix}.${key}` : key
      out.push({ path, field, depth })
      if (field.type === "object" && field.properties) {
        walk(field.properties, path, depth + 1)
      }
    }
  }
  walk(section.properties, section.section_key, 0)
  return out
}

export type MatrixColumn = { key: string; title: string; field: IbvField }
export type MatrixGroupRow = { path: string; rowLabel: string }
export type MatrixGroup = {
  label: string
  icd10: string
  path: string
  rows: MatrixGroupRow[]
}
export type SectionMatrix = {
  rowHeader: string
  showGroupColumn: boolean
  hasIcd: boolean
  rowLabelHeader: string
  columns: MatrixColumn[]
  groupColumns: MatrixColumn[]
  groups: MatrixGroup[]
}

/** Strip a leading "<Group> — " prefix so row labels stay short. */
function shortLabel(title: string): string {
  const parts = title.split(/\s[—–-]\s/)
  return parts[parts.length - 1].trim()
}

/** Object-typed child entries of a field (i.e. its nested sub-objects). */
function objectEntries(field: IbvField): [string, IbvField][] {
  return Object.entries(field.properties ?? {}).filter(
    ([, f]) => f.type === "object" && f.properties
  )
}

/** True if every child field is a leaf (no further nesting) — a real CPT row. */
function allLeafChildren(field: IbvField): boolean {
  const children = Object.values(field.properties ?? {})
  return children.length > 0 && children.every((c) => c.type !== "object")
}

/**
 * If a section is a grouped CPT matrix — ≥1 group object, each containing ≥1
 * CPT-row sub-object whose children are all leaves, all rows sharing the same
 * coverage columns, and ≥2 rows in total — return its table model, else null.
 */
export function getSectionMatrix(section: IbvSection): SectionMatrix | null {
  const groups = Object.entries(section.properties).filter(
    ([, f]) => f.type === "object" && f.properties
  )
  if (groups.length === 0) return null

  const firstRows = objectEntries(groups[0][1])
  if (firstRows.length === 0) return null

  const colKeys = Object.keys(firstRows[0][1].properties ?? {}).join("|")
  if (colKeys === "") return null

  let totalRows = 0
  for (const [, g] of groups) {
    const rows = objectEntries(g)
    if (rows.length === 0) return null
    for (const [, r] of rows) {
      if (!allLeafChildren(r)) return null
      if (Object.keys(r.properties ?? {}).join("|") !== colKeys) return null
    }
    totalRows += rows.length
  }
  if (totalRows < 2) return null

  const columns: MatrixColumn[] = Object.entries(
    firstRows[0][1].properties ?? {}
  ).map(([key, field]) => ({ key, title: field.title, field }))

  const groupColumns: MatrixColumn[] = Object.entries(groups[0][1].properties ?? {})
    .filter(([, f]) => f.type !== "object" && f.prompt_role !== "prose")
    .map(([key, field]) => ({ key, title: field.title, field }))

  const groupsOut: MatrixGroup[] = groups.map(([gKey, g]) => ({
    label: shortLabel(g.title),
    icd10: g.icd10 ?? "",
    path: `${section.section_key}.${gKey}`,
    rows: objectEntries(g).map(([rKey, r]) => ({
      path: `${section.section_key}.${gKey}.${rKey}`,
      rowLabel: r.title,
    })),
  }))

  const hasIcd = groupsOut.some((g) => g.icd10)
  const rowLabelHeader = hasIcd ? "CPT Code" : ""
  const showGroupColumn = groups.length > 1 || hasIcd
  const rowHeader =
    section.row_header ||
    (showGroupColumn
      ? groups[0][1].title.split(/\s[—–-]\s/)[0].trim()
      : groupsOut[0].label) ||
    section.title

  return {
    rowHeader,
    showGroupColumn,
    hasIcd,
    rowLabelHeader,
    columns,
    groupColumns,
    groups: groupsOut,
  }
}

/** All leaf (non-group) fields across the whole schema. */
export function allLeafFields(): FlatField[] {
  return schema.sections.flatMap((s) =>
    flattenSection(s).filter((f) => f.field.type !== "object")
  )
}

/** Required leaf field paths — used for completion %. */
export function requiredPaths(): string[] {
  return allLeafFields()
    .filter((f) => f.field.required_state === "required")
    .map((f) => f.path)
}

/** 0–100 completion based on filled required fields. */
export function completionPercent(values: FormValues): number {
  const req = requiredPaths()
  if (req.length === 0) return 100
  const filled = req.filter((p) => (values[p] ?? "").trim() !== "").length
  return Math.round((filled / req.length) * 100)
}

/** Sections that are context-only (rendered read-mostly on the right rail). */
const RAIL_SECTIONS = new Set([
  "patient_information",
  "appointment_information",
  "verification_information",
  "hospital_information",
])

export type SectionPlacement = "rail" | "main"

/** Where a section renders: the right reference rail or the main column. */
export function sectionPlacement(sectionKey: string): SectionPlacement {
  return RAIL_SECTIONS.has(sectionKey) ? "rail" : "main"
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npx vitest run src/lib/ibv/schema.test.ts`
Expected: PASS (all describe blocks green).

- [ ] **Step 5: Commit**

```bash
git add src/lib/ibv/schema.ts src/lib/ibv/schema.test.ts
git commit -m "feat(ibv): schema helpers + section placement (TDD)"
```

---

## Task 4: zod validation from the schema (TDD)

**Files:**
- Create: `src/lib/ibv/validation.ts`
- Create: `src/lib/ibv/validation.test.ts`

- [ ] **Step 1: Write the failing test**

Create `src/lib/ibv/validation.test.ts`:

```ts
import { describe, expect, it } from "vitest"

import { validateAll } from "./validation"

describe("validateAll", () => {
  it("flags an empty form's required fields as errors", () => {
    const errors = validateAll({})
    expect(errors["patient_information.patient_name"]).toBeTruthy()
    expect(errors["insurance_information.policy_number"]).toBeTruthy()
  })

  it("clears an error once the required field is filled", () => {
    const errors = validateAll({
      "patient_information.patient_name": "Sarah Johnson",
    })
    expect(errors["patient_information.patient_name"]).toBeUndefined()
  })

  it("does not flag optional fields", () => {
    const errors = validateAll({})
    expect(errors["patient_information.chart_number"]).toBeUndefined()
  })

  it("applies a conditional-required rule (spouse name when coverage is Family)", () => {
    const family = validateAll({ "benefit_coverage.coverage_type": "Family" })
    expect(family["patient_information.spouse_partner_name"]).toBeTruthy()

    const individual = validateAll({
      "benefit_coverage.coverage_type": "Individual",
    })
    expect(
      individual["patient_information.spouse_partner_name"]
    ).toBeUndefined()
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npx vitest run src/lib/ibv/validation.test.ts`
Expected: FAIL — `validateAll` not found.

- [ ] **Step 3: Implement `validation.ts`**

Create `src/lib/ibv/validation.ts`:

```ts
import { z } from "zod"

import { allLeafFields, schema } from "./schema"
import type { FieldRule, FormValues, IbvField } from "./types"

/** Errors keyed by dotted field path (absent = valid). */
export type ValidationErrors = Record<string, string>

/** A field is conditionally required if a "make this required" rule matches. */
function conditionallyRequired(field: IbvField, values: FormValues): boolean {
  const rules: FieldRule[] = field.rules ?? []
  for (const rule of rules) {
    if (!/required/i.test(rule.effect)) continue
    const conds = rule.conditions ?? []
    if (conds.length === 0) continue
    const allMatch = conds.every(
      (c) => (values[`${conditionScope(c.field)}`] ?? values[c.field]) === c.value
    )
    if (allMatch) return true
  }
  return false
}

/**
 * Rule conditions reference a bare field key (e.g. "coverage_type"). Resolve it
 * to its dotted path by scanning the schema for a leaf whose last segment matches.
 */
function conditionScope(fieldKey: string): string {
  for (const f of allLeafFields()) {
    if (f.path.endsWith(`.${fieldKey}`) || f.path === fieldKey) return f.path
  }
  return fieldKey
}

/**
 * Validate the whole form. Required fields (static `required_state` or matched
 * conditional rule) must be non-empty strings. Returns a path→message map.
 */
export function validateAll(values: FormValues): ValidationErrors {
  const shape: Record<string, z.ZodTypeAny> = {}
  const leaves = allLeafFields()

  for (const { path, field } of leaves) {
    const required =
      field.required_state === "required" ||
      conditionallyRequired(field, values)
    shape[path] = required
      ? z.string().trim().min(1, { message: `${field.title} is required` })
      : z.string().optional()
  }

  // zod treats dotted keys as flat keys here (we pass a flat object), so build a
  // record schema and validate the flat values object directly.
  const result = z.object(shape).safeParse(
    Object.fromEntries(leaves.map(({ path }) => [path, values[path] ?? ""]))
  )

  const errors: ValidationErrors = {}
  if (!result.success) {
    for (const issue of result.error.issues) {
      const key = String(issue.path[0])
      if (key && !(key in errors)) errors[key] = issue.message
    }
  }
  return errors
}

/** Validate only the required fields belonging to one section. */
export function validateSection(
  sectionKey: string,
  values: FormValues
): ValidationErrors {
  const all = validateAll(values)
  const prefix = `${sectionKey}.`
  return Object.fromEntries(
    Object.entries(all).filter(([p]) => p.startsWith(prefix))
  )
}

export { schema as validationSchema }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npx vitest run src/lib/ibv/validation.test.ts`
Expected: PASS.

> Note: if the conditional-required test fails because the schema's spouse rule
> references `coverage_type` and `conditionScope` resolves it correctly, confirm by
> logging `conditionScope("coverage_type")` — it should return
> `benefit_coverage.coverage_type`. The test asserts the documented behavior; the
> implementation above resolves bare keys to dotted paths.

- [ ] **Step 5: Commit**

```bash
git add src/lib/ibv/validation.ts src/lib/ibv/validation.test.ts
git commit -m "feat(ibv): zod validation with conditional-required rules (TDD)"
```

---

## Task 5: Inline dispute model + helpers (TDD)

**Files:**
- Modify (rewrite): `src/lib/ibv/disputes.ts`
- Modify (rewrite): `src/lib/ibv/disputes.test.ts`

- [ ] **Step 1: Write the failing test**

Replace the entire contents of `src/lib/ibv/disputes.test.ts`:

```ts
import { describe, expect, it } from "vitest"

import {
  mockDisputes,
  humanizeLabel,
  confidenceLevel,
  confidenceHighlightClass,
  defaultFlags,
  activeDisputeValue,
  badgeValue,
  toggleApplied,
  toggleSwapped,
  toggleReasked,
  applyAllFlags,
  buildSavePayload,
  seedValues,
  type Dispute,
  type DisputeMap,
  type DisputeFlagMap,
} from "./disputes"

const d: Dispute = { previousValue: "No", currentValue: "Yes", confidence: 95 }

describe("humanizeLabel", () => {
  it("title-cases snake_case joined by ›", () => {
    expect(humanizeLabel("insurance_information.health_plan")).toBe(
      "Insurance Information › Health Plan"
    )
  })
})

describe("confidenceLevel", () => {
  it("maps scores at documented thresholds", () => {
    expect(confidenceLevel(100)).toBe("high")
    expect(confidenceLevel(95)).toBe("medium")
    expect(confidenceLevel(85)).toBe("low")
    expect(confidenceLevel(50)).toBe("very-low")
    expect(confidenceLevel(undefined)).toBe("unknown")
  })
})

describe("confidenceHighlightClass", () => {
  it("returns distinct classes per level", () => {
    expect(confidenceHighlightClass(100)).not.toBe(confidenceHighlightClass(50))
    expect(confidenceHighlightClass(95)).toBeTruthy()
  })
})

describe("active/badge values + flags", () => {
  it("defaults: active = current, badge = previous", () => {
    const f = defaultFlags()
    expect(activeDisputeValue(d, f)).toBe("Yes")
    expect(badgeValue(d, f)).toBe("No")
  })

  it("swapped: active = previous, badge = current", () => {
    const f = toggleSwapped(defaultFlags())
    expect(activeDisputeValue(d, f)).toBe("No")
    expect(badgeValue(d, f)).toBe("Yes")
  })

  it("toggleApplied flips applied", () => {
    expect(toggleApplied(defaultFlags()).applied).toBe(true)
    expect(toggleApplied(toggleApplied(defaultFlags())).applied).toBe(false)
  })

  it("toggleReasked flips reasked", () => {
    expect(toggleReasked(defaultFlags()).reasked).toBe(true)
  })
})

describe("applyAllFlags", () => {
  it("marks every dispute path applied (and not swapped)", () => {
    const flags: DisputeFlagMap = {
      "a.b": { applied: false, swapped: true, reasked: false },
    }
    const disputes: DisputeMap = {
      "a.b": { previousValue: "1", currentValue: "2" },
      "c.d": { previousValue: "3", currentValue: "4" },
    }
    const next = applyAllFlags(disputes, flags)
    expect(next["a.b"].applied).toBe(true)
    expect(next["a.b"].swapped).toBe(false)
    expect(next["c.d"].applied).toBe(true)
  })
})

describe("buildSavePayload", () => {
  const disputes: DisputeMap = {
    "insurance_information.health_plan": {
      previousValue: "BCBS TX",
      currentValue: "Blue Cross Blue Shield",
    },
    "benefit_coverage.coverage_type": {
      previousValue: "Individual",
      currentValue: "Family",
    },
  }

  it("lists applied dispute paths and reasked paths", () => {
    const values = seedValues(disputes)
    const flags: DisputeFlagMap = {
      "insurance_information.health_plan": {
        applied: true,
        swapped: false,
        reasked: false,
      },
      "benefit_coverage.coverage_type": {
        applied: false,
        swapped: false,
        reasked: true,
      },
    }
    const payload = buildSavePayload(values, disputes, flags)
    expect(payload.disputeFields).toEqual(["insurance_information.health_plan"])
    expect(payload.reaskedFields).toEqual(["benefit_coverage.coverage_type"])
    expect(payload.formData["insurance_information.health_plan"]).toBe(
      "Blue Cross Blue Shield"
    )
  })
})

describe("mockDisputes integrity", () => {
  it("every dispute has two distinct values and a dotted path", () => {
    for (const [path, dd] of Object.entries(mockDisputes)) {
      expect(path).toMatch(/^[a-z0-9_]+(\.[a-z0-9_]+)+$/)
      expect(dd.previousValue).not.toBe(dd.currentValue)
    }
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npx vitest run src/lib/ibv/disputes.test.ts`
Expected: FAIL — new exports not found.

- [ ] **Step 3: Rewrite `disputes.ts`**

Replace the entire contents of `src/lib/ibv/disputes.ts`:

```ts
// Inline dispute model (smart-caller-fe parity): a field where the assistant
// captured a value (currentValue) that disagrees with the prior value
// (previousValue). The captured value is pre-seeded into the field; per-field
// flags track the apply (✓ / ↶), swap (⇄) and Re-Ask interactions. Pure helpers
// here are unit-tested in disputes.test.ts.

import type { FormValues } from "./types"

export type Dispute = {
  /** the prior / original value (shown in the badge by default) */
  previousValue: string
  /** the assistant-captured value (pre-seeded into the field) */
  currentValue: string
  /** 0–100 confidence in the captured value */
  confidence?: number
  /** short supporting evidence */
  evidence?: string
  /** model reasoning */
  reasoning?: string
}

/** Disputes keyed by dotted field path. */
export type DisputeMap = Record<string, Dispute>

/** Per-field interaction flags. */
export type DisputeFlags = {
  /** the dispute has been confirmed/applied (resolved) */
  applied: boolean
  /** the input shows the prior value instead of the captured value */
  swapped: boolean
  /** flagged for re-verification on the next call */
  reasked: boolean
}

export type DisputeFlagMap = Record<string, DisputeFlags>

export function defaultFlags(): DisputeFlags {
  return { applied: false, swapped: false, reasked: false }
}

/** The value currently "primary" for the field (what Apply would commit). */
export function activeDisputeValue(d: Dispute, f: DisputeFlags): string {
  return f.swapped ? d.previousValue : d.currentValue
}

/** The value shown in the badge (the non-active alternative). */
export function badgeValue(d: Dispute, f: DisputeFlags): string {
  return f.swapped ? d.currentValue : d.previousValue
}

export function toggleApplied(f: DisputeFlags): DisputeFlags {
  return { ...f, applied: !f.applied }
}
export function toggleSwapped(f: DisputeFlags): DisputeFlags {
  return { ...f, swapped: !f.swapped }
}
export function toggleReasked(f: DisputeFlags): DisputeFlags {
  return { ...f, reasked: !f.reasked }
}

/** Mark every dispute path applied (non-swapped) — for "Resolve all disputes". */
export function applyAllFlags(
  disputes: DisputeMap,
  flags: DisputeFlagMap
): DisputeFlagMap {
  const next: DisputeFlagMap = { ...flags }
  for (const path of Object.keys(disputes)) {
    next[path] = { ...(next[path] ?? defaultFlags()), applied: true, swapped: false }
  }
  return next
}

export type SavePayload = {
  formData: FormValues
  /** paths whose dispute was applied/resolved */
  disputeFields: string[]
  /** paths flagged for re-ask */
  reaskedFields: string[]
}

/** Build the per-person save payload from current values + dispute flags. */
export function buildSavePayload(
  values: FormValues,
  disputes: DisputeMap,
  flags: DisputeFlagMap
): SavePayload {
  const disputeFields: string[] = []
  const reaskedFields: string[] = []
  for (const path of Object.keys(disputes)) {
    const f = flags[path] ?? defaultFlags()
    if (f.applied) disputeFields.push(path)
    if (f.reasked) reaskedFields.push(path)
  }
  return { formData: { ...values }, disputeFields, reaskedFields }
}

/** Pre-seed field values with each dispute's captured (current) value. */
export function seedValues(disputes: DisputeMap): FormValues {
  const out: FormValues = {}
  for (const [path, d] of Object.entries(disputes)) out[path] = d.currentValue
  return out
}

/** "insurance_information.health_plan" -> "Insurance Information › Health Plan" */
export function humanizeLabel(path: string): string {
  return path
    .split(".")
    .filter(Boolean)
    .map((seg) => seg.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()))
    .join(" › ")
}

export type ConfidenceLevel = "high" | "medium" | "low" | "very-low" | "unknown"

/** Map a confidence score to a level (matches smart-caller-fe thresholds). */
export function confidenceLevel(score?: number): ConfidenceLevel {
  if (score === undefined || score === null) return "unknown"
  if (score >= 100) return "high"
  if (score >= 90) return "medium"
  if (score >= 80) return "low"
  return "very-low"
}

/** Tailwind border+bg classes for an unresolved disputed field, by confidence. */
export function confidenceHighlightClass(score?: number): string {
  switch (confidenceLevel(score)) {
    case "high":
      return "border-teal-500 bg-teal-50"
    case "medium":
      return "border-amber-500 bg-amber-50"
    case "low":
      return "border-orange-500 bg-orange-50"
    case "very-low":
      return "border-red-500 bg-red-50"
    default:
      return "border-amber-400 bg-amber-50"
  }
}

/** Tailwind classes for the small confidence chip in the tooltip. */
export function confidenceChipClass(score?: number): string {
  switch (confidenceLevel(score)) {
    case "high":
      return "bg-teal-500 text-white"
    case "medium":
      return "bg-amber-500 text-white"
    case "low":
      return "bg-orange-500 text-white"
    case "very-low":
      return "bg-red-500 text-white"
    default:
      return "bg-muted text-muted-foreground"
  }
}

/** Demo disputes on real field paths (insurance text, a dropdown, a matrix cell). */
export const mockDisputes: DisputeMap = {
  "insurance_information.health_plan": {
    previousValue: "BCBS TX",
    currentValue: "Blue Cross Blue Shield",
    confidence: 95,
    evidence: "Carrier portal listed the full plan name.",
    reasoning: "Plan name updated from the carrier portal during the call.",
  },
  "insurance_information.policy_number": {
    previousValue: "841560981",
    currentValue: "841560982",
    confidence: 88,
    evidence: "Rep read the number back.",
    reasoning: "Final digit corrected when the rep read it back.",
  },
  "benefit_coverage.coverage_type": {
    previousValue: "Individual",
    currentValue: "Family",
    confidence: 72,
    evidence: "Rep confirmed dependents on the plan.",
    reasoning: "Rep confirmed the plan is family coverage.",
  },
  "general_coverage.office_visits.cpt_1.covered": {
    previousValue: "No",
    currentValue: "Yes",
    confidence: 99,
    evidence: "Confirmed covered for CPT 99211.",
    reasoning: "Office visit is a covered benefit.",
  },
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npx vitest run src/lib/ibv/disputes.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lib/ibv/disputes.ts src/lib/ibv/disputes.test.ts
git commit -m "feat(ibv): inline dispute model + helpers (TDD)"
```

---

## Task 6: Mock people, per-person disputes, and save

**Files:**
- Modify (rewrite): `src/lib/ibv/mock.ts`

- [ ] **Step 1: Rewrite `mock.ts`**

Replace the entire contents of `src/lib/ibv/mock.ts`:

```ts
import { mockDisputes, type DisputeMap } from "./disputes"
import type { InsuredPerson } from "./types"
import type { SavePayload } from "./disputes"

/** Mock insured members. Swap for backend `GET /case/:id/people` later. */
export const mockPeople: InsuredPerson[] = [
  { id: "p1", name: "Sarah Johnson", relationship: "Patient" },
  { id: "p2", name: "Michael Johnson", relationship: "Spouse" },
  { id: "p3", name: "Emma Johnson", relationship: "Dependent" },
  { id: "p4", name: "David Martinez", relationship: "Patient" },
]

/** Disputes seeded for the first person only (demo). */
export const disputesByPerson: Record<string, DisputeMap> = {
  [mockPeople[0].id]: mockDisputes,
}

export type SaveResult = { ok: true; savedAt: string }

/**
 * Mock save. Same shape the backend will return from
 * `POST {VITE_API_URL}/ibv/forms` — swapping is a one-line change here.
 */
export async function saveIbvForms(
  _payload: Record<string, SavePayload>
): Promise<SaveResult> {
  await new Promise((r) => setTimeout(r, 600))
  return { ok: true, savedAt: new Date().toISOString() }
}
```

- [ ] **Step 2: Verify typecheck**

Run: `npx tsc --noEmit -p tsconfig.app.json`
Expected: errors only from `IbvProvider.tsx` / UI files not yet updated. `mock.ts` itself must be clean.

- [ ] **Step 3: Commit**

```bash
git add src/lib/ibv/mock.ts
git commit -m "feat(ibv): per-person mock disputes + save payload shape"
```

---

## Task 7: Rewrite IbvProvider (state + disputes + validation)

**Files:**
- Modify (rewrite): `src/components/ibv/IbvProvider.tsx`

- [ ] **Step 1: Rewrite `IbvProvider.tsx`**

Replace the entire contents of `src/components/ibv/IbvProvider.tsx`:

```tsx
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react"

import { completionPercent } from "@/lib/ibv/schema"
import { validateAll, type ValidationErrors } from "@/lib/ibv/validation"
import { mockPeople, disputesByPerson, saveIbvForms } from "@/lib/ibv/mock"
import {
  activeDisputeValue,
  applyAllFlags,
  buildSavePayload,
  defaultFlags,
  seedValues,
  toggleApplied,
  toggleReasked,
  toggleSwapped,
  type Dispute,
  type DisputeFlagMap,
  type DisputeFlags,
  type DisputeMap,
} from "@/lib/ibv/disputes"
import type { FormValues, InsuredPerson } from "@/lib/ibv/types"

type SaveState = "idle" | "saving" | "saved"
export type FormStatus = "Not started" | "In progress" | "Complete"

type IbvContextValue = {
  people: InsuredPerson[]
  completionById: Record<string, number>
  statusById: Record<string, FormStatus>
  // active form
  activeId: string
  setActiveId: (id: string) => void
  values: FormValues
  setValue: (path: string, value: string) => void
  errors: ValidationErrors
  dirty: boolean
  saveState: SaveState
  save: () => Promise<void>
  // disputes (active person)
  disputes: DisputeMap
  disputeFor: (path: string) => Dispute | undefined
  flagsFor: (path: string) => DisputeFlags
  applyDispute: (path: string) => void
  swapDispute: (path: string) => void
  reaskDispute: (path: string) => void
  resolveAll: () => void
  pendingDisputeCount: number
  // modal control
  modalOpen: boolean
  openForm: (personId?: string) => void
  closeForm: () => void
}

const IbvContext = createContext<IbvContextValue | null>(null)

function statusOf(pct: number): FormStatus {
  if (pct === 0) return "Not started"
  if (pct === 100) return "Complete"
  return "In progress"
}

export function IbvProvider({ children }: { children: ReactNode }) {
  const people = mockPeople
  const [activeId, setActiveId] = useState(people[0]?.id ?? "")

  const [valuesByPerson, setValuesByPerson] = useState<
    Record<string, FormValues>
  >(() =>
    Object.fromEntries(
      people.map((p) => [p.id, seedValues(disputesByPerson[p.id] ?? {})])
    )
  )
  const [flagsByPerson, setFlagsByPerson] = useState<
    Record<string, DisputeFlagMap>
  >(() => Object.fromEntries(people.map((p) => [p.id, {}])))
  const [dirty, setDirty] = useState(false)
  const [saveState, setSaveState] = useState<SaveState>("idle")
  const [modalOpen, setModalOpen] = useState(false)

  const values = valuesByPerson[activeId] ?? {}
  const disputes = disputesByPerson[activeId] ?? {}
  const flags = flagsByPerson[activeId] ?? {}

  const errors = useMemo(() => validateAll(values), [values])

  const setValue = useCallback(
    (path: string, value: string) => {
      setValuesByPerson((prev) => ({
        ...prev,
        [activeId]: { ...prev[activeId], [path]: value },
      }))
      setDirty(true)
      setSaveState("idle")
    },
    [activeId]
  )

  const flagsFor = useCallback(
    (path: string) => flagsByPerson[activeId]?.[path] ?? defaultFlags(),
    [activeId, flagsByPerson]
  )

  const setFlags = useCallback(
    (path: string, next: DisputeFlags) => {
      setFlagsByPerson((prev) => ({
        ...prev,
        [activeId]: { ...prev[activeId], [path]: next },
      }))
      setDirty(true)
      setSaveState("idle")
    },
    [activeId]
  )

  const applyDispute = useCallback(
    (path: string) => setFlags(path, toggleApplied(flagsFor(path))),
    [flagsFor, setFlags]
  )

  const swapDispute = useCallback(
    (path: string) => {
      const d = disputesByPerson[activeId]?.[path]
      if (!d) return
      const next = toggleSwapped(flagsFor(path))
      setFlags(path, next)
      setValue(path, activeDisputeValue(d, next))
    },
    [activeId, flagsFor, setFlags, setValue]
  )

  const reaskDispute = useCallback(
    (path: string) => setFlags(path, toggleReasked(flagsFor(path))),
    [flagsFor, setFlags]
  )

  const resolveAll = useCallback(() => {
    const personDisputes = disputesByPerson[activeId] ?? {}
    setFlagsByPerson((prev) => ({
      ...prev,
      [activeId]: applyAllFlags(personDisputes, prev[activeId] ?? {}),
    }))
    setValuesByPerson((prev) => {
      const nextValues = { ...prev[activeId] }
      for (const [path, d] of Object.entries(personDisputes)) {
        nextValues[path] = d.currentValue
      }
      return { ...prev, [activeId]: nextValues }
    })
    setDirty(true)
    setSaveState("idle")
  }, [activeId])

  const disputeFor = useCallback(
    (path: string) => disputesByPerson[activeId]?.[path],
    [activeId]
  )

  const pendingDisputeCount = useMemo(
    () =>
      Object.keys(disputes).filter((p) => !(flags[p]?.applied ?? false)).length,
    [disputes, flags]
  )

  const completionById = useMemo(
    () =>
      Object.fromEntries(
        people.map((p) => [p.id, completionPercent(valuesByPerson[p.id] ?? {})])
      ),
    [people, valuesByPerson]
  )

  const statusById = useMemo(
    () =>
      Object.fromEntries(
        people.map((p) => [p.id, statusOf(completionById[p.id] ?? 0)])
      ) as Record<string, FormStatus>,
    [people, completionById]
  )

  const save = useCallback(async () => {
    setSaveState("saving")
    const payload = Object.fromEntries(
      people.map((p) => [
        p.id,
        buildSavePayload(
          valuesByPerson[p.id] ?? {},
          disputesByPerson[p.id] ?? {},
          flagsByPerson[p.id] ?? {}
        ),
      ])
    )
    await saveIbvForms(payload)
    setDirty(false)
    setSaveState("saved")
  }, [people, valuesByPerson, flagsByPerson])

  const openForm = useCallback((personId?: string) => {
    if (personId) setActiveId(personId)
    setModalOpen(true)
  }, [])
  const closeForm = useCallback(() => setModalOpen(false), [])

  const value: IbvContextValue = {
    people,
    completionById,
    statusById,
    activeId,
    setActiveId,
    values,
    setValue,
    errors,
    dirty,
    saveState,
    save,
    disputes,
    disputeFor,
    flagsFor,
    applyDispute,
    swapDispute,
    reaskDispute,
    resolveAll,
    pendingDisputeCount,
    modalOpen,
    openForm,
    closeForm,
  }

  return <IbvContext.Provider value={value}>{children}</IbvContext.Provider>
}

export function useIbv() {
  const ctx = useContext(IbvContext)
  if (!ctx) throw new Error("useIbv must be used within <IbvProvider>")
  return ctx
}
```

- [ ] **Step 2: Verify typecheck**

Run: `npx tsc --noEmit -p tsconfig.app.json`
Expected: errors only from UI files that import the old provider API (`resolveDispute`, `DisputeReviewPanel`, etc.) — fixed in later tasks. `IbvProvider.tsx` itself must be clean.

- [ ] **Step 3: Commit**

```bash
git add src/components/ibv/IbvProvider.tsx
git commit -m "feat(ibv): rewrite provider for inline disputes + validation"
```

---

## Task 8: Dispute UI controls

**Files:**
- Create: `src/components/ibv/DisputeControls.tsx`

- [ ] **Step 1: Create `DisputeControls.tsx`**

Create `src/components/ibv/DisputeControls.tsx`:

```tsx
import { Check, RotateCcw, ArrowLeftRight, Repeat } from "lucide-react"

import { cn } from "@/lib/utils"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  confidenceChipClass,
  confidenceLevel,
  type Dispute,
} from "@/lib/ibv/disputes"

/** ✓ apply (teal) → ↶ unapply (green) for a disputed field. */
export function ApplyButton({
  applied,
  onClick,
}: {
  applied: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={applied ? "Unapply" : "Apply captured value"}
      className={cn(
        "inline-flex size-5 items-center justify-center rounded-full text-white transition-colors",
        applied ? "bg-emerald-500 hover:bg-emerald-600" : "bg-teal-600 hover:bg-teal-700"
      )}
    >
      {applied ? <RotateCcw className="size-3" /> : <Check className="size-3" />}
    </button>
  )
}

/** ⇄ swap the input value with the prior value. */
export function SwapButton({
  swapped,
  onClick,
}: {
  swapped: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title="Swap with prior value"
      className={cn(
        "inline-flex size-5 items-center justify-center rounded-full text-white transition-colors",
        swapped ? "bg-cyan-500 hover:bg-cyan-600" : "bg-[#003e64] hover:bg-[#024a78]"
      )}
    >
      <ArrowLeftRight className="size-3" />
    </button>
  )
}

/** "Re-Ask" toggle — flag the field for re-verification. */
export function ReAskButton({
  reasked,
  onClick,
}: {
  reasked: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title="Flag for re-ask"
      className={cn(
        "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium transition-colors",
        reasked ? "bg-emerald-600 text-white" : "bg-muted text-muted-foreground hover:bg-muted/80"
      )}
    >
      <Repeat className="size-2.5" />
      {reasked ? "Re-Ask ✓" : "Re-Ask"}
    </button>
  )
}

/** A small badge showing the alternative (prior/captured) value, with a tooltip. */
export function DisputeBadge({
  value,
  dispute,
  label = "Prior",
}: {
  value: string
  dispute: Dispute
  label?: string
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex max-w-[120px] items-center gap-1 truncate rounded border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-800">
          <span className="font-medium">{label}:</span>
          <span className="truncate">{value || "—"}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent>
        <DisputeTooltipBody dispute={dispute} />
      </TooltipContent>
    </Tooltip>
  )
}

/** Tooltip body: confidence chip + evidence + reasoning. */
export function DisputeTooltipBody({ dispute }: { dispute: Dispute }) {
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2">
        <span
          className={cn(
            "rounded px-1.5 py-0.5 text-[10px] font-semibold",
            confidenceChipClass(dispute.confidence)
          )}
        >
          {dispute.confidence ?? "—"}% · {confidenceLevel(dispute.confidence)}
        </span>
      </div>
      <div>
        <span className="font-medium">Prior:</span> {dispute.previousValue}
      </div>
      <div>
        <span className="font-medium">Captured:</span> {dispute.currentValue}
      </div>
      {dispute.evidence && (
        <div>
          <span className="font-medium">Evidence:</span> {dispute.evidence}
        </div>
      )}
      {dispute.reasoning && (
        <div>
          <span className="font-medium">Reasoning:</span> {dispute.reasoning}
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 2: Verify typecheck**

Run: `npx tsc --noEmit -p tsconfig.app.json`
Expected: `DisputeControls.tsx` clean (other UI files may still error until updated).

- [ ] **Step 3: Commit**

```bash
git add src/components/ibv/DisputeControls.tsx
git commit -m "feat(ibv): inline dispute controls (apply/swap/re-ask/badge/tooltip)"
```

---

## Task 9: FieldRenderer (widget switch)

**Files:**
- Modify (rewrite): `src/components/ibv/FieldRenderer.tsx`

- [ ] **Step 1: Rewrite `FieldRenderer.tsx`**

Replace the entire contents of `src/components/ibv/FieldRenderer.tsx`:

```tsx
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { cn } from "@/lib/utils"
import { resolveOptions, widgetOf } from "@/lib/ibv/schema"
import type { IbvField } from "@/lib/ibv/types"

type Props = {
  field: IbvField
  path: string
  value: string
  onChange: (value: string) => void
  invalid?: boolean
  highlightClass?: string
  /** extra right padding so inline dispute controls don't overlap the text */
  inputPaddingRight?: string
}

/** Renders just the input control for a field, switching on ui.widget. */
export function FieldRenderer({
  field,
  path,
  value,
  onChange,
  invalid,
  highlightClass,
  inputPaddingRight,
}: Props) {
  const widget = widgetOf(field)

  if (field.confirm_only) {
    return (
      <div className="flex h-8 items-center px-2.5 text-sm text-muted-foreground">
        {value || "—"}
      </div>
    )
  }

  if (widget === "radio") {
    const options = resolveOptions(field)
    return (
      <RadioGroup
        value={value}
        onValueChange={onChange}
        className={cn(
          "flex flex-wrap gap-x-4 gap-y-1 px-2.5 py-1.5",
          highlightClass && `rounded-md border ${highlightClass}`
        )}
      >
        {options.map((opt) => (
          <label
            key={opt}
            className="flex items-center gap-1.5 text-sm"
            htmlFor={`${path}-${opt}`}
          >
            <RadioGroupItem id={`${path}-${opt}`} value={opt} />
            {opt}
          </label>
        ))}
      </RadioGroup>
    )
  }

  if (widget === "textarea") {
    return (
      <Textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-invalid={invalid}
        className={cn("min-h-16 rounded-none border-0 shadow-none focus-visible:ring-1", highlightClass)}
      />
    )
  }

  return (
    <Input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-invalid={invalid}
      style={inputPaddingRight ? { paddingRight: inputPaddingRight } : undefined}
      className={cn(
        "h-8 rounded-none border-0 shadow-none focus-visible:ring-1",
        highlightClass
      )}
    />
  )
}
```

- [ ] **Step 2: Verify typecheck**

Run: `npx tsc --noEmit -p tsconfig.app.json`
Expected: `FieldRenderer.tsx` clean.

- [ ] **Step 3: Commit**

```bash
git add src/components/ibv/FieldRenderer.tsx
git commit -m "feat(ibv): widget-switching FieldRenderer"
```

---

## Task 10: FieldRow (label + input + inline disputes)

**Files:**
- Create: `src/components/ibv/FieldRow.tsx`

- [ ] **Step 1: Create `FieldRow.tsx`**

Create `src/components/ibv/FieldRow.tsx`:

```tsx
import { cn } from "@/lib/utils"
import { useIbv } from "./IbvProvider"
import { FieldRenderer } from "./FieldRenderer"
import {
  ApplyButton,
  ReAskButton,
  SwapButton,
  DisputeBadge,
} from "./DisputeControls"
import { badgeValue, confidenceHighlightClass } from "@/lib/ibv/disputes"
import { sectionPlacement } from "@/lib/ibv/schema"
import type { IbvField } from "@/lib/ibv/types"

type Props = {
  field: IbvField
  path: string
  depth: number
}

/** One dense form row: ~180px label cell + input cell + inline dispute controls. */
export function FieldRow({ field, path, depth }: Props) {
  const {
    values,
    setValue,
    errors,
    disputeFor,
    flagsFor,
    applyDispute,
    swapDispute,
    reaskDispute,
  } = useIbv()

  const value = values[path] ?? ""
  const error = errors[path]
  const dispute = disputeFor(path)
  const flags = flagsFor(path)
  const required = field.required_state === "required"
  const sectionKey = path.split(".")[0]
  const allowReask = sectionPlacement(sectionKey) === "main"

  // Highlight + badge only while an unresolved dispute is present.
  const showDispute = !!dispute && !flags.applied
  const highlightClass = showDispute
    ? confidenceHighlightClass(dispute!.confidence)
    : undefined

  return (
    <div className="flex border-b border-[#EAECF0] last:border-b-0">
      <div
        className={cn(
          "flex w-[180px] min-w-[180px] items-center gap-1 border-r border-[#EAECF0] bg-muted/30 px-2.5 py-1.5 text-xs font-medium",
          error && "text-destructive"
        )}
        style={depth > 0 ? { paddingLeft: 10 + depth * 12 } : undefined}
      >
        <span className="truncate">{field.title}</span>
        {required && <span className="text-destructive">*</span>}
        {dispute && allowReask && (
          <span className="ml-auto">
            <ReAskButton reasked={flags.reasked} onClick={() => reaskDispute(path)} />
          </span>
        )}
      </div>

      <div className="relative flex-1">
        <FieldRenderer
          field={field}
          path={path}
          value={value}
          onChange={(v) => setValue(path, v)}
          invalid={!!error}
          highlightClass={highlightClass}
          inputPaddingRight={showDispute ? "150px" : undefined}
        />
        {showDispute && (
          <div className="absolute top-1/2 right-1.5 flex -translate-y-1/2 items-center gap-1">
            <SwapButton swapped={flags.swapped} onClick={() => swapDispute(path)} />
            <ApplyButton applied={flags.applied} onClick={() => applyDispute(path)} />
            <DisputeBadge
              value={badgeValue(dispute!, flags)}
              dispute={dispute!}
              label={flags.swapped ? "Captured" : "Prior"}
            />
          </div>
        )}
        {error && (
          <div className="px-2.5 pb-1 text-[10px] text-destructive">{error}</div>
        )}
      </div>
    </div>
  )
}
```

- [ ] **Step 2: Verify typecheck**

Run: `npx tsc --noEmit -p tsconfig.app.json`
Expected: `FieldRow.tsx` clean.

- [ ] **Step 3: Commit**

```bash
git add src/components/ibv/FieldRow.tsx
git commit -m "feat(ibv): dense FieldRow with inline dispute controls"
```

---

## Task 11: SectionMatrix (CPT tables)

**Files:**
- Create: `src/components/ibv/SectionMatrix.tsx`

- [ ] **Step 1: Create `SectionMatrix.tsx`**

Create `src/components/ibv/SectionMatrix.tsx`:

```tsx
import { cn } from "@/lib/utils"
import { useIbv } from "./IbvProvider"
import { FieldRenderer } from "./FieldRenderer"
import {
  ApplyButton,
  SwapButton,
  DisputeBadge,
} from "./DisputeControls"
import { badgeValue, confidenceHighlightClass } from "@/lib/ibv/disputes"
import type { SectionMatrix as SectionMatrixModel } from "@/lib/ibv/schema"

/** One editable matrix cell at `${rowPath}.${colKey}` with inline dispute UI. */
function MatrixCell({ rowPath, colKey, field }: {
  rowPath: string
  colKey: string
  field: SectionMatrixModel["columns"][number]["field"]
}) {
  const {
    values,
    setValue,
    disputeFor,
    flagsFor,
    applyDispute,
    swapDispute,
  } = useIbv()
  const path = `${rowPath}.${colKey}`
  const value = values[path] ?? ""
  const dispute = disputeFor(path)
  const flags = flagsFor(path)
  const showDispute = !!dispute && !flags.applied
  const highlightClass = showDispute
    ? confidenceHighlightClass(dispute!.confidence)
    : undefined

  return (
    <td className="border border-[#EAECF0] p-0 align-middle">
      <div className="relative">
        <FieldRenderer
          field={field}
          path={path}
          value={value}
          onChange={(v) => setValue(path, v)}
          highlightClass={highlightClass}
          inputPaddingRight={showDispute ? "70px" : undefined}
        />
        {showDispute && (
          <div className="absolute top-1/2 right-1 flex -translate-y-1/2 items-center gap-0.5">
            <SwapButton swapped={flags.swapped} onClick={() => swapDispute(path)} />
            <ApplyButton applied={flags.applied} onClick={() => applyDispute(path)} />
          </div>
        )}
        {showDispute && (
          <div className="px-1 pb-0.5">
            <DisputeBadge
              value={badgeValue(dispute!, flags)}
              dispute={dispute!}
              label={flags.swapped ? "Captured" : "Prior"}
            />
          </div>
        )}
      </div>
    </td>
  )
}

/** Renders a CPT coverage table from a SectionMatrix model. */
export function SectionMatrix({ matrix }: { matrix: SectionMatrixModel }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr className="bg-muted/50 text-left">
            {matrix.showGroupColumn && (
              <th className="border border-[#EAECF0] px-2 py-1 font-medium">
                {matrix.rowHeader}
              </th>
            )}
            {matrix.hasIcd && (
              <th className="border border-[#EAECF0] px-2 py-1 font-medium">ICD-10</th>
            )}
            <th className="border border-[#EAECF0] px-2 py-1 font-medium">
              {matrix.rowLabelHeader || "Item"}
            </th>
            {matrix.columns.map((c) => (
              <th
                key={c.key}
                className="border border-[#EAECF0] px-2 py-1 font-medium"
              >
                {c.title}
              </th>
            ))}
            {matrix.groupColumns.map((c) => (
              <th
                key={c.key}
                className="border border-[#EAECF0] px-2 py-1 font-medium"
              >
                {c.title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {matrix.groups.map((group) =>
            group.rows.map((row, rowIdx) => (
              <tr key={row.path}>
                {matrix.showGroupColumn && rowIdx === 0 && (
                  <td
                    className="border border-[#EAECF0] px-2 py-1 align-top font-medium"
                    rowSpan={group.rows.length}
                  >
                    {group.label}
                  </td>
                )}
                {matrix.hasIcd && rowIdx === 0 && (
                  <td
                    className="border border-[#EAECF0] px-2 py-1 align-top text-muted-foreground"
                    rowSpan={group.rows.length}
                  >
                    {group.icd10 || "—"}
                  </td>
                )}
                <td className={cn("border border-[#EAECF0] px-2 py-1 font-mono")}>
                  {row.rowLabel}
                </td>
                {matrix.columns.map((c) => (
                  <MatrixCell
                    key={c.key}
                    rowPath={row.path}
                    colKey={c.key}
                    field={c.field}
                  />
                ))}
                {matrix.groupColumns.map((c) =>
                  rowIdx === 0 ? (
                    <MatrixCell
                      key={c.key}
                      rowPath={group.path}
                      colKey={c.key}
                      field={c.field}
                    />
                  ) : null
                )}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  )
}
```

- [ ] **Step 2: Verify typecheck**

Run: `npx tsc --noEmit -p tsconfig.app.json`
Expected: `SectionMatrix.tsx` clean.

> Note: `groupColumns` cells use `rowSpan` semantics loosely (rendered only on the
> first row). If a section has group-level columns spanning multiple rows, this
> renders the input once on the first row; acceptable for v1.

- [ ] **Step 3: Commit**

```bash
git add src/components/ibv/SectionMatrix.tsx
git commit -m "feat(ibv): CPT matrix table with inline dispute cells"
```

---

## Task 12: Section (collapsible block)

**Files:**
- Modify (rewrite): `src/components/ibv/Section.tsx`

- [ ] **Step 1: Rewrite `Section.tsx`**

Replace the entire contents of `src/components/ibv/Section.tsx`:

```tsx
import { useState } from "react"
import { ChevronDown } from "lucide-react"

import { cn } from "@/lib/utils"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import { FieldRow } from "./FieldRow"
import { SectionMatrix } from "./SectionMatrix"
import { flattenSection, getSectionMatrix } from "@/lib/ibv/schema"
import type { IbvSection } from "@/lib/ibv/types"

/** A collapsible section: header + (field rows | CPT matrix). */
export function Section({
  section,
  defaultOpen = true,
}: {
  section: IbvSection
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  const matrix = getSectionMatrix(section)
  const rows = matrix ? [] : flattenSection(section)

  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className="overflow-hidden rounded-md border border-[#EAECF0]"
    >
      <CollapsibleTrigger className="flex w-full items-center justify-between bg-muted/60 px-3 py-2 text-left text-sm font-semibold">
        {section.title}
        <ChevronDown
          className={cn("size-4 transition-transform", open ? "" : "-rotate-90")}
        />
      </CollapsibleTrigger>
      <CollapsibleContent>
        {matrix ? (
          <SectionMatrix matrix={matrix} />
        ) : (
          <div>
            {rows.map(({ path, field, depth }) =>
              field.type === "object" ? (
                <div
                  key={path}
                  className="border-b border-[#EAECF0] bg-muted/20 px-2.5 py-1 text-xs font-semibold"
                  style={depth > 0 ? { paddingLeft: 10 + depth * 12 } : undefined}
                >
                  {field.title}
                </div>
              ) : (
                <FieldRow key={path} field={field} path={path} depth={depth} />
              )
            )}
          </div>
        )}
      </CollapsibleContent>
    </Collapsible>
  )
}
```

- [ ] **Step 2: Verify typecheck**

Run: `npx tsc --noEmit -p tsconfig.app.json`
Expected: `Section.tsx` clean.

- [ ] **Step 3: Commit**

```bash
git add src/components/ibv/Section.tsx
git commit -m "feat(ibv): collapsible Section (rows or matrix)"
```

---

## Task 13: PersonSwitcher

**Files:**
- Create: `src/components/ibv/PersonSwitcher.tsx`

- [ ] **Step 1: Create `PersonSwitcher.tsx`**

Create `src/components/ibv/PersonSwitcher.tsx`:

```tsx
import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { useIbv } from "./IbvProvider"

/** Segmented person tabs with a per-person completion badge. */
export function PersonSwitcher() {
  const { people, activeId, setActiveId, completionById } = useIbv()

  return (
    <div className="flex flex-wrap gap-1 rounded-lg bg-muted p-1">
      {people.map((p) => {
        const active = p.id === activeId
        const pct = completionById[p.id] ?? 0
        return (
          <button
            key={p.id}
            type="button"
            onClick={() => setActiveId(p.id)}
            className={cn(
              "inline-flex items-center gap-2 rounded-md px-3 py-1 text-sm font-medium transition-colors",
              active
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground"
            )}
          >
            <span className="flex flex-col items-start leading-tight">
              <span>{p.name}</span>
              <span className="text-[10px] font-normal text-muted-foreground">
                {p.relationship}
              </span>
            </span>
            <Badge variant={pct === 100 ? "default" : "secondary"}>{pct}%</Badge>
          </button>
        )
      })}
    </div>
  )
}
```

- [ ] **Step 2: Verify typecheck**

Run: `npx tsc --noEmit -p tsconfig.app.json`
Expected: `PersonSwitcher.tsx` clean.

- [ ] **Step 3: Commit**

```bash
git add src/components/ibv/PersonSwitcher.tsx
git commit -m "feat(ibv): person switcher with completion badges"
```

---

## Task 14: SchemaForm (left/right split)

**Files:**
- Create: `src/components/ibv/SchemaForm.tsx`

- [ ] **Step 1: Create `SchemaForm.tsx`**

Create `src/components/ibv/SchemaForm.tsx`:

```tsx
import { Section } from "./Section"
import { schema, sectionPlacement } from "@/lib/ibv/schema"

/** Renders schema sections into the left main column + right reference rail. */
export function SchemaForm() {
  const mainSections = schema.sections.filter(
    (s) => sectionPlacement(s.section_key) === "main"
  )
  const railSections = schema.sections.filter(
    (s) => sectionPlacement(s.section_key) === "rail"
  )

  return (
    <div className="flex gap-4">
      <div className="flex min-w-0 flex-1 flex-col gap-3">
        {mainSections.map((s) => (
          <Section key={s.section_key} section={s} />
        ))}
      </div>
      <aside className="sticky top-0 hidden w-[300px] shrink-0 flex-col gap-3 self-start lg:flex">
        {railSections.map((s) => (
          <Section key={s.section_key} section={s} />
        ))}
      </aside>
    </div>
  )
}
```

- [ ] **Step 2: Verify typecheck**

Run: `npx tsc --noEmit -p tsconfig.app.json`
Expected: `SchemaForm.tsx` clean.

- [ ] **Step 3: Commit**

```bash
git add src/components/ibv/SchemaForm.tsx
git commit -m "feat(ibv): two-column SchemaForm (main + reference rail)"
```

---

## Task 15: IbvFormModal (assemble)

**Files:**
- Modify (rewrite): `src/components/ibv/IbvFormModal.tsx`

- [ ] **Step 1: Rewrite `IbvFormModal.tsx`**

Replace the entire contents of `src/components/ibv/IbvFormModal.tsx`:

```tsx
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { useIbv } from "./IbvProvider"
import { PersonSwitcher } from "./PersonSwitcher"
import { SchemaForm } from "./SchemaForm"

export function IbvFormModal() {
  const {
    modalOpen,
    closeForm,
    dirty,
    saveState,
    save,
    resolveAll,
    pendingDisputeCount,
  } = useIbv()

  return (
    <Dialog open={modalOpen} onOpenChange={(o) => (o ? null : closeForm())}>
      <DialogContent
        showCloseButton
        className="flex max-h-[92vh] w-[96vw] max-w-[1200px] flex-col gap-0 p-0"
      >
        <DialogHeader className="gap-3 border-b border-border p-4">
          <div className="flex items-start justify-between gap-4">
            <div>
              <DialogTitle>IBV Data Entry Form</DialogTitle>
              <DialogDescription>
                Insurance Benefit Verification — review captured values and resolve
                disputes.
              </DialogDescription>
            </div>
          </div>
          <PersonSwitcher />
        </DialogHeader>

        <div className="flex-1 overflow-y-auto p-4">
          <SchemaForm />
        </div>

        <div className="flex items-center justify-between gap-4 border-t border-border p-4">
          <label className="flex items-center gap-2 text-sm">
            <Checkbox
              checked={false}
              onCheckedChange={(c) => {
                if (c) resolveAll()
              }}
              disabled={pendingDisputeCount === 0}
            />
            Resolve all disputes
            {pendingDisputeCount > 0 && (
              <span className="text-muted-foreground">
                ({pendingDisputeCount} pending)
              </span>
            )}
          </label>
          <div className="flex items-center gap-3">
            {saveState === "saved" && !dirty && (
              <span className="text-sm text-emerald-600">Saved</span>
            )}
            <Button variant="outline" onClick={closeForm}>
              Cancel
            </Button>
            <Button onClick={save} disabled={!dirty || saveState === "saving"}>
              {saveState === "saving" ? "Saving…" : "Save"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
```

- [ ] **Step 2: Verify typecheck + build**

Run: `npx tsc --noEmit -p tsconfig.app.json`
Expected: clean across the whole project (no remaining references to removed APIs).

If errors mention `SectionTable` or `DisputeReviewPanel`, proceed to Task 16 (their deletion) — but no other file should import them after this task. Grep to confirm:

Run: `npx vitest run` (sanity: all lib tests still pass)
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add src/components/ibv/IbvFormModal.tsx
git commit -m "feat(ibv): assemble modal (header/switcher/two-column/footer)"
```

---

## Task 16: Remove dead files + full verification

**Files:**
- Delete: `src/components/ibv/SectionTable.tsx`
- Delete: `src/components/ibv/DisputeReviewPanel.tsx`

- [ ] **Step 1: Confirm nothing imports the dead files**

Run: `grep -rn "SectionTable\|DisputeReviewPanel" src` (or use the editor search)
Expected: no matches outside the files themselves. If `IbvFormModal.tsx` or any other file still imports them, remove those imports first.

- [ ] **Step 2: Delete the dead files**

```bash
git rm src/components/ibv/SectionTable.tsx src/components/ibv/DisputeReviewPanel.tsx
```

- [ ] **Step 3: Full typecheck + build**

Run: `npm run build`
Expected: `tsc -b` passes and `vite build` succeeds with no errors.

- [ ] **Step 4: Run the full test suite**

Run: `npm run test`
Expected: all `src/lib/ibv/*.test.ts` suites PASS.

- [ ] **Step 5: Lint**

Run: `npm run lint`
Expected: no errors (fix any unused-import/var warnings the new files introduced).

- [ ] **Step 6: Manual verification (dev server)**

Run: `npm run dev`, open the app, click the **IBV Form** button on Live Monitoring, then confirm:
- Modal opens with the person switcher (4 members, completion badges).
- Left column shows Insurance / Benefit Coverage / coverage-table sections; right rail shows Patient / Appointment / VA Info / Hospital.
- CPT sections (General Coverage, etc.) render as tables with Covered/Copay/Coinsurance/Auth Req columns.
- Disputed fields (e.g. Insurance › Health Plan) show a confidence highlight, a Prior badge, and ✓ / ⇄ / Re-Ask controls; hovering the badge shows the tooltip (confidence/evidence/reasoning).
- Apply (✓) clears the highlight/badge and toggles to ↶; Swap (⇄) flips the input/badge values; Re-Ask toggles its label.
- "Resolve all disputes" applies all pending disputes.
- Fill a required field → completion badge updates; switch person → values persist per person.
- Save shows "Saving…" then "Saved"; the Save button disables when clean.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "chore(ibv): remove dead dispute-review/table files; verify build+tests"
```

---

## Self-review notes (author)

- **Spec coverage:** modal + two-column (Tasks 14–15), person switcher (Task 13), schema-driven rendering incl. CPT tables (Tasks 3, 11, 12), full inline dispute UX — apply/swap/re-ask/resolve-all/confidence highlight/tooltip (Tasks 5, 7, 8, 10, 11, 15), zod validation incl. conditional-required (Task 4), mock people/disputes/save (Tasks 5–6), section placement left/right (Task 3). All present.
- **Engine deviation from spec:** RHF omitted in favor of plain context + zod (documented above).
- **Type consistency:** `DisputeFlags`/`DisputeFlagMap`, `SavePayload`, `ValidationErrors`, `SectionMatrix` are defined once (Tasks 2/4/5/3) and reused unchanged by later tasks.
- **No jsdom:** UI components are verified by `tsc`/`vite build`/manual run, not component unit tests (matches the repo's pure-logic vitest convention). All TDD tasks target pure logic.
