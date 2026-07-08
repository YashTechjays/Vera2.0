# Task prompts in the form-schema DSL — introduction task, placeholder contract, STT key terms

**Date:** 2026-07-06 (STT key terms added 2026-07-08)
**Status:** Approved
**Amends:** `2026-07-02-form-schema-dsl-v2-design.md` §4.6 (tasks), §5 (task builder / prompt compiler contracts)

## 1. Problem

The v2.1 DSL gives every task `intro` / `outro` / `prompt`, but the IBV catalog fills
them with thin one-liners and the call's opening ritual — the agent introducing itself
to the insurance representative and establishing that the patient is on the plan — is
not representable at all: the spec deliberately pushed it to the runtime prompt
pipeline. In practice that split makes the LiveKit AgentTask mapping guessy: the task
builder has to invent what the agent says when a task starts and ends, and the
verification behavior (what the rep may ask to establish the call is legitimate) lives
nowhere near the schema that knows the patient's identifiers.

SmartCaller (Vera 1.0) solved this with per-phase prompt modules
(`src/pipecat_module/prompts/phases/phase_*.py`). This design ports the *task-specific*
parts of those phases into the schema DSL and defines the placeholder contract that
lets one schema serve every patient form.

## 2. Goals / non-goals

**Goals**

- A schema-defined greeting/verification task that runs before `insurance_basics`,
  has no section questions, and carries the exact spoken introduction script.
- A crisp LiveKit mapping for every task: `intro` = verbatim speech on task entry,
  `outro` = verbatim speech on task exit, `prompt` = agent instructions. No guessing
  at task-definition time.
- `{{system_field_key}}` placeholders in task-level text, hydrated per patient form at
  task creation, validated at document-validation time.
- Port the remaining task-specific SmartCaller content (wrap-up critical-fields rule,
  hold-phrase outro).
- A document-level `stt_key_terms` vocabulary fed to the STT component to improve
  transcription of domain terms — session-wide, applying to every task.

**Non-goals**

- IVR navigation (provider playbooks), the gap-analysis phase, and the final goodbye /
  end-call ritual remain runtime stages composed around the schema tasks.
- No new `Task` model fields; no `dsl_version` bump — the only grammar addition
  (`stt_key_terms`) is optional and additive (§6).
- The runtime task builder / prompt compiler implementation (this branch's next step)
  is specified only at the contract level here.

## 3. Task contract (LiveKit AgentTask mapping)

For every task in `tasks`:

| Key | LiveKit AgentTask meaning | Placeholders |
|---|---|---|
| `intro` | Spoken verbatim when the task starts (TTS-safe text — no stage directions; pacing via ellipses) | yes |
| `outro` | Spoken verbatim when the task completes; also masks next-task spin-up latency | yes |
| `prompt` | Supplied directly as the agent's task instructions | yes |
| `sections` | The form sections whose `ask`/`confirm` fields the task collects; **may be empty** for ritual tasks that collect nothing | — |

A task with `sections: []` is a first-class shape: it exists purely for conversational
behavior (the introduction task). The UI and the intake/review readers never consume
`tasks`, so nothing renders differently.

### 3.1 Placeholder namespace

Task-level `intro` / `outro` / `prompt` may embed `{{token}}` where `token` is a key of
the document's top-level `system_fields` map (e.g. `{{patient_name}}`,
`{{member_id}}`, `{{hospital_npi}}`). That is the whole namespace — field paths,
`{{value}}` (field-level confirm prompts) and `{{current_year}}` (derive templates)
are separate, unchanged namespaces that do not apply to task text.

**New validator rule:** every `{{token}}` occurring in any task's `intro`, `outro`, or
`prompt` must resolve to a defined `system_fields` key. Unknown tokens are a document
validation error (caught at compile/seed time, never at call time).

### 3.2 Hydration contract (task builder, runtime)

At call initiation the task builder resolves each placeholder through
`system_fields[token]` → field path → the form's intake `field_answer` value, falling
back to the field's `default` (e.g. `callback_number` → `"N/A"`) when unanswered.

**PHI:** hydration happens inside the trust boundary, and raw patient identifiers
never land in an LLM prompt. Text destined for the LLM is hydrated with the session's
`[[TYPE_N]]` PHI tokens; re-identification happens at the TTS seam
(`vera_core.phi.hydrate_for_speech`) so the caller *speaks* real values. Non-PHI
handles (facility/provider identifiers) may hydrate raw. The schema stays neutral —
placeholders carry no PHI marking; the task builder decides token-vs-raw per seam.

## 4. New `introduction` task (SmartCaller Phase 2 START)

First entry in `tasks`, `task_key: "introduction"`, title "Introduction & Patient
Verification", `sections: []`.

**intro** (adapted from `phase_2_basics.py::PHASE_2_START`; `{clinic_name}` →
`{{hospital_name}}`, `[pause]` dropped — TTS would read it aloud):

> Hello, I'm VERA, an AI Virtual Assistant... calling from {{hospital_name}}, on
> behalf of Dr. {{doctor_name}}. Before we begin... I'd like to let you know that this
> call is being recorded for quality and training purposes. Also, please note that...
> this call is supervised by my human manager, {{verified_by}}, who may intervene if
> necessary. I'm looking at the details for... {{patient_name}}, date of birth
> {{patient_dob}}. Could you let me know if this matches the name on the plan?

**prompt** (the verification behavior contract):

- Deliver the introduction exactly once, calmly; if interrupted, continue from where
  you left off — never restart it.
- Wait for the representative to confirm they can see the patient AND introduce
  themselves. "Let me check", "hold on", "one moment", "give me a second" and similar
  are NOT confirmations — say "Take your time" once, then stay silent until they
  return. A bare "yes" without the rep introducing themselves is NOT a confirmation —
  keep waiting.
- If the representative cannot find the patient, provide the member ID {{member_id}}
  and the insurance provider {{insurance_provider_name}}.
- If the representative asks questions to verify the call is legitimate, answer from
  these details: patient {{patient_name}}, date of birth {{patient_dob}}, member ID
  {{member_id}}, facility {{hospital_name}} at {{hospital_address}}, facility NPI
  {{hospital_npi}}, tax ID {{hospital_tax_id}}, ordering provider Dr. {{doctor_name}}
  with NPI {{doctor_npi}}, callback number {{callback_number}}.
- After this task, never re-introduce yourself for the rest of the call.

**outro:** "Great, let me pull up my questions..." — plays while `insurance_basics`
spins up; `insurance_basics` therefore keeps no `intro` and its first ask lands
immediately after.

## 5. SmartCaller phase → task mapping (existing tasks)

| SmartCaller phase | Task | Change |
|---|---|---|
| Phase 1 IVR | — | runtime (IVR playbooks) — unchanged |
| Phase 2 START | `introduction` (new) | §4 above |
| Phase 2 questions | `insurance_basics` | no intro (by design); prompt unchanged — OON early termination already lives in `flow_rules.no_out_of_network_coverage` |
| Phase 3 | `coverage` | intro/outro already match; unchanged |
| Phase 4 | `financial` | intro/outro already match; unchanged |
| Phase 5 male partner | `male_partner` | intro/outro already match; unchanged |
| Phase 5 admin | `closing_admin` | intro/outro already match; unchanged |
| Phase 5 closing ritual | `wrap_up` | prompt gains the critical-fields rule: the representative's name and the call reference number must be actual values — never accept "None", "Unknown", "Not provided" or any placeholder. outro added (hold phrase, masks gap-analysis latency): "Perfect, I have everything I need. Let me take a quick moment to review my notes and make sure I haven't missed anything. One moment please." |
| Phase 6 gap analysis | — | runtime — unchanged |

The final goodbye / end-call phrase is deliberately NOT a task outro: gap analysis may
re-open questions after `wrap_up`, and call termination is a tool call in Vera 2.0,
not a phrase trigger.

## 6. `stt_key_terms` — session-wide STT vocabulary

New optional top-level key on `FormSchemaDoc`, alongside `system_fields`:

```jsonc
"stt_key_terms": ["intrauterine insemination", "IUI", "coinsurance", ...]
```

**Semantics.** A flat list of domain terms fed verbatim to the STT component when the
voice session is built — `deepgram.STTv2(model="flux-general-en", keyterms=terms)` in
`agent_worker/cascade.py::build_session`. STT is constructed once per session, so the
terms apply to every task for the whole call; they are deliberately NOT per-task.
Plain strings only: Flux keyterm prompting takes no boost weights (nova-2 `keywords`
did; that model is not in play).

**Validator rules.** Every term is a non-empty trimmed string (multi-word phrases
count as one keyterm); no case-insensitive duplicates; at most 100 terms (Deepgram's
keyterm-prompting limit); no `{{placeholders}}` — key terms are static domain
vocabulary and are never hydrated. Being schema-level and shared across all patients,
per-patient PHI in key terms is impossible by construction.

**Rejected shapes.** Per-task terms (STT is per-session; requirement is session-wide)
and auto-derivation from schema titles/enum values (implicit and noisy — it would
sweep in "Yes"/"No"/"N/A"; an authoring helper can come later).

**Initial IBV vocabulary** (authored in `catalog/ibv_standard.py`, ~55 terms):

- *Treatments:* intrauterine insemination, IUI, in vitro fertilization, IVF,
  ovulation induction, egg cryopreservation, embryo cryopreservation, frozen embryo
  transfer, embryo biopsy, semen analysis, sperm cryopreservation, infertility
- *Plan/benefits:* coinsurance, copay, deductible, out-of-pocket maximum, lifetime
  maximum, prior authorization, coordination of benefits, policy situs, PPO, HMO,
  EPO, POS, self insured, fully funded, benefit year, plan year, telehealth,
  PCP referral, infertility plan mandate, cycle limit
- *Admin:* pharmacy benefit manager, third party administrator, specialty pharmacy,
  member ID, group number, NPI, tax ID
- *Common answers* (the enum values the extractor records — misrecognition here costs
  a field): covered, not covered, in network, out of network, individual, family,
  spouse, dependent, primary, secondary, tertiary, small group, large group,
  no limit, unlimited

CPT codes are deliberately excluded — they are spoken as digit strings, where keyterm
boosting does not help. The common-answer terms are ordinary English words, which
keyterm prompting can over-trigger on; if live-call tuning shows over-recognition,
prune from that group first.

**Versioning.** Additive optional key; `_Model` is `extra="forbid"` but validator and
documents ship together in-repo, so no `dsl_version` bump and no intake/review/
frontend gate changes (the UI subset ignores voice-only keys).

## 7. Implementation surfaces

1. `vera_core/forms/dsl.py` — the placeholder validator rule (scan task
   `intro`/`outro`/`prompt` for `{{token}}`, require membership in `system_fields`);
   `Task` doc comment documenting the LiveKit mapping + placeholder contract;
   `FormSchemaDoc.stt_key_terms` + its validator rules (§6).
2. `vera_core/forms/catalog/ibv_standard.py` — the `introduction` task; `wrap_up`
   prompt + outro additions; the `stt_key_terms` list. Compiled JSON is generated —
   never hand-edited; run `just compile-schemas` and let the freshness + round-trip
   tests gate drift.
3. Spec `2026-07-02-form-schema-dsl-v2-design.md` — amend §4.6 ("only
   form-collection tasks") to admit schema-defined ritual tasks with `sections: []`;
   document the placeholder namespace, validator rule, and PHI hydration note in §5;
   add `stt_key_terms` to the document grammar (§4) and the task-builder contract (§5).
4. Tests (`tests/unit/forms/test_schema_dsl.py` area) — unknown placeholder rejected;
   known placeholder accepted; `sections: []` task valid; `stt_key_terms` duplicate /
   over-cap / placeholder-bearing lists rejected; recompiled artifact fresh.
5. `just check` + code-simplifier pass before commit (repo rule).

## 8. Edge cases

- Unknown `{{token}}` in task text → document validation error listing the task key
  and the offending token.
- `{{` without a closing `}}` → not a placeholder; left as literal text (validator
  matches complete `{{token}}` tokens only).
- Placeholder whose system field has no intake answer → task-builder falls back to
  the leaf's `default`; schema guarantees only that the key exists.
- Seeding: the published document changes (new task + changed strings), so
  `just seed-schemas` publishes a new `schema_version` — expected, order-sensitive
  equality is the mechanism.
