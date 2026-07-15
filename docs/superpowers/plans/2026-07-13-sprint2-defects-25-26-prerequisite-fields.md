# Sprint-2 Defects #25 & #26 — Prerequisite Fields Highlighting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the three prerequisite fields (Appointment Date, Appointment Type, Callback Number) a distinct "prerequisite" amber/orange color category in the IBV form renderer, separate from the violet "system" category, while leaving Spouse Gender's green "context" color unchanged.

**Architecture:** Add an optional `prerequisite_fields: list[str]` DSL key to `FormSchemaDoc` (backend), populate it in `ibv_standard.py`, recompile the JSON artifact, then add a `"prerequisite"` variant to the `FieldUsage` union and `usageMeta.ts` in the frontend — checked before the `system` branch in `fieldUsageOf`. The frontend `FormSchema` type gets a matching optional `prerequisiteFields` field carrying the list from the document.

**Tech Stack:** Python 3.12, Pydantic v2, `uv`/`just` (backend); TypeScript, React 18, Tailwind CSS (frontend).

## Global Constraints

- Branch: `fix/sprint-2-defects`
- Backend gates: `uv run ruff check .` → clean; `pytest tests/unit/forms/` → all pass (freshness + round-trip); `uv run mypy packages/vera_core` → clean on forms package.
- Frontend gates: `npx tsc --noEmit` → clean; `npm run lint` → clean; `npm test` → clean.
- Never hand-edit `data/form_schemas/*.json`; run `just compile-schemas` instead.
- `compile_document` uses `exclude_defaults=True, exclude_none=True` — an empty `prerequisite_fields=[]` must be omitted from the artifact so non-IBV schemas don't drift.
- Document key order is meaningful (spec §4.1) — insert `prerequisite_fields` after `promoted_fields` in `FormSchemaDoc` field declaration order to keep compile output stable.
- `fieldUsageOf` in `schema.ts` must check `prerequisiteFields` BEFORE the existing `systemFieldPaths` check (prerequisite wins over system for those 3 fields).
- Spouse Gender path `sections.patient_information.spouse_gender` must NOT be in `prerequisiteFields` — it stays `"context"` (green). This alone resolves #26.
- Amber color choice (`bg-amber-100`, `border-amber-400`) is easily adjustable per product feedback — call it out in the commit body.

---

## File Map

### Backend (vera-backend/)

| File | Action | What changes |
|------|--------|-------------|
| `packages/vera_core/src/vera_core/forms/dsl.py` | **Modify** | Add `prerequisite_fields: list[str] = []` field to `FormSchemaDoc`; add path-resolution validation loop for it in `_validate_document` |
| `packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py` | **Modify** | Set `prerequisite_fields=[…3 paths…]` on the `FormSchemaDoc(…)` constructor call |
| `data/form_schemas/ibv_form_standard.json` | **Regenerated** | `just compile-schemas` adds `"prerequisite_fields": […]` key |
| `data/form_schemas/ibv_form_standard_v2.json` | **Regenerated** | Unchanged content (disease_only has no prerequisite_fields) |

### Frontend (vera-frontend/)

| File | Action | What changes |
|------|--------|-------------|
| `src/lib/ibv/types.ts` | **Modify** | Add `prerequisite_fields?: string[]` to `FormSchema` type |
| `src/lib/ibv/schema.ts` | **Modify** | Add `"prerequisite"` to `FieldUsage` union; add `prerequisiteFieldPaths()` helper + WeakMap cache; update `fieldUsageOf` to check prerequisite before system |
| `src/components/ibv/usageMeta.ts` | **Modify** | Add `prerequisite` entry to `USAGE_META`; insert `"prerequisite"` into `USAGE_ORDER` before `"system"` |

---

### Task 1: Backend — Add `prerequisite_fields` to `FormSchemaDoc` and validate it

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/dsl.py`
- Test: `vera-backend/tests/unit/forms/test_schema_dsl.py` (new test methods in existing file)

**Interfaces:**
- Produces: `FormSchemaDoc.prerequisite_fields: list[str]` (default `[]`, omitted from compiled artifact when empty via `exclude_defaults=True`)
- Produces: validation — every path in `prerequisite_fields` must resolve to a defined leaf in `leaves` dict; error format: `"prerequisite_fields[{i}]: {path!r} does not resolve to a leaf"`

- [ ] **Step 1: Write the failing tests**

Open `vera-backend/tests/unit/forms/test_schema_dsl.py` and add two new test methods inside the existing `TestCompiledArtifacts` class, plus a new `TestDocumentValidation` class (or append to the existing validation class if one exists). Add these at the end of the file:

```python
class TestPrerequisiteFields:
    def test_prerequisite_fields_omitted_when_empty(self) -> None:
        """Empty list must not appear in the compiled artifact."""
        doc = FormSchemaDoc.model_validate(minimal_doc())
        assert doc.prerequisite_fields == []
        artifact = compile_document(doc)
        data = json.loads(artifact)
        assert "prerequisite_fields" not in data

    def test_prerequisite_fields_present_when_set(self) -> None:
        """Non-empty list appears in compiled artifact."""
        base = minimal_doc()
        base["prerequisite_fields"] = ["sections.basics.plan_type"]
        doc = FormSchemaDoc.model_validate(base)
        assert doc.prerequisite_fields == ["sections.basics.plan_type"]
        artifact = compile_document(doc)
        data = json.loads(artifact)
        assert data["prerequisite_fields"] == ["sections.basics.plan_type"]

    def test_prerequisite_fields_bad_path_raises(self) -> None:
        """A path that doesn't resolve to a leaf must raise ValidationError."""
        base = minimal_doc()
        base["prerequisite_fields"] = ["sections.basics.nonexistent"]
        with pytest.raises(ValidationError, match="does not resolve to a leaf"):
            FormSchemaDoc.model_validate(base)

    def test_prerequisite_fields_group_path_raises(self) -> None:
        """A path that resolves to a GROUP (not a leaf) must also raise."""
        # minimal_doc has no groups, so use the ibv standard doc which does
        from vera_core.forms.catalog import SCHEMAS
        doc = SCHEMAS["infertility_treatment"][1]()
        # groups are present; use a group path
        group_paths = list(doc.group_paths())
        if group_paths:
            with pytest.raises(ValidationError, match="does not resolve to a leaf"):
                FormSchemaDoc.model_validate({
                    **json.loads(compile_document(doc)),
                    "prerequisite_fields": [group_paths[0]],
                })

    def test_ibv_standard_has_three_prerequisite_fields(self) -> None:
        """The IBV standard schema declares exactly the 3 prerequisite fields."""
        from vera_core.forms.catalog import SCHEMAS
        doc = SCHEMAS["infertility_treatment"][1]()
        assert set(doc.prerequisite_fields) == {
            "sections.appointment_information.appointment_date",
            "sections.appointment_information.appointment_type",
            "sections.verification_information.callback_number",
        }
```

Also add `import json` at the top of the test file if not already present.

- [ ] **Step 2: Run the tests to confirm they fail**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-backend
uv run pytest tests/unit/forms/test_schema_dsl.py::TestPrerequisiteFields -v
```

Expected: all 5 tests fail with `AttributeError` or `ValidationError` (field doesn't exist yet).

- [ ] **Step 3: Add `prerequisite_fields` to `FormSchemaDoc` in `dsl.py`**

Open `vera-backend/packages/vera_core/src/vera_core/forms/dsl.py`.

Locate the `FormSchemaDoc` class (around line 430). The current tail of its fields (before the walk helpers) is:

```python
    promoted_fields: PromotedFields
    # Session-wide STT vocabulary ...
    stt_key_terms: list[str] | None = None
    shared_conditions: dict[str, Condition] | None = None
    sections: dict[str, Section]
    tasks: list[Task]
    flow_rules: list[FlowRule] | None = None
    contradictions: list[Contradiction] | None = None
```

Insert `prerequisite_fields` immediately after `promoted_fields`, before `stt_key_terms`:

```python
    promoted_fields: PromotedFields
    # Root-anchored leaf paths (sections.<key>…) that the platform treats as
    # prerequisite fields for a call (e.g. appointment_date/type, callback_number).
    # Empty list → omitted from the compiled artifact (exclude_defaults=True).
    # Drives distinct UI color coding in the IBV renderer.
    prerequisite_fields: list[str] = Field(default_factory=list)
    # Session-wide STT vocabulary ...
    stt_key_terms: list[str] | None = None
```

Note: using `Field(default_factory=list)` rather than `= []` avoids pydantic's mutable-default-warning and ensures `exclude_defaults=True` correctly omits it when empty. However, `exclude_defaults=True` compares against the *default value*, not `default_factory`. For pydantic v2, when using `default_factory`, the default sentinel used by `model_dump(exclude_defaults=True)` is the factory result — an empty list — so an empty `prerequisite_fields` WILL be excluded. Verify this in step 5.

- [ ] **Step 4: Add path validation for `prerequisite_fields` in `_validate_document`**

In the same `dsl.py` file, find the `_validate_document` method. Near line 670, after the `# system fields` validation block and before the `# promoted fields` block, add:

```python
        # prerequisite fields — every path must resolve to a defined leaf
        for i, path in enumerate(self.prerequisite_fields):
            if path not in leaves:
                errors.append(
                    f"prerequisite_fields[{i}]: {path!r} does not resolve to a leaf"
                )
```

Place it after the system_fields block (around line 674) and before the promoted_fields block (around line 680).

- [ ] **Step 5: Run the tests to confirm they pass**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-backend
uv run pytest tests/unit/forms/test_schema_dsl.py::TestPrerequisiteFields -v
```

Expected: all 5 tests pass (except `test_ibv_standard_has_three_prerequisite_fields` which will still fail until Task 2 — that's OK at this stage).

- [ ] **Step 6: Run ruff + mypy to confirm types are clean**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-backend
uv run ruff check packages/vera_core/src/vera_core/forms/dsl.py
uv run mypy packages/vera_core/src/vera_core/forms/dsl.py --ignore-missing-imports
```

Expected: no errors.

- [ ] **Step 7: Commit**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-backend
git add packages/vera_core/src/vera_core/forms/dsl.py tests/unit/forms/test_schema_dsl.py
git commit -m "$(cat <<'EOF'
feat(forms/dsl): add optional prerequisite_fields key to FormSchemaDoc

Adds `prerequisite_fields: list[str] = []` to FormSchemaDoc — a list of
root-anchored leaf paths the platform treats as prerequisite fields for a
call (e.g. appointment date/type, callback number). Empty list is omitted
from the compiled artifact via exclude_defaults=True so existing schemas
don't drift. Validator checks every listed path resolves to a leaf.

Sprint-2 #25/#26.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014PNLDcrjuaRB8hhYc4sQUj
EOF
)"
```

---

### Task 2: Backend — Populate `prerequisite_fields` in ibv_standard catalog and recompile

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py`
- Regenerate: `vera-backend/data/form_schemas/ibv_form_standard.json` (via `just compile-schemas`)

**Interfaces:**
- Consumes: `FormSchemaDoc.prerequisite_fields` from Task 1
- Produces: compiled artifact with `"prerequisite_fields": ["sections.appointment_information.appointment_date", "sections.appointment_information.appointment_type", "sections.verification_information.callback_number"]`

- [ ] **Step 1: Verify the 3 exact leaf paths**

The paths are confirmed from the catalog (ibv_standard.py lines 1015–1022 system_fields block):
- `sections.appointment_information.appointment_date` → `system_fields["appointment_date"]`
- `sections.appointment_information.appointment_type` → `system_fields["appointment_type"]`
- `sections.verification_information.callback_number` → `system_fields["callback_number"]`

Double-check by grepping:

```bash
grep -n "appointment_date\|appointment_type\|callback_number" \
  /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-backend/packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py | head -20
```

Expected output should include the three paths in the system_fields dict (lines ~1015–1022).

- [ ] **Step 2: Add `prerequisite_fields` to the `FormSchemaDoc(...)` constructor call**

Open `vera-backend/packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py`.

Find the `return FormSchemaDoc(` call (around line 1002). The current call has:

```python
    return FormSchemaDoc(
        dsl_version="2.1",
        name="Infertility",
        insurance_type="infertility_treatment",
        description=(...),
        system_fields={...},
        promoted_fields=PromotedFields(...),
        stt_key_terms=[...],
```

After `promoted_fields=PromotedFields(...)` and before `stt_key_terms=[...]`, insert:

```python
        prerequisite_fields=[
            "sections.appointment_information.appointment_date",
            "sections.appointment_information.appointment_type",
            "sections.verification_information.callback_number",
        ],
```

- [ ] **Step 3: Recompile the schemas**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-backend
just compile-schemas
```

Expected: command exits 0; `data/form_schemas/ibv_form_standard.json` is updated (contains `"prerequisite_fields"` key); other JSON artifacts are unchanged.

- [ ] **Step 4: Run the full forms test suite including freshness + round-trip**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-backend
uv run pytest tests/unit/forms/ -v
```

Expected: ALL tests pass, including:
- `TestCompiledArtifacts::test_committed_artifact_is_fresh[infertility_treatment]` — confirms the artifact matches the fresh compile.
- `TestCompiledArtifacts::test_round_trip[infertility_treatment]` — confirms load→compile is identity.
- `TestPrerequisiteFields::test_ibv_standard_has_three_prerequisite_fields` — now passes.
- All disease_only tests pass unchanged.

- [ ] **Step 5: Spot-check the generated artifact**

```bash
python3 -c "
import json
data = json.load(open('/Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-backend/data/form_schemas/ibv_form_standard.json'))
print(data.get('prerequisite_fields'))
"
```

Expected: `['sections.appointment_information.appointment_date', 'sections.appointment_information.appointment_type', 'sections.verification_information.callback_number']`

- [ ] **Step 6: Run ruff check on the catalog file**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-backend
uv run ruff check packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py
```

Expected: no errors.

- [ ] **Step 7: Commit**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-backend
git add packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py \
        data/form_schemas/ibv_form_standard.json
git commit -m "$(cat <<'EOF'
feat(forms/catalog): mark 3 prerequisite fields in ibv_standard + recompile

Sets prerequisite_fields = [appointment_date, appointment_type, callback_number]
on the IBV standard FormSchemaDoc. Recompiles the artifact so the freshness
gate stays green. No change to roles or system_fields — the 3 fields remain
system-bound context leaves; prerequisite_fields is a UI-only annotation.

Sprint-2 #25/#26.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014PNLDcrjuaRB8hhYc4sQUj
EOF
)"
```

---

### Task 3: Frontend — Add `prerequisiteFields` to TypeScript types and schema parser

**Files:**
- Modify: `vera-frontend/src/lib/ibv/types.ts`
- Modify: `vera-frontend/src/lib/ibv/schema.ts`

**Interfaces:**
- Produces: `FormSchema.prerequisite_fields?: string[]` (raw document field, snake_case to match JSON)
- Produces: `prerequisiteFieldPaths(schema: FormSchema): Set<string>` — memoized per schema, returns a `Set<string>` of paths
- Produces: `FieldUsage = "prerequisite" | "system" | "context" | "noop" | "asked"` (prerequisite first in union for documentation clarity)
- Consumes: none (first frontend task)

- [ ] **Step 1: Write the failing frontend tests**

Open `vera-frontend/src/lib/ibv/schema.test.ts` (or create it if absent). Add:

```typescript
import { describe, expect, it } from "vitest"
import { fieldUsageOf, parseSchema, prerequisiteFieldPaths } from "./schema"
import type { FormSchema } from "./types"

const minimalSchema: FormSchema = {
  dsl_version: "2.1",
  name: "Test",
  insurance_type: "test",
  system_fields: { plan_type: "sections.basics.plan_type" },
  prerequisite_fields: ["sections.basics.plan_type"],
  sections: {
    basics: {
      title: "Basics",
      fields: {
        plan_type: { type: "text", title: "Plan Type", role: "ask" },
        notes: { type: "text", title: "Notes", role: "context" },
      },
    },
  },
}

describe("prerequisiteFieldPaths", () => {
  it("returns a Set of paths from prerequisite_fields", () => {
    const paths = prerequisiteFieldPaths(minimalSchema)
    expect(paths.has("sections.basics.plan_type")).toBe(true)
    expect(paths.has("sections.basics.notes")).toBe(false)
  })

  it("returns empty Set when prerequisite_fields is absent", () => {
    const schema: FormSchema = { ...minimalSchema, prerequisite_fields: undefined }
    expect(prerequisiteFieldPaths(schema).size).toBe(0)
  })
})

describe("fieldUsageOf — prerequisite beats system", () => {
  it("returns 'prerequisite' for a path in prerequisiteFields even when in system_fields", () => {
    const field = minimalSchema.sections.basics.fields.plan_type as import("./types").LeafField
    const usage = fieldUsageOf(minimalSchema, "sections.basics.plan_type", field)
    expect(usage).toBe("prerequisite")
  })

  it("returns 'context' for a non-prerequisite context-role field", () => {
    const field = minimalSchema.sections.basics.fields.notes as import("./types").LeafField
    const usage = fieldUsageOf(minimalSchema, "sections.basics.notes", field)
    expect(usage).toBe("context")
  })
})
```

- [ ] **Step 2: Run the tests to confirm they fail**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-frontend
npm test -- --run src/lib/ibv/schema.test.ts 2>&1 | tail -30
```

Expected: failures because `prerequisiteFieldPaths` is not exported and `"prerequisite"` usage is unknown.

- [ ] **Step 3: Add `prerequisite_fields` to `FormSchema` in `types.ts`**

Open `vera-frontend/src/lib/ibv/types.ts`. Find the `FormSchema` type (around line 110):

```typescript
export type FormSchema = {
  dsl_version: string
  name: string
  insurance_type: string
  description?: string
  /** well-known system handles → field paths */
  system_fields?: Record<string, string>
  shared_conditions?: Record<string, Condition>
  /** object keyed by section_key; key order = UI order */
  sections: Record<string, Section>
  contradictions?: Contradiction[]
}
```

Add `prerequisite_fields` after `system_fields`:

```typescript
export type FormSchema = {
  dsl_version: string
  name: string
  insurance_type: string
  description?: string
  /** well-known system handles → field paths */
  system_fields?: Record<string, string>
  /**
   * Root-anchored leaf paths the platform treats as prerequisites for a call.
   * Drives a distinct amber UI highlight, separate from the violet system-field tint.
   */
  prerequisite_fields?: string[]
  shared_conditions?: Record<string, Condition>
  /** object keyed by section_key; key order = UI order */
  sections: Record<string, Section>
  contradictions?: Contradiction[]
}
```

- [ ] **Step 4: Add `"prerequisite"` to `FieldUsage` and implement `prerequisiteFieldPaths` in `schema.ts`**

Open `vera-frontend/src/lib/ibv/schema.ts`.

**4a.** Find the `FieldUsage` type (line 187):

```typescript
export type FieldUsage = "system" | "context" | "noop" | "asked"
```

Replace with:

```typescript
export type FieldUsage = "prerequisite" | "system" | "context" | "noop" | "asked"
```

**4b.** After the `systemFieldPaths` function (around line 199), add the new `prerequisiteFieldPaths` helper:

```typescript
const _prerequisitePathsBySchema = new WeakMap<FormSchema, Set<string>>()

/** The field paths listed in `prerequisite_fields` (call-prerequisite UI annotation). */
export function prerequisiteFieldPaths(schema: FormSchema): Set<string> {
  let paths = _prerequisitePathsBySchema.get(schema)
  if (!paths) {
    paths = new Set(schema.prerequisite_fields ?? [])
    _prerequisitePathsBySchema.set(schema, paths)
  }
  return paths
}
```

**4c.** Find `fieldUsageOf` (line 201):

```typescript
export function fieldUsageOf(
  schema: FormSchema,
  path: string,
  field: LeafField
): FieldUsage {
  if (systemFieldPaths(schema).has(path)) return "system"
  // A ui_only SECTION is never voice-touched, whatever its leaves' roles say.
  if (schema.sections[path.split(".")[1]]?.role === "ui_only") return "noop"
  if (field.role === "context") return "context"
  if (field.role === "input" || field.role === "readonly") return "noop"
  return "asked"
}
```

Replace with (prerequisite check BEFORE system):

```typescript
export function fieldUsageOf(
  schema: FormSchema,
  path: string,
  field: LeafField
): FieldUsage {
  if (prerequisiteFieldPaths(schema).has(path)) return "prerequisite"
  if (systemFieldPaths(schema).has(path)) return "system"
  // A ui_only SECTION is never voice-touched, whatever its leaves' roles say.
  if (schema.sections[path.split(".")[1]]?.role === "ui_only") return "noop"
  if (field.role === "context") return "context"
  if (field.role === "input" || field.role === "readonly") return "noop"
  return "asked"
}
```

- [ ] **Step 5: Run the tests to confirm they pass**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-frontend
npm test -- --run src/lib/ibv/schema.test.ts 2>&1 | tail -30
```

Expected: all new tests pass.

- [ ] **Step 6: TypeScript check**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-frontend
npx tsc --noEmit 2>&1 | head -40
```

Expected: the only errors (if any) are in `usageMeta.ts` (TS complains `"prerequisite"` is missing from `USAGE_META` Record — this is expected, resolved in Task 4). Zero errors in `types.ts` and `schema.ts`.

- [ ] **Step 7: Commit**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-frontend
git add src/lib/ibv/types.ts src/lib/ibv/schema.ts src/lib/ibv/schema.test.ts
git commit -m "$(cat <<'EOF'
feat(fe/schema): add prerequisite FieldUsage + prerequisiteFieldPaths helper

Adds prerequisite_fields?: string[] to FormSchema type (mirrors the new
backend DSL key). Adds prerequisiteFieldPaths() memoized helper (WeakMap
cache, same pattern as systemFieldPaths). Inserts "prerequisite" check
at the top of fieldUsageOf so it wins over "system" for those 3 fields.

Sprint-2 #25/#26.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014PNLDcrjuaRB8hhYc4sQUj
EOF
)"
```

---

### Task 4: Frontend — Add amber "prerequisite" entry to `usageMeta.ts` and update `USAGE_ORDER`

**Files:**
- Modify: `vera-frontend/src/components/ibv/usageMeta.ts`

**Interfaces:**
- Consumes: `FieldUsage` (now includes `"prerequisite"`) from Task 3
- Produces: `USAGE_META["prerequisite"]` with amber styling
- Produces: `USAGE_ORDER` with `"prerequisite"` before `"system"` so the legend renders them in priority order

- [ ] **Step 1: Confirm usageMeta.ts shape**

The current `USAGE_META` shape (from `usageMeta.ts`) is:

```typescript
export const USAGE_META: Record<
  FieldUsage,
  { label: string; description: string; labelCellClass: string; swatchClass: string }
> = { system: {...}, context: {...}, asked: {...}, noop: {...} }
export const USAGE_ORDER: FieldUsage[] = ["system", "context", "asked", "noop"]
```

The `Record<FieldUsage, ...>` type will cause a TypeScript error until `"prerequisite"` is added — this is the TS error expected from Task 3 Step 6.

- [ ] **Step 2: Add `prerequisite` to `USAGE_META` and update `USAGE_ORDER`**

Open `vera-frontend/src/components/ibv/usageMeta.ts`. Add the `prerequisite` entry to `USAGE_META` before `system`, and add `"prerequisite"` to `USAGE_ORDER` before `"system"`:

```typescript
import type { FieldUsage } from "@/lib/ibv/schema"

/**
 * One place for the field-usage color coding: FieldRow tints its label cell
 * with `labelCellClass`, and UsageLegend (below the reference rail) explains
 * the same classes.
 */
export const USAGE_META: Record<
  FieldUsage,
  { label: string; description: string; labelCellClass: string; swatchClass: string }
> = {
  // Amber: distinct from the violet system tint and the green context tint.
  // Amber color is easily adjustable — product may request a different hue.
  prerequisite: {
    label: "Prerequisite",
    description:
      "Required before the call begins — the voice agent uses these to set up and route the call (appointment date, appointment type, callback number).",
    labelCellClass: "bg-amber-100",
    swatchClass: "border-amber-400 bg-amber-100",
  },
  // Violet, not red: the dispute highlight already uses red for low-confidence
  // captures, so a red system tint would read as a dispute.
  system: {
    label: "System field",
    description:
      "Required by the platform — worklists, integrations and call setup read these; their values are also given to the voice agent as known context.",
    labelCellClass: "bg-violet-100",
    swatchClass: "border-violet-300 bg-violet-100",
  },
  context: {
    label: "Voice-agent context",
    description:
      "Fed to the voice agent as known background — answered if the representative asks, never volunteered, never asked.",
    labelCellClass: "bg-green-100",
    swatchClass: "border-green-400 bg-green-100",
  },
  asked: {
    label: "Collected on the call",
    description: "Asked (or confirmed) by the voice agent during the verification call.",
    labelCellClass: "",
    swatchClass: "border-ibv-input-border bg-ibv-input-bg",
  },
  // Diagonal gray hatching ("not in use"), not another hue — flat gray was
  // indistinguishable from the default label cells.
  noop: {
    label: "UI only",
    description:
      "Display / data entry only (including UI-only sections) — not asked on the call and not part of the agent's context.",
    labelCellClass:
      "bg-[repeating-linear-gradient(45deg,#e4e4e7_0px,#e4e4e7_5px,#fafafa_5px,#fafafa_10px)]",
    swatchClass:
      "border-zinc-400 bg-[repeating-linear-gradient(45deg,#e4e4e7_0px,#e4e4e7_3px,#fafafa_3px,#fafafa_6px)]",
  },
}

/** Legend display order. */
export const USAGE_ORDER: FieldUsage[] = ["prerequisite", "system", "context", "asked", "noop"]
```

- [ ] **Step 3: TypeScript check — should now be clean**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-frontend
npx tsc --noEmit 2>&1 | head -40
```

Expected: zero errors.

- [ ] **Step 4: Run lint**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-frontend
npm run lint 2>&1 | tail -20
```

Expected: no errors or warnings from modified files.

- [ ] **Step 5: Run all frontend tests**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-frontend
npm test -- --run 2>&1 | tail -30
```

Expected: all tests pass.

- [ ] **Step 6: Verify spouse_gender is NOT in prerequisiteFields (defect #26 check)**

Add a quick sanity test to `schema.test.ts` or verify inline:

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-frontend
node -e "
const schema = require('./data/form_schemas/ibv_form_standard.json');
const prereqs = new Set(schema.prerequisite_fields ?? []);
console.log('spouse_gender in prereqs:', prereqs.has('sections.patient_information.spouse_gender'));
console.log('prerequisite_fields:', [...prereqs]);
" 2>/dev/null || echo "Node check skipped (use compiled artifact check instead)"
```

Alternatively, verify via the backend:

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-backend
python3 -c "
import json
data = json.load(open('data/form_schemas/ibv_form_standard.json'))
prereqs = set(data.get('prerequisite_fields', []))
assert 'sections.patient_information.spouse_gender' not in prereqs, 'spouse_gender must NOT be in prerequisite_fields'
print('OK: spouse_gender is not in prerequisite_fields')
print('prerequisite_fields:', data.get('prerequisite_fields'))
"
```

Expected: `OK: spouse_gender is not in prerequisite_fields`

- [ ] **Step 7: Commit**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-frontend
git add src/components/ibv/usageMeta.ts
git commit -m "$(cat <<'EOF'
feat(fe/usageMeta): distinct amber color for prerequisite fields in legend

Adds "prerequisite" to USAGE_META with bg-amber-100 / border-amber-400
styling and inserts it first in USAGE_ORDER. The amber color is easily
adjustable via Tailwind classes in usageMeta.ts if product requests a
different hue. Spouse Gender remains context/green (#26 resolved because
it is absent from prerequisite_fields, not because its role changed).

Sprint-2 #25/#26.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014PNLDcrjuaRB8hhYc4sQUj
EOF
)"
```

---

### Task 5: Final gate run + write report

**Files:**
- Create: `/Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/.superpowers/sdd/sprint2-m25-report.md`

**Interfaces:**
- Consumes: all prior tasks complete

- [ ] **Step 1: Run full backend gate**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-backend
just check
```

Expected: `lint` + `typecheck` + `test` all pass. If mypy emits errors in livekit-related modules that already existed before this change, those are pre-existing and not regressions — note them in the report.

- [ ] **Step 2: Run full frontend gate**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/vera-frontend
npx tsc --noEmit && npm run lint && npm test -- --run
```

Expected: all three pass.

- [ ] **Step 3: Write the report**

```bash
mkdir -p /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/.superpowers/sdd
```

Create `/Users/mainadmin/Documents/GitHub/Techjays/Vera2.0/.superpowers/sdd/sprint2-m25-report.md` with the following content (fill in actual commit SHAs and gate outputs):

```markdown
# Sprint-2 Defects #25 & #26 — Prerequisite Fields Highlight

**Date:** 2026-07-13  
**Branch:** fix/sprint-2-defects  
**Author:** YashTechjays

## Summary

Implemented a distinct "prerequisite" field category for the IBV form renderer.

### Defect #25 — Prerequisite fields indistinct from system fields

**Root cause:** Appointment Date, Appointment Type, and Callback Number are bound in
`system_fields` → `fieldUsageOf` returned `"system"` (violet) for all three.

**Fix:** Added `prerequisite_fields: list[str] = []` to `FormSchemaDoc` in `dsl.py`.
Populated it with the 3 paths in `ibv_standard.py`. Recompiled the artifact. On the
frontend, `fieldUsageOf` now checks `prerequisiteFieldPaths` *before* `systemFieldPaths`,
so the 3 fields return `"prerequisite"` (amber) instead of `"system"` (violet).

### Defect #26 — Spouse Gender green highlight reads as "prerequisite"

**Root cause:** QA was conflating "context" (green) with the expected prerequisite
highlight they wanted for the 3 prerequisite fields.

**Fix:** No role change to Spouse Gender. The defect resolves automatically: now that
"prerequisite" is a distinct amber category, green = context, not prerequisite. Spouse
Gender (`sections.patient_information.spouse_gender`) is NOT in `prerequisite_fields`.

## DSL Change

**File:** `packages/vera_core/src/vera_core/forms/dsl.py`

Added to `FormSchemaDoc`:
```python
prerequisite_fields: list[str] = Field(default_factory=list)
```
- Optional, defaults to empty list → omitted from compiled artifact (`exclude_defaults=True`)
- Validated: every listed path must resolve to a defined leaf (same pattern as `system_fields`)
- Existing schemas without the key are unaffected (backward-compatible)

## The 3 Paths (ibv_standard.py)

```python
prerequisite_fields=[
    "sections.appointment_information.appointment_date",
    "sections.appointment_information.appointment_type",
    "sections.verification_information.callback_number",
]
```

Confirmed via `system_fields` dict lines 1015–1022 of `ibv_standard.py`.

## Compiled Artifact Diff Summary

**`data/form_schemas/ibv_form_standard.json`:** Added `"prerequisite_fields"` key after
`"promoted_fields"`, containing the 3 paths above. All other content unchanged.

**`data/form_schemas/disease_only_verification.json`:** Unchanged (no `prerequisite_fields`
set in `disease_only.py` — optional field, defaults to empty, excluded from artifact).

**`data/form_schemas/manifest.json`:** Unchanged.

## Frontend Color Choice

**Chosen:** `bg-amber-100` (label cell) / `border-amber-400 bg-amber-100` (swatch) — Tailwind amber family.

**Rationale:** Amber is visually distinct from violet (system), green (context), and the
white/diagonal-gray (asked/noop). It reads as "attention / prerequisite" without clashing
with the red dispute-highlight already in use.

**Adjustability:** The color is a one-line change in
`vera-frontend/src/components/ibv/usageMeta.ts` under the `prerequisite` key's
`labelCellClass` and `swatchClass` properties. Product can request a different Tailwind
color class without touching any logic.

## Gate Results

### Backend

- `uv run ruff check .` → PASS (0 errors)
- `uv run mypy packages/vera_core` → PASS on forms package (pre-existing livekit errors excluded)
- `pytest tests/unit/forms/` → PASS (all tests including freshness + round-trip)
- `just check` → PASS

### Frontend

- `npx tsc --noEmit` → PASS
- `npm run lint` → PASS
- `npm test -- --run` → PASS

## Commits

| SHA | Message |
|-----|---------|
| (fill) | feat(forms/dsl): add optional prerequisite_fields key to FormSchemaDoc |
| (fill) | feat(forms/catalog): mark 3 prerequisite fields in ibv_standard + recompile |
| (fill) | feat(fe/schema): add prerequisite FieldUsage + prerequisiteFieldPaths helper |
| (fill) | feat(fe/usageMeta): distinct amber color for prerequisite fields in legend |
```

- [ ] **Step 4: Final commit for the report**

```bash
cd /Users/mainadmin/Documents/GitHub/Techjays/Vera2.0
git add .superpowers/sdd/sprint2-m25-report.md
git commit -m "$(cat <<'EOF'
docs(sdd): sprint-2 #25/#26 prerequisite fields implementation report

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014PNLDcrjuaRB8hhYc4sQUj
EOF
)"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] `prerequisite_fields: list[str] = []` on `FormSchemaDoc` → Task 1
- [x] Empty list omitted from artifact (exclude_defaults) → Task 1 Step 3 + test
- [x] Path validation (resolves to leaf) → Task 1 Step 4
- [x] 3 exact paths populated in ibv_standard.py → Task 2
- [x] `just compile-schemas` rerun → Task 2 Step 3
- [x] Freshness + round-trip tests pass → Task 2 Step 4
- [x] `FieldUsage` union includes `"prerequisite"` → Task 3
- [x] `prerequisiteFieldPaths()` helper with WeakMap cache → Task 3
- [x] `fieldUsageOf` checks prerequisite BEFORE system → Task 3 Step 4c
- [x] `FormSchema.prerequisite_fields?: string[]` type added → Task 3 Step 3
- [x] `usageMeta.ts` amber entry + `USAGE_ORDER` updated → Task 4
- [x] Spouse Gender NOT in prerequisiteFields → Task 4 Step 6 (verified, not changed)
- [x] Amber color noted as adjustable → Task 4 + report
- [x] Report written to `.superpowers/sdd/sprint2-m25-report.md` → Task 5
- [x] All gate commands specified → Task 5

**Placeholder scan:** No TBD/TODO/placeholder patterns found in this plan.

**Type consistency:**
- `FieldUsage` union defined in `schema.ts` → consumed by `usageMeta.ts` (Record key), `UsageLegend.tsx` (USAGE_ORDER iteration)
- `prerequisiteFieldPaths(schema: FormSchema): Set<string>` → called in `fieldUsageOf(schema, path, field)`
- `FormSchema.prerequisite_fields?: string[]` → consumed by `prerequisiteFieldPaths`
- All names consistent across tasks.
