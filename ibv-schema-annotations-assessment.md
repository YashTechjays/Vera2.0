# IBV Schema — Annotation Review Assessment

Source of comments: `Downloads/ibv_form_standard_annotated.jsonc`
Target schema: `vera-backend/data/form_schemas/ibv_form_standard.json`
Date: 2026-07-03

## How each verdict was grounded (what the code actually consumes)

Verified against the real consumers of the compiled form schema:

- **Backend** reads only a tiny slice of the schema:
  - `forms/intake.py` — `sections[].required` (patient_information) + walks the
    submitted payload; promotes `patient_name`, `patient_dob`, `chart_number`,
    `appointment_*`, `policy_number`, insurance ref.
  - `forms/review.py` — `sections[].section_key`, `properties[].required_state`,
    `type`. Builds dotted paths `section_key.field_key` (**one level only**, does
    not recurse into nested groups).
- **Frontend** (`src/lib/ibv/*.ts`) reads: `properties` (nested), `type`, `title`,
  `ui.widget`, `enum`, `constraint_ref` (fallback when no `enum`), `required_state`,
  `rules`, `icd10`, `prompt_role === "prose"` (skipped). Renders CPT matrices from
  group → row → leaf structure (`getSectionMatrix`).
- **Prompt is a SEPARATE file**: `vera-backend/data/prompts/ibv_standard_prompt.json`
  (phases/IVR/persona live there). Its `source` cites `vera-schema-builder/…`.
- **Decisive check:** grep of every backend `.py` for
  `verbatim_prompt | section_policies | policies | constraint_library | field_order`
  → **no matches**. None of that material is consumed at runtime — it is dead
  duplication inside the compiled form schema.

Verdict legend:
✅ Valid · ⚠️ Valid, with a caveat · 🔷 Valid but a NEW mechanism (design task) · ❓ Fair question / judgment call

---

## A. Strip non-form material from the compiled schema

**1. Remove `constraint_library` (dup of each field's `enum`). Keep refs in the builder if needed.** — ⚠️ Valid
Backend never reads it. Correct to remove **provided every enum field keeps an
explicit `enum`**. Caveat: the frontend's `resolveOptions` (schema.ts:7-13) falls
back to `constraint_library` when a field has no `enum`, and the *transformed*
frontend schema has YES_NO_NA fields with `constraint_ref` but no `enum`. So: inline
`enum` on every choice field first, then the library is safe to drop.

**2. Remove `constraint_ref` from every field (enum already below it).** — ⚠️ Valid
Same caveat as #1: backend ignores `constraint_ref` entirely; safe to drop **only if
`enum` is present** on that field. Do #1 and #2 together with an "every choice field
has an inline `enum`" guarantee.

**3. Remove `summary` from all `rules`.** — ✅ Valid
`validation.ts` reads `effect`, `match`, `conditions`, `field`, `value` only.
`summary` is purely descriptive and unused. Safe to delete.

**4. Remove `source` from all policy objects.** — ✅ Valid
It's provenance (e.g. `"phases/phase_2_basics.py:PHASE_2_START"`). Not consumed.
Mostly moot once the policies themselves are removed (see #6/#7 and the prompt split).

**5. Remove the field-ordering array; derive order from the schema itself.** — ✅ Valid
Both consumers iterate JSON object insertion order (Python dict + `json.dump` and JS
`Object.entries` both preserve it). A separate order array is read by nobody. Just
ensure whatever *writes* the schema preserves key order (it does).

**6. Remove rejection-handling from the schema (prompt concern).** — ✅ Valid
Not consumed by any form logic; belongs in the prompt. Consistent with the schema =
form-source-of-truth thesis.

**7. Remove IVR script from the schema.** — ✅ Valid
IVR is handled by `agent_worker/ivr_prompt.py` + `ivr_agent.py` and the separate
prompt file — not from the form schema. Clear duplication; remove.

> Note on A as a whole: the reviewer's core thesis — "the compiled schema is purely
> the form's source of truth; prompt/IVR/builder concerns don't belong in it" — is
> **strongly supported by the code**. `verbatim_prompt` + `section_policies` are dead
> weight in this file. Removing them is the single biggest, safest win.

---

## B. Read-only / context-only / no-op marking

**8. `chart_number` is read-only, not part of the prompt — but tagged `prompt_role: "question"`.** — ✅ Valid (real defect)
`intake.py` promotes `chart_number` as a provided identifier; the bot never asks it.
Tagging it a question is wrong. (Enforcement note: `prompt_role` isn't read by the
current backend at all — the read-only distinction is honored by the prompt-compile /
schema-builder step, so the fix is correct but takes effect there, not in intake/review.)

**9. `patient_name` is context-only; bot provides it if the rep/IVR asks.** — ✅ Valid
Already modeled as `verifiable_question` + `confirm_only` in the annotated draft. Sound.

**10. Hospital/provider section is context-only (bot replies with name/NPI/office; UI renders it).** — ✅ Valid
Matches reality: these are supplied at intake, not collected on the call.

**11. Add a "no-op section" marker to exclude UI-only sections (e.g. `form_information`) from the agent task.** — 🔷 Valid design task
Genuine gap — nothing today excludes a section from the prompt/agent build. Good idea.
It's a NEW schema key (e.g. `"usage": "ui_only"` / `"no_op": true`) that both the
prompt-compiler and, ideally, completion-% logic must honor. Additive, not a
find-and-fix.

**12. Reconsider `intro_prose` prompt_role on sections not used in the prompt.** — ❓ Fair question
`prompt_role` is not consumed by the current backend, so this is a design-consistency
question for the schema-builder/prompt-compiler, not a runtime defect. Worth resolving
alongside #11 (define the small, closed vocabulary of roles and what each does).

---

## C. Diagnostic testing — per-CPT restructure

**13. Give each of the 8 CPT codes its own group with `covered`, `copay`, `coinsurance`, `prior_auth`.** — ✅ Valid (and already the frontend's shape)
The transformed frontend schema (`ibv-schema.json`) already models diagnostic testing
exactly this way (`cpt_1..cpt_8`, copay/coinsurance split, `icd10` on the group), and
`getSectionMatrix` expects precisely group → row → 4-leaf. Aligning the backend schema
to it removes the schema mismatch you originally spotted. Strongly valid.

---

## D. Proper nested JSON — no flat/blob grouping

**14. No flat dotted keys like `"office_visits.covered"`; use real nested JSON.** — ✅ Valid
A literal dotted key is fragile and breaks the frontend matrix (it renders as one leaf,
not a group) and any deeper nesting. Reviewer's "another child level breaks it" is
correct. Required for the CPT/service matrix rendering to work.

**15. `group_integrity` alone is meaningless — actually define the group and its members.** — ✅ Valid
`group_integrity: "all_or_nothing"` is consumed by nobody (not in backend, not in
frontend types). It's decorative. Real nested structure (per #13/#14) is what's needed.

**16. Split the `third_party` blob into separate properties (`..._exists` YES_NO, `..._name`).** — ✅ Valid
A blob object stored under one key = one `field_answer` row holding a dict, which
defeats per-field review/dispute (review keys by dotted path). One property per field
is the right model.

**17. Split the pharmacy blob (`pharmacy_benefit_manager`, `..._phone_number`).** — ✅ Valid  (same reasoning as #16)

**18. Split the ISP blob (`isp_exists` YES_NO, `isp_name`, `isp_phone_number`).** — ✅ Valid  (same)

**19. Split `insurance_representative` blob AND add the missing `insurance_rep_name` + `call_reference_number`.** — ✅ Valid
Both the split and the "these fields are missing" observation are correct. Per-field
properties enable per-field answers/review.

---

## E. Renames & cross-references

**20. Rename section `third_party` → `third_party_administrator`.** — ⚠️ Valid (clarity)
No backend `.py` references the key; check the frontend for the literal before/after.
Caveat: `section_key` is the path prefix for stored `field_answer`s — fine pre-launch
with no real data, but if any data exists it needs a path migration.

**21. Encode cross-section refs in the rule's field key (full path) instead of a bare key + `summary`.** — ⚠️ Valid & insightful (needs a code change)
This directly fixes the fragility the code itself warns about: `validation.ts`
`conditionScope` maps a **bare last segment** to a path and its own NOTE says this
breaks if two fields share a segment (e.g. `covered`, `npi`). Explicit paths remove the
ambiguity. Two caveats: (a) `validation.ts` must be updated to consume the explicit
path instead of last-segment matching; (b) use the **dotted** convention
(`benefit_coverage.coverage_type`) already used everywhere, not the slash form
(`sections/benefit_coverage/coverage_type`) shown in the comment.

---

## F. Misc

**22. Remove the "…verbatim from v1" description on the male-partner section.** — ✅ Valid (low importance)
Authoring cruft in a `description`; harmless to remove.

**23. Male-partner section is conditionally mandatory when `benefit_coverage.coverage_type = "Family"`.** — ✅ Valid (encode it)
Correct requirement. Encode it with the same conditional-rule mechanism used for the
spouse fields (and see #21 for how to reference the cross-section field cleanly).

---

## Summary

- **19 of 23 are straightforwardly valid** (✅). The reviewer clearly understands the
  intended architecture, and the code backs it up.
- **4 carry caveats** (⚠️ #1, #2, #20, #21) — all "yes, but do X alongside it," not
  disagreements.
- **2 are design tasks / questions** (🔷 #11, ❓ #12) — they propose *new* schema
  mechanisms (no-op marker, role vocabulary), so they need a small design decision, not
  just an edit.
- **No command is invalid or wrong.**

### Highest-leverage, lowest-risk order to execute
1. **A (strip prompt/IVR/policy/ordering/constraint_library)** — biggest size cut, and
   provably unused by the backend. Do #3/#4/#5/#6/#7 first, then #1+#2 together (after
   guaranteeing inline `enum`s).
2. **C + D (per-CPT groups + nested JSON, kill blobs & dotted keys)** — aligns the
   backend schema with the frontend's already-correct shape; fixes the mismatch you
   first reported.
3. **B + E (read-only/no-op markers, renames, cross-section paths)** — needs the small
   design decisions in #11/#12/#21 first.

### Watch-outs when implementing
- Removing `constraint_ref`/`constraint_library` is only safe if every choice field
  keeps an explicit `enum` (frontend `resolveOptions` fallback).
- Splitting blobs / converting dotted keys / renaming `section_key`s changes stored
  `field_answer` **paths** — fine pre-launch, needs a migration if real data exists.
- The "no-op section" marker (#11) and cross-section path refs (#21) require
  corresponding **consumer code** changes (prompt-compiler; `validation.ts`), not just
  schema edits.
