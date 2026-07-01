# Retry / Partial Gap-Resolution Prompt — Design Spec

**Date:** 2026-07-01
**Status:** Approved (design) — awaiting implementation plan
**Scope:** Compute what a form still needs from stored values, validate stored
values against their field constraints, and compose a follow-up ("gap
resolution") prompt that re-asks only the gaps — dispatched when a form is
called again after a prior call captured a `call_reference_no`.

---

## 1. Problem

Today every dispatched call uses one static `SYSTEM_PROMPT`
(`agent_worker/prompt.py`) that walks the full verification from scratch. When a
call partially completes — the rep answered some questions, gave a call
reference number, then the call dropped — the next call should **not** start
over. It should pick up naturally and ask only what is still missing or invalid.

This spec adds a **partial / retry mode**: when a form is dispatched and its most
recent terminal call captured a `call_reference_no` (proof the call connected and
progressed), the control plane composes a *gap-resolution* prompt from the
schema + stored values instead of the full prompt, and ships it to the worker.

A prior proof-of-concept (`../vera-schema-builder`) already designed and
validated this as browser JS: `computeGaps(schema, filled, ctx) → GapReport` and
`composeGapPrompt(section, gapReport, ctx)`, plus constraint-based validation
(`constraint_enum` / `constraint_format`). This spec ports that mechanism into
production `vera-backend` against the **existing** production schema shape
(`sections[].properties{}` with `required_state`, `rules[]`, `constraint_ref`).

## 2. Prerequisite (explicitly out of scope)

**Nothing writes a call's captured answers back to the database yet.**
`intake_payload` is written only at intake (`POST /patient-forms`, pre-provided
context). `Call.call_reference_no` and `Call.rep_info` are columns that nothing
populates — the worker is a chat-only slice with no tool machinery.

Consequence: until a **call→DB write-back** path exists, (a) the trigger never
fires (`call_reference_no` is never set), and (b) `compute_gaps` sees only intake
context, so every field reads as missing and the gap prompt degenerates to the
full prompt.

**Decision:** the write-back is a *separate prerequisite spec*. This spec builds
the gap engine, validation, renderer, trigger, and delivery against a
well-defined known-values source so the feature is ready the moment write-back
lands. Everything here is testable in isolation without write-back.

## 3. Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Retry trigger | Most recent terminal `Call.call_reference_no` is set | "The call connected and got a reference number" = partial success worth resuming. Independent of `retry_count`. |
| Schema source | Existing production schema (`schema_version.schema_json`) | No schema-format migration; the production shape already carries `properties`, `required_state`, `rules`, `constraint_ref`, `enum`. |
| "Filled" definition | Non-empty **and** passes its constraint | Folds field validation into gap detection (§6). |
| Invalid value | Treated as a gap (re-ask) | A present-but-wrong value is worse than a missing one. |
| Gap engine | Pure function, no LLM, no DB | Deterministic, unit-testable, debuggable — mirrors the POC guarantees. |
| Renderer | Reuse existing field/service rendering | No duplication of the emission logic. |
| Delivery | LiveKit dispatch metadata (`PersonaTweak.instructions_override`) | Worker is inside the BAA boundary; no new worker→DB path. |
| Known-facts context | Include all stored facts, incl. PHI | Faithful to the POC; gated synthetic-only (§9). |
| Home module | `vera_core/forms/` | Extends existing `intake.py` helpers (`missing_required`, `iter_leaf_answers`, `_is_empty`). |

## 4. Data Flow

```
                         ┌─────────────────── control plane ───────────────────┐
dispatch a call ──▶ look up form's most recent terminal Call
                         │                                                      │
              call_reference_no set? ──no──▶ full SYSTEM_PROMPT (unchanged path)
                         │ yes                                                  │
              known_values = merge(intake_payload, prior Call answers)          │
                         │                                                      │
              compute_gaps(schema_json, known_values, ctx) → GapReport          │
                         │                                                      │
              compose_gap_prompt(schema, report, known_values, ctx) → str       │
                         │                                                      │
              PersonaTweak(instructions_override=<gap prompt>) → dispatch meta  │
                         └──────────────────────────┬───────────────────────────┘
                                                    ▼
                    LiveKit dispatch  ──▶  worker.build_instructions()
                                           uses instructions_override when present
```

## 5. Gap Engine — `vera_core/forms/gaps.py`

Pure function, no LLM, no DB:

```python
def compute_gaps(
    schema_json: dict, known_values: dict, ctx: GapContext
) -> GapReport: ...
```

### 5.1 Output shape

```python
@dataclass(frozen=True)
class Gap:
    path: str          # dotted schema path, e.g. "benefit_coverage.coverage_type"
    kind: str          # "field" | "group" | "gate"
    group_key: str | None

@dataclass(frozen=True)
class GapReport:
    missing: list[Gap]              # ordered by schema property order
    groups_to_rerender: list[str]   # service groups where all_or_nothing fired
    skipped: list[tuple[str, str]]  # (path, reason) — condition unmet
    always_ask: list[str]           # ephemeral fields rendered in the closing block
```

**Names and counts only — never values.** A `GapReport` is safe to log/audit.

### 5.2 Algorithm (single pass over `sections[].properties`)

1. **Gate check** — a section may declare a gate field (e.g.
   `infertility_tx_covered`). If the gate is answered "No", the whole section is
   skipped (auto-NA); if unanswered, the gate itself is the only gap.
2. **Per field, in schema order:**
   - `always_ask` → append to `always_ask[]`, skip (rendered separately).
   - Conditional-skip: a `rules[]` entry with a skip/ask effect whose condition
     is unmet → `skipped[]`, continue. (`asked_when` equivalent.)
   - Not required in this context → not a gap (see §5.3), continue.
   - **Service** (`category == "service"` / has `sub_fields`): recurse; if any
     child is a gap and integrity is `all_or_nothing` (default for services),
     emit one `kind="group"` gap + record `groups_to_rerender`; else emit the
     individual missing children.
   - **Leaf**: if `not is_filled(value, field, ctx)` → `kind="field"` gap.

### 5.3 "Required in this context"

A field is a gap-candidate when either:
- `required_state == "required"` (static), **or**
- a `rules[]` entry with effect *"make this required"* whose `conditions` match
  `known_values` (conditional-required — e.g. spouse fields when
  `coverage_type == "Family"`).

Match evaluation reuses the schema's `conditions` shape
(`{field, comparison, value}` + `match: "all of these" | "any of these"`) via a
small internal evaluator (`==`, `!=`, `and`, `or`) — no external library.

### 5.4 `is_filled` (validation-aware)

```python
def is_filled(value, field, ctx) -> bool:
    if _is_empty(value):            # reuses intake.py: None/""/"NA"/"N/A"
        return False
    return _passes_constraint(value, field, ctx)   # §6
```

### 5.5 Guarantees (ported from the POC)

- **Pure** — same inputs → same output.
- **Order-preserving** — `missing[]` follows schema property order (= question order).
- **Idempotent** — a fully-valid fill returns `missing == []`.

## 6. Field Validation — `_passes_constraint`

A stored value is "filled" only if it satisfies its field constraint:

- **Enum** — `field["enum"]` (inline) or `constraint_ref → constraint_library[ref].values`:
  value must be a member (case-insensitive, trimmed).
- **Format** — `constraint_library[ref]` of kind `format`: value must match the
  regex.
- **No constraint** — any non-empty value passes (free text).

A present value that fails its constraint is **not** filled → becomes a gap and
is re-asked. Validation lives inside the gap engine (not a separate pass) so
there is one definition of "done".

## 7. Gap-Resolution Prompt — `vera_core/forms/gap_prompt.py`

```python
def compose_gap_prompt(
    schema_json: dict, report: GapReport, known_values: dict, ctx: GapContext
) -> str: ...
```

Reuses the existing field/service rendering (the `assemble.py`-style
`render_v1_field` / `render_v1_service` logic, ported alongside the engine or
factored from the current renderer). The new code only decides *which* fields to
render and the wrapping shell.

### 7.1 Shell

```xml
<gap_resolution_context>
You are following up on a call where some questions were missed or unclear.
Do not re-introduce yourself. Pick up naturally with the representative.
Already confirmed: <known facts from known_values minus the missing set>.
</gap_resolution_context>

<section name="..." mode="gap_resolution">
  ...per-gap entries in schema order...
</section>

<closing_block>
  ...always_ask fields (call reference number, rep name)...
</closing_block>
```

### 7.2 Per-gap rendering

- `kind="gate"` → render the gate question.
- `kind="group"` → render the whole service group (transition + all sub-fields).
- `kind="field"` with a `group_key` → emit a `<transition>About {display}…` once
  per group run, then the field.
- `kind="field"` standalone → render the field.

### 7.3 Edge cases

- **Nothing missing** → returns a "no gap follow-up needed" marker; caller falls
  back to the full prompt (defensive — trigger implies at least the reference
  field exists).
- **Gate = No** → the section renders as skipped (auto-NA).

## 8. Trigger & Delivery

### 8.1 Trigger (control plane)

At dispatch, resolve the form's most recent **terminal** `Call`. If its
`call_reference_no` is non-null, enter partial mode. This applies to both the
manual `POST /calls` path and the queue dispatcher's call-creation branch
(§ call-queue design). The lookup is a single indexed query on `call` by
`form_id` ordered by `created_at desc`.

### 8.2 Known-values assembly

`known_values` = `form.intake_payload` merged with the prior call's captured
answers. Merge precedence and the exact prior-call source are pinned when the
write-back prereq lands; for now the merge function takes `(intake_payload,
prior_call_answers: dict)` with `prior_call_answers` defaulting to `{}` (so the
engine is fully testable today).

### 8.3 Delivery

Add an optional field to `PersonaTweak` (`vera_core/schemas`):

```python
instructions_override: str | None = None
```

The control plane sets it to the composed gap prompt. The worker's
`build_instructions(tweak)` uses `tweak.instructions_override` as the system
instructions when present, else the static `SYSTEM_PROMPT` (unchanged default).
`parse_persona_tweak` already fails safe on bad metadata, so a malformed override
falls back to the base persona — no live call is ever killed by this path.

## 9. PHI / Compliance

The gap prompt embeds stored answers — including PHI identifiers — directly into
the system prompt and into LiveKit dispatch metadata. This **bypasses the
worker's `redact()` STT→LLM seam** (which tokenizes the live transcript, not the
system prompt). It therefore depends on the real `PHIBoundary`; today
`build_phi_boundary` returns the passthrough stub, so no tokenization occurs.

**Guardrails:**
- **Synthetic / role-play data only** until the real `PHIBoundary` is wired.
- Add an `adr/devops-todo.md` row: "Retry gap prompt embeds PHI in the system
  prompt + dispatch metadata; gate behind real PHIBoundary tokenization (or seed
  the session vault with the known values so tokens are consistent) before any
  real PHI flows."
- `GapReport` itself carries only paths/counts and is safe to log/audit; the
  `PHI_ACCESS` audit is emitted when the prompt (with values) is composed.
- Delivery stays inside the BAA boundary (self-hosted LiveKit OSS), consistent
  with the existing persona-metadata path.

## 10. Modules

| Module | Status | Purpose |
|--------|--------|---------|
| `vera_core/forms/gaps.py` | new | `compute_gaps` + `GapReport` + condition/constraint evaluators |
| `vera_core/forms/gap_prompt.py` | new | `compose_gap_prompt` + field/service rendering |
| `vera_core/forms/intake.py` | reuse | `_is_empty`, `iter_leaf_answers`, `required_intake_fields` |
| `vera_core/schemas` (`PersonaTweak`) | modify | add `instructions_override` |
| `agent_worker/prompt.py` | modify | `build_instructions` honors `instructions_override` |
| control-plane dispatch (`calls.py` / queue dispatcher) | modify | trigger lookup + compose + attach metadata |
| `adr/devops-todo.md` | modify | PHI-in-prompt gating row |

## 11. What Does NOT Change

- **Full-prompt path** — a first call (no prior `call_reference_no`) is untouched.
- **Worker DB access** — none added; the worker still only reads dispatch metadata.
- **Schema format** — no migration; the existing `schema_json` shape drives everything.
- **PHI boundary** — no new tokenization here; this spec *depends on* it, and
  documents the gate.

## 12. Testing Strategy

- **Unit — `compute_gaps`:** static-required gap; conditional-required fires /
  doesn't; conditional-skip suppresses a field; constraint-fail (enum + format)
  becomes a gap; service `all_or_nothing` re-ask; `always_ask` surfaced
  separately; fully-valid fill → empty report; order preserved.
- **Unit — `_passes_constraint`:** enum member / non-member (case, trim);
  format match / mismatch; no-constraint free text; empty short-circuits.
- **Unit — `compose_gap_prompt`:** context lists known facts; per-gap entries in
  order; group transition dedup; closing block; nothing-missing marker; gate-No.
- **Unit — `build_instructions`:** `instructions_override` wins; absent → base;
  malformed metadata → base (fail-safe).
- **Integration:** trigger selects partial mode when the prior terminal call has
  `call_reference_no`; composed prompt rides dispatch metadata end-to-end
  (synthetic data).

## 13. Open Questions

- Exact prior-call answer source + merge precedence — pinned with the write-back
  prereq spec (§8.2). Does not block building the engine.
- Whether `always_ask` is added as an explicit schema flag or derived from the
  verification section — resolved during implementation against the current
  `ibv_form_standard.json`.
