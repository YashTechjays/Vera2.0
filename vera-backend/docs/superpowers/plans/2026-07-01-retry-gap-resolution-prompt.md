# Retry / Partial Gap-Resolution Prompt — Design & Plan

**Date:** 2026-07-01 · **Status:** Proposed (awaiting team-lead confirmation) · **Branch:** `feat/retry-gap-resolution-prompt`

**One-line goal:** When Vera calls the same insurance form a second time, ask only the questions still missing or answered invalidly — instead of restarting the whole script.

---

## 1. At a glance

| | |
|---|---|
| **What** | Read a form's stored answers → compute the still-missing/invalid fields → build a short follow-up prompt that asks only those. |
| **Trigger** | The form's most recent finished call captured a `call_reference_no` (proof the call connected and progressed). |
| **Where the logic lives** | Two pure functions in `vera_core/forms/` — `compute_gaps` and `compose_gap_prompt`. No schema change, no new worker→DB path. |
| **How it reaches Vera** | The composed prompt rides `PersonaTweak.instructions_override` in the existing LiveKit dispatch metadata. |
| **Reference POC** | `../vera-schema-builder` already proved this (`computeGaps` / `composeGapPrompt`); we port the idea to production. |
| **Ships now** | The engine + renderer (Phases 1–2), fully unit-tested against the real schema. |
| **Deferred** | The live wiring (Phase 3) — it depends on a prerequisite that doesn't exist yet (see §2). |

## 2. The one dependency that gates real use ⚠️

**Nothing writes a call's captured answers back to the database yet.** `intake_payload` holds only the pre-provided intake context; `Call.call_reference_no` / `Call.rep_info` are columns nothing populates (the worker is a chat-only slice with no tool machinery).

**So:** until a **call→DB answer write-back** exists, the trigger never fires and the engine sees no call answers. We therefore build and unit-test the engine now, and **gate the live wiring** behind that prerequisite. This feature is *ready* but not *live* until write-back lands. Write-back is a **separate piece, explicitly out of scope here.**

## 3. Why this shape (key decisions & tradeoffs)

| Decision | Choice | Why |
|---|---|---|
| Trigger | Prior call's `call_reference_no` is set | "We got a reference number" = partial success worth resuming. |
| Schema | Existing production schema (`schema_version.schema_json`) | No format migration; it already has `properties`, `required_state`, `rules[]`, `constraint_ref`. |
| "Filled" | Non-empty **and** passes its constraint | Folds field validation into gap detection — one definition of "done". |
| Invalid value | Re-ask it | A present-but-wrong answer is worse than a missing one. |
| Validation safety | **Conservative** — unknown constraint ⇒ treat as filled | Never re-ask a valid answer because our parser was unsure (protects call quality). |
| Engine | Pure function, no LLM / no DB | Deterministic, unit-testable against the real schema. |
| Delivery | Dispatch metadata (`instructions_override`) | Worker is inside the BAA boundary; no new worker→DB path. |
| PHI in prompt | Deferred — no raw values in the prompt yet | Sidesteps the PHI-in-prompt decision until the real `PHIBoundary` is wired (§7). |

## 4. Where the data lives

- **Schema (test fixture):** `data/form_schemas/ibv_form_standard.json`.
- **Schema (runtime):** `schema_version.schema_json` (JSONB) — `SchemaVersion` model, `authoring.py:44`; a `PatientForm` binds via `schema_version_id`.
- **Answers:** `patient_form.intake_payload` (JSONB); call answers arrive later via write-back.
- **Reused helpers:** `vera_core/forms/intake.py` — `_is_empty`, `iter_leaf_answers`.

## 5. Data flow

```
dispatch a call ─▶ prior call's call_reference_no set?
                        │ no ─▶ full SYSTEM_PROMPT (unchanged)
                        │ yes
       known_values = intake_payload (+ prior-call answers when write-back lands)
                        │
       compute_gaps(schema_json, known_values) ─▶ GapReport (paths/counts only, never values)
                        │
       compose_gap_prompt(schema_json, report) ─▶ short follow-up prompt
                        │
       PersonaTweak.instructions_override ─▶ LiveKit dispatch metadata ─▶ worker uses it
```

---

## 6. The plan — 4 phases (test-first, small commits)

### Phase 0 — Ground-truth spike (no shipping code)
Confirm every schema assumption *before* writing engine code (an earlier draft baked in bugs from guessing). Deliverables: a trimmed real-schema **test fixture** + a short findings note.
- Map the real `rules[].effect` values → engine behaviour: `make this required` (required-when), `ask this question` (skip when unmet), `auto-fill a value` (treat as already filled), `terminate_call_when` (ignore for gaps).
- Confirm `constraint_library` entry shape — real key is **`"category": "enum"`** with `values` (not `"kind"`); record any `format`/regex shape.
- Confirm there are **no** service groups (`sub_fields`) in the schema → that feature is cut.
- Build `ibv_gap_fixture.json` covering: static-required, conditional-required, conditional-ask, auto-fill, an `always_ask` field (the call reference number), and the constraints they use.

### Phase 1 — Pure gap engine  ·  `vera_core/forms/gaps.py` (the core deliverable)
All tests run against the Phase-0 real-schema fixture.
- **`_passes_constraint(value, field, library)`** — enum membership (trim + case-insensitive) / format regex; unrecognised constraint ⇒ `True` (conservative).
- **`is_filled` / `rule_matches`** — non-empty **and** valid; evaluate a rule's `conditions` (`all of these` / `any of these`).
- **`compute_gaps(schema_json, known_values) -> GapReport`** — walk `sections[].properties` in order; honour required-when, skip conditional-ask when unmet, treat auto-fill fields as filled, surface `always_ask` separately; a field is a gap when required-in-context **and** not filled. `GapReport` carries paths/counts only.

### Phase 2 — Prompt renderer  ·  `vera_core/forms/gap_prompt.py`
- **`compose_gap_prompt(schema_json, report, known_values) -> str`** — a `<gap_resolution_context>` ("don't re-introduce yourself, pick up naturally"), one `<field>` per gap in schema order (using the field's real `prompt.ask` wording), a `<closing_block>` for `always_ask`, and a "no gap follow-up needed" marker when nothing is missing. **No raw stored values in the prompt** (PHI gate) until write-back.

### Phase 3 — Wiring & delivery (DEFERRED — gated on write-back)
Build **dry-run first** (log the computed gaps for real forms, change nothing) to prove correctness in production before touching a live call.
- Add `PersonaTweak.instructions_override`; `build_instructions` uses it when present, else the base prompt (fail-safe on bad metadata).
- One shared `gap_prompt_for_form(session, form)` helper, called by **both** `calls.py::start_call` **and** the queue dispatcher (PR #33) — so the feature applies to every call-creation path, not just one.
- Measure dispatch-metadata size with a real prompt; if it exceeds LiveKit's limit, switch to a worker-fetch-by-`call_id` endpoint.
- Add the PHI-gating row to `adr/devops-todo.md`; run `/simplify` then `just check`.

---

## 7. PHI / compliance

The eventual "known facts" context would put stored values — some PHI — into the system prompt and dispatch metadata, **bypassing the worker's `redact()` seam**. Today `build_phi_boundary` returns the passthrough stub (no tokenization). Guardrails: **synthetic / role-play data only** until the real `PHIBoundary` is wired; `GapReport` itself is values-free (safe to log/audit); delivery stays inside the BAA boundary (self-hosted LiveKit). This is why Phase 2 keeps raw values out of the prompt for now.

## 8. Risks & handling

| Risk | Handling |
|---|---|
| Can't run end-to-end without write-back | Phases 1–2 are independently valuable + tested; Phase 3 gated + dry-run. |
| False re-asks from misparsed constraints/conditions | Conservative validation; **all tests on the real schema**; dry-run before live use. |
| Two call-creation paths (PR #33) | One shared trigger helper wired into both; coordinate with Yash. |
| PHI in prompt/metadata | Synthetic-only + `devops-todo`; real-PHI deferred to `PHIBoundary`. |
| Metadata too large for the full prompt | Measure in Phase 3; fall back to worker-fetch-by-`call_id`. |

## 9. Explicitly out of scope

- Call→DB answer write-back (the prerequisite — separate spec).
- Real-PHI handling / tokenization in the prompt.
- Service-group re-ask (no `service`/`sub_fields` in the schema).
- `terminate_call_when` / early-termination logic in the prompt.
- Non-IBV / tenant-specific schema variants.

---

*Full step-by-step TDD tasks (exact test + implementation code per step) are available on request; this document is the design + phased plan for review.*
