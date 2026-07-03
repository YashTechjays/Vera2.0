# IBV Schema Correction — Decision Log

**Branch:** `feat/ibv-schema-cleanup` (off `main`). Not committed yet.
Working file (source of truth for now): **`vera-frontend/src/lib/ibv/ibv-schema.json`**
(the frontend schema is the latest; it will be migrated into the backend later).

Comments being worked through: `ibv-schema-comments.txt`
Full validity assessment: `ibv-schema-annotations-assessment.md`

Conventions agreed:
- **Read-only / context fields:** use `prompt_role: "context"` (bot never asks it) and,
  where the form should be non-editable, also `read_only: true`. `read_only?: boolean`
  is declared on `IbvField` in `types.ts`.
- **Cross-section references in rules:** use the **dotted** path `section_key.field`
  (e.g. `benefit_coverage.coverage_type`), NOT the `sections/…/…` slash form. Dotted
  works with the existing `validation.ts:conditionScope` fall-through — no code change.

## Progress

| # | Comment | Decision | Status |
|---|---------|----------|--------|
| 1 | Remove `constraint_library` (+ `constraint_ref`) | **Skip for now**, keep it. Revisit later. | ⏸️ deferred |
| 2 | `chart_number` read-only, not a prompt question | Set `prompt_role: "context"` **and** `read_only: true`. | ✅ done |
| 3 | `patient_name` context-only | **Keep as-is** (`verifiable_question` + `confirm_only`) for now — the bot's identity-verification step needs it. May add `read_only` later. | ⏸️ keep-as-is |
| 4 | Cross-section ref in the field key (not `summary`) | Applied to all 5 rules using **dotted paths** (7 refs). Tests green. | ✅ done |
| 5 | Remove `summary` from `rules` | Removed all 6 `summary` keys from the rule blocks. Tests green (26). | ✅ done |
| 6 | Remove `constraint_ref` (pairs with #1) | Deferred with #1. | ⏸️ deferred |
| 7 | Diagnostic testing → per-CPT groups (covered/copay/coinsurance/prior_auth) | Frontend schema **already** has this shape; backend to catch up later. | ✅ already in FE |
| 8 | Male-partner section conditionally mandatory | Section-level `rules`: required when `benefit_coverage.coverage_type = Family` **AND** `patient_information.spouse_gender = Male`. Recorded as intent; **enforcement in validation.ts is a follow-up** (section rules not read yet). `rules?` added to `IbvSection` type. Tests + tsc green. | ✅ done |

**Follow-up (deferred):** wire `validation.ts` to enforce section-level `rules` (today only field-level "make this required" is enforced).
| 9 | Remove "verbatim from v1" descriptions | Removed **all** verbatim-from-v1 descriptions (5 total, section + group level: infertility_treatment_overview, embryo_cryo_storage, third_party) — chose to delete entirely. Tests + tsc green. | ✅ done |
| 10 | `group_integrity` alone is meaningless | **Skip / keep.** NOT meaningless — `vera-schema-builder` emits it (decompose/convert tools) and its validation/faithfulness harness uses it (`validation_harness.py`, `shadow_compare.py`) to mean "collect the coverage group all-or-nothing; short-circuit if covered=No"; 2 builder tests assert it. Removing = a coordinated schema-builder change, not a FE edit. Left as-is. | ⏸️ keep |
| 11 | No flat dotted keys — use nested JSON | Verified: **0** dotted property keys in the FE schema (transform already nested everything). | ✅ done (already) |
| 12 | Rename `third_party` → `third_party_administrator` | Renamed `section_key`. No FE code referenced it; 26 tests pass. Caveat: `vera-frontend/scripts/transform_ibv_percpt.py` still says `move_after(...,"third_party")` — harmless unless regenerated. | ✅ done |
| 13 | Split `third_party` blob (exists + name) | Split into `third_party_administrator_exists` (YES_NO radio, required) + `third_party_administrator_name` (text, conditionally required when exists=Yes). Dropped combined `verbatim_prompt`. Tests + tsc green. | ✅ done |
| 14 | Split pharmacy blob (PBM + phone) | Already 2 props; renamed `pbm_phone_number` → `pharmacy_benefit_manager_phone_number` for exact reviewer naming. | ✅ done |
| 15 | Split ISP blob (exists + name + phone) | Added `isp_exists` (YES_NO gate, required); renamed name prop → `isp_name` (conditionally required when exists=Yes); kept `isp_phone_number`. Dropped `verbatim_prompt`s. | ✅ done |
| 16 | Split `insurance_representative` (+ add rep_name, call_ref) | Already split — `insurance_rep_name`, `call_reference_number`, `web_portal_ref_number` all present. Reviewer's "missing fields" already exist. Nothing to do. | ✅ done (already) |
| 17 | Provider/hospital section = context-only (bot replies, UI renders) | Set `prompt_role: "context"` + `read_only: true` on all fields of **both** `hospital_information` and `provider_reference_information`. Tests + tsc green. | ✅ done |
| 18 | Add `no_op` marker for UI-only sections (`form_information`) | Added `"no_op": true` to `form_information`; declared `no_op?` on `IbvSection`. FE still renders it; flag is a signal for the future prompt/agent-task compiler to skip it (not enforced yet). | ✅ done |

**Follow-up (deferred):** prompt/agent-task compiler should honor `no_op` (exclude those sections) and `read_only`/`prompt_role: "context"` (don't ask, render non-editable).
| 19 | Reconsider `intro_prose` role on non-prompt sections | Removed section-level `prompt_role: "intro_prose"` from all 10 sections — it's the auto-assigned default, consumed by no renderer (only builder migration/tests). Field-level roles untouched. Tests + tsc green. Caveat: builder would re-add on regen (we don't regen). | ✅ done |
| 20 | Remove `source` from policy objects | **Deferred — required by prompt generation.** `vera-schema-builder/assemble.py:126,128` uses `source` as the **key to pool/dedupe policy blocks** ("section wins on name clash"). Removing it breaks policy assembly. | ⏸️ deferred |
| 21 | Remove rejection handling from schema | **Deferred — required by prompt generation.** Rejection-handling entries live in `global_policies`, which `assemble.py:124` pools into the bot prompt. Removing loses that behavior. | ⏸️ deferred |
| 22 | Remove IVR script from schema | **Deferred — required by prompt generation.** IVR-script entries (Aetna/Cigna/UHC) live in `global_policies` → composed into the prompt by `assemble.py:124`. Removing breaks IVR navigation. | ⏸️ deferred |
| 23 | Remove field-order array | **Deferred — required by prompt generation.** `field_list_order` (`assemble.py:5,8`) sets where the field list renders in the phase prompt; `phase_order` (`assemble.py:441`) drives block order ("the SCHEMA drives the order"). Removing degrades prompt ordering. | ⏸️ deferred |

### ⚠️ Key correction (found via "how does this impact prompt prepare?")
My original assessment marked the prompt/policy material as "safe to strip" because the
**Vera2.0 runtime** ignores it (FE renderer + backend intake/review don't read it). That
was incomplete: the **`vera-schema-builder`** is the prompt *generator*, and its
`assemble.py` / `compose_body.py` build the bot prompt **directly** from this schema —
pasting `verbatim_prompt` (exact question wording), pooling `global_policies` /
`section_policies` keyed by `source`, and ordering by `phase_order` / `field_list_order`.

**So this schema IS the prompt-generation source.** Stripping prompt material is NOT a safe
edit — it's a coordinated schema-builder refactor (re-point `assemble.py` at another input).
#20–23 (and the broader "strip all policies/verbatim_prompt" idea) are therefore **deferred**,
same category as #10 (`group_integrity`) and #19 (`intro_prose`).

**Side note:** the `verbatim_prompt` we dropped when splitting blobs in **#13** (third_party)
and **#15** (ISP) removed those fields' exact ask-wording. Low-risk (short prompts; sections
were being restructured), but on record — restore/re-author if byte-parity matters there.

## Side task (done)
- **Backend seed for Data Management forms:** added `SAMPLE_PATIENT_FORMS` +
  `_seed_patient_forms()` to `vera-backend/scripts/seed.py`. 8 synthetic forms across
  all statuses, INTAKE field_answers + one AI dispute on the EXCEPTION_REVIEW form.
  Idempotent on `(tenant_id, chart_number)`. Verified live via `just seed`. ✅
