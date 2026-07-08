# Form-Schema DSL v2 — Design

**Date:** 2026-07-02
**Status:** Proposed (schema artifact generated; consumer migration pending)
**Artifacts:** this spec + `vera-backend/data/form_schemas/ibv_form_standard_v2.json`

## 1. Context and goals

The compiled form schema (`schema_version.schema_json`) must be the single source of
truth for three consumers:

1. **UI rendering** — the form the clinic staff sees and edits.
2. **Voice-agent prompt generation** — per-task instructions for the LiveKit agent
   (sections map to agentTasks built dynamically at call time).
3. **Transcript-driven data extraction** — answers written to `field_answer` rows keyed
   by `field_path`.

The v1 schema (`ibv_form_standard.json`) was AI-generated to mirror the handwritten
`ibv_standard_prompt.json` and was never reviewed. A parity analysis (§2) shows it
cannot drive any of the three consumers reliably. The tech lead annotated a copy
(`vera-backend/ibv_form_standard_annotated.jsonc`) with 24 comments; every one is
resolved by this design (§8).

Non-goals: wiring the seeded prompt to the live agent, building the extraction
pipeline, or migrating consumer code — those are follow-up work items (§9). Today the
live agent runs a hardcoded prompt (`agent_worker/prompt.py`) and no `ai_call`
extraction exists, so the agent-facing half of the DSL has **no live consumer to
break** — it can be designed fresh.

## 2. Parity verdict: v1 schema vs. prompt

**The v1 schema is not in parity with the prompt.** Beyond phrasing drift, the schema
cannot even *store* much of what the prompt collects:

| Gap class | Examples |
|---|---|
| Asked by prompt, **no field exists** | all 6 out-of-pocket values, 2 deductible-remaining values, lifetime max (total/met/remaining/applicable area/notes), `embryo_cryo_storage_time`, `rep_name`, `call_reference_number`, `cob_status`, `renewal_date`, telehealth answer (fused into `referrals_telehealth`), `center_of_excellence_required`, lifetime cycle max/used, web portal reference, employer name |
| Branch variable with **no field** | `infertility_tx_covered` (the master coverage gate), `male_partner_covered`, `pbm_exists`, `isp_exists`, `tpa_exists`, `enrollment_required` |
| One string blob holding 2–5 data points | 9 sections (`deductibles_oop`, `infertility_limits`, `enrollment`, `authorization_department`, `third_party`, `pharmacy`, `infertility_specialty_pharmacy`, `embryo_cryo_storage`, `insurance_representative`) — extraction has no per-value path, UI has no discrete inputs |
| Conditional logic prose-only or absent | skip male-partner section (Family + male spouse), skip family deductible/OOP (Individual), skip auth department (no prior auth anywhere), skip PBM/ISP/TPA details (none exists), per-service sub-field gates (covered=Yes), calendar-year effective-date derivation, out-of-network early termination |
| Role information absent | context-only vs confirm vs ask vs read-only vs UI-only exists only in free prose; 102/115 fields carry `prompt_role: "question"` including fields that must never be asked |
| Extraction namespace mismatch | prompt `<record>` names ≠ schema field keys throughout (`iui` vs `infertility_treatment_intrauterine_insemination`, `plan_type`/`cob_status` vs `health_plan`, …) |
| Dead/contradictory constructs | `constraint_library` + inline `enum` duplication, `field_list_order` (an integer named as a list), `phase_order` referencing Python constants (2 of them undefined), triple-encoded requiredness with verified mismatches, `source` keys pointing at v1 Python files, embedded IVR scripts for 3 payers |

The prompt itself contains internal contradictions (auth-department requiredness stated
three conflicting ways; Phase-4 asks 12 deductible/OOP questions but requires only 4 in
its completion contract; per the user requirement, family deductible/OOP **should** be
conditional on coverage type but the prompt asks them unconditionally). The v2 schema
resolves these in favor of the stated business rules.

## 3. Approaches considered

1. **Patch v1 in place** — add the missing fields and bolt roles onto the existing
   shape. Rejected: the blob fields, dotted pseudo-keys, triple-encoded requiredness and
   prompt-content-in-schema are structural; patching preserves the ambiguity the
   annotations call out.
2. **Standard JSON Schema + vendor extensions** (`x-vera-*` keywords). Rejected: we get
   no value from JSON Schema tooling (values are transcribed strings like "No Limit",
   not machine-validated payloads), and the important semantics (roles, tasks,
   applicability, derivation) would all live in extensions anyway — worst of both.
3. **Purpose-built compiled DSL (chosen)** — a small, closed vocabulary designed around
   the three consumers, with a validator. The schema-builder authors it; the compiled
   artifact is self-contained (no source refs, no library indirection except
   `shared_conditions`, which are referenced rather than duplicated).

## 4. DSL v2 specification

### 4.1 Document shape

```jsonc
{
  "dsl_version": "2.1",
  "name": "IBV Form Standard",           // form_schema.name / prompt.name
  "insurance_type": "infertility_treatment",
  "description": "…",                     // optional, top-level human description of the form
  "system_fields": { "<handle>": "field_path", ... },  // optional, see below
  "stt_key_terms": ["intrauterine insemination", ...],  // optional; session-wide STT vocabulary
  "shared_conditions": { "<name>": Condition, ... },   // optional
  "sections": { "<section_key>": Section, ... },       // object; key order = UI order
  "tasks": [ Task, ... ],                 // document order = call order
  "flow_rules": [ FlowRule, ... ],        // optional
  "contradictions": [ Contradiction, ... ] // optional cross-field consistency rules
}
```

Ordering is always **document order** — there is no separate ordering construct
(resolves `field_list_order`).

**`system_fields`** binds well-known system handles to schema paths so integrations
never hard-code per-form paths: intake and the promoted `patient_form` columns
(`member_id`, `patient_name`, …), the IVR navigator's context handles
(`hospital_npi`, `doctor_name`, …), and system-written values (`form_queued_by` →
`verified_by`). Each insurance type's schema declares where its version of each handle
lives — this replaces Vera 1.0's `primary_field_mapping`, `ivr_field_mapping` and
`system_mappings` blocks with one map.

### 4.2 Path grammar (the identity contract)

`field_path = "sections." section_key ( "." field_key )+` — rooted at the document, so
every reference is traceable from the root node
(`sections.patient_information.patient_name`). Paths are produced by real JSON nesting,
never by dots inside keys, and are byte-identical to `field_answer.field_path`
(String(255)): one namespace shared by the schema, conditions, `system_fields`,
extraction, intake payloads and the UI value map. Keys match `^[a-z][a-z0-9_]*$` and
are unique within their parent.

### 4.3 Section

```jsonc
"deductibles": {                      // the object key IS the section_key
  "title": "Deductibles",
  "role": "collect",                  // collect (default) | context | ui_only
  "description": "…",                 // optional, human help text ONLY — never agent directives
  "applicable_when": Condition,       // optional
  "codes": Codes,                     // optional (see 4.7)
  "prompt": { "intro": "…" },         // optional one-line section opener
  "ask_groups": [                     // optional combined asks over sibling fields
    { "fields": ["sections.pharmacy_benefit_manager.pbm_name",
                 "sections.pharmacy_benefit_manager.pbm_phone"],
      "ask": "What is the name and contact phone number of the pharmacy benefit manager?" }
  ],
  "alternatives": [                   // optional either/or sets (see below)
    { "members": ["sections.general_coverage.asc_professional",
                  "sections.general_coverage.asc_facility"],
      "ask": "…" }
  ],
  "ui": { "layout": "table" },        // optional render hints
  "fields": { "<key>": Field, ... }
}
```

**Section roles** (this is the "what is this section for" marker the annotations demand):

| role | agentTask | prompt | UI |
|---|---|---|---|
| `collect` | member of exactly one task | questions generated from its fields | rendered, values live-updated |
| `context` | never | values injected as known-background ("provide if asked, never volunteer") | rendered, human-editable |
| `ui_only` | never | completely absent from prompt compilation | rendered, human-editable |

**`ask_groups`** — one spoken question that collects several *distinct* schema fields
("What is the group name and group number?"). A conversational overlay only: the data
model is untouched — every member still records at its own `field_path`. The compiler
speaks the combined `ask` when **all** members are applicable and unanswered; each
member's own `prompt.ask` remains mandatory and is the fallback for partial answers
(rep gave one value of two), gap re-asks, and when only some members are applicable.
Members are full paths that must be `ask`-role leaves of this section (≥ 2 per group; a
field may belong to at most one ask group). For a `group`'s children the same effect
comes from the group-level `prompt.ask` opener — `ask_groups` covers flat siblings that
do not form a data unit.

**`alternatives`** — either/or sets: several members (leaves *or* groups of this
section) of which the rep realistically supplies one (ASC professional vs facility, egg
cryo elective vs cancer-related, copay vs coinsurance per service). Once one member has
data, the others stop being asked, the set counts complete, and unanswered members
(and their descendant leaves) are auto-recorded `"N/A"`; if the rep volunteers several,
all are recorded. The optional `ask` is a generic opener for the set ("…is that billed
as professional or facility?"); without it, the members' normal asks/ask_groups apply
and `alternatives` contributes only the completion/auto-N/A semantics (used for the
per-service copay/coinsurance pairs). A member belongs to at most one alternatives
entry.

### 4.4 Field

```jsonc
{
  "type": "text | enum | date | currency | percent | integer | phone | group",
  "title": "Copay",
  "role": "ask | confirm | context | readonly | input",   // ALWAYS explicit on leaves in the
                                                           // compiled artifact; the section-role
                                                           // default is an authoring convenience
  "required": true | { "when": Condition },               // default false
  "values": ["Yes", "No", "N/A"],       // enum only, inline, single source of truth
  "special_values": ["No Limit"],       // typed fields: extra verbatim-legal answers
  "default": "N/A",                     // optional: value assumed when nothing was recorded
  "validation": {                       // optional constraints, extensible
    "pattern": "^[0-9]{10}$",           //   regex for text-family values (NPI, tax ID)
    "range": { "min": 0, "max": 100 }   //   numeric bounds for currency/percent/integer
  },
  "applicable_when": Condition,         // optional
  "inapplicable_value": "N/A",          // optional: value auto-recorded when skipped as inapplicable
  "derive": { "when": Condition, "value": "01/01/{{current_year}}" },  // optional
  "confirm_in_task": "insurance_basics",// only on role=confirm fields in context sections
  "tags": ["prior_auth"],               // optional labels for cross-cutting runtime rules
  "integrity": "all",                   // groups only: all (default) | any — completion semantics
  "prompt": {
    "ask": "What is the copay amount for this service?",   // role=ask (and group openers)
    "confirm": "I have {{value}} — can you confirm that is correct?",  // role=confirm
    "hints": ["Pronounce as: in-fer-TIL-ih-tee plan MAN-date."]  // short, field-scoped guidance only
  },
  "codes": Codes,                       // optional
  "ui": { "widget": "textarea" },       // optional; widget otherwise derived from type
  "description": "…",                   // optional human help text
  "fields": { ... }                     // type=group only; children recurse
}
```

**Field roles** — default is `ask` in `collect` sections, `context` in `context`
sections, `input` in `ui_only` sections:

| role | in prompt | voice writes `field_answer`? | UI |
|---|---|---|---|
| `ask` | agent asks `prompt.ask` | yes (`source=ai_call`) | editable |
| `confirm` | agent speaks `prompt.confirm` with `{{value}}` = the field's current answer (intake/human), rep confirms or corrects | yes (confirmed or corrected value) | editable, pre-filled |
| `context` | injected as known information; agent answers if asked, never volunteers, never asks | never | editable |
| `readonly` | absent entirely | never | display only |
| `input` | absent entirely | never | editable |

`confirm` resolves the v1 `{member_id}`-style placeholder-namespace problem: the value
to confirm **is the field's own current answer** — no second namespace exists.

`confirm_in_task` covers the one legitimate cross-cutting case: a confirm field living
in a `context` section (spouse name/DOB in `patient_information`) that must be spoken
during a specific task. Such fields are asked at the **end** of the named task, after
its sections complete — by then any answers their `applicable_when` depends on (e.g.
`coverage_type`, asked mid-task) exist. It is the only field-level task reference;
everything else maps via sections.

> **Amended 2026-07-08** (by the prompt-compiler design §3.4): `confirm_in_task`
> widens from a plain task key to
> `{"task_key": "…", "confirm_immediate": true|false}` (the string form stays legal
> and means `confirm_immediate: false`). `confirm_immediate: true` speaks the
> confirmation **immediately after the gating question is answered** (spouse
> name/DOB right after `coverage_type` = Family), not at the task's end; `false`
> keeps the end-of-task behavior described above.

**Types** drive both the UI widget default and the extraction/normalization target:
`enum`→select, `date`→date input, `phone`→tel, `currency`/`percent`/`integer`→validated
text, `text`→input (or `ui.widget: "textarea"`). `special_values` lists the
non-numeric answers the payer world produces ("No Limit", "Met", "$0", "None") so
extraction has an explicit vocabulary instead of prose tables.

**`derive`** replaces v1's value-less `auto-fill a value` rule: when `when` is true the
field is not asked and `value` is recorded (template vars: `{{current_year}}`; the set
is closed and documented here — extend deliberately).

**Groups** replace all three v1 shapes (nested objects, dotted keys, prose `<record>`
blobs). A group may carry its own `prompt.ask` — the conversational opener covering its
children ("Can you provide coverage and benefit details for IUI?"); children keep
individual asks for follow-ups and gap re-asks. **`integrity`** (groups only) defines
completion semantics with the meaning v1's `group_integrity` never had: `all` (default)
— every applicable required child must be answered for the group to count complete;
`any` — the group counts complete once at least one applicable child has a value, and
its remaining children drop off the gap re-ask list. Used by completion %, post-call
form validation, and gap analysis.

**`inapplicable_value`** delivers "skip *and* fill defaults": when a field is skipped
because its own or an ancestor's `applicable_when` is false, the runtime auto-records
this value instead of leaving the field blank (e.g. infertility not covered ⇒ every
service records `covered: "No"`, `copay: "$0"`, `coinsurance: "0%"`, `prior_auth/
cycle_limit/notes: "N/A"`). The declared value is legal by declaration — it need not
appear in `values`/`special_values`. Fields without it are simply left blank when
skipped (e.g. diagnostic-testing sub-fields).

**`default`** is the value the form assumes when nothing was recorded (display, export
and completion treat the field as filled with it) — Vera 1.0's `default: "N/A"`
convenience. Distinct from `derive` (conditional auto-fill during the call) and
`inapplicable_value` (fill on conditional skip); like `inapplicable_value`, the
declared value is legal by declaration. **`validation`** is a nested, extensible
constraint object checked at intake and extraction time: `pattern` (regex for
text-family fields — NPI `^[0-9]{10}$`, tax ID `^[0-9]{9}$`) and `range` (numeric
bounds — copay `{min: 0}`, coinsurance `{min: 0, max: 100}`); future constraints slot
in without new top-level keys. On `text` fields, `special_values` doubles as the
*canonical suggested values* (e.g. `plan_type` is free text — real plans overflow any
enum — with `["PPO", "HMO", "EPO", "POS"]` as normalization targets and UI
suggestions).

**`tags`** label fields for cross-cutting *runtime* rules that no static condition can
express because they trigger on an utterance, not a field value. Example: when the rep
says prior auth is handled by a separate department, the agent's tool fills every
remaining field tagged `prior_auth` with `"Prior auth department"` (legalized via
`special_values`, which is therefore allowed on `enum` fields too) and stops asking it
— while all other questions continue.

### 4.5 Conditions

```jsonc
{ "field": "sections.benefit_coverage.coverage_type", "op": "eq", "value": "Family" }
{ "all": [ C1, C2 ] }   { "any": [ ... ] }   { "not": C }
{ "ref": "family_coverage" }        // resolves into shared_conditions
```

Ops: `eq`, `ne`, `in`, `not_in` (list value). Field references are always **full
root-anchored paths** — no bare names, no prose `summary` (annotations C5/C6).
`shared_conditions` exists for genuinely reused predicates (e.g.
`any_service_requires_prior_auth`, an `any` over all 27 `prior_auth` paths — a
*predicate over* the per-service fields, not a duplicate of them); unlike v1's
`constraint_library`, a `ref` is
never duplicated inline, so there is still exactly one source of truth.

`applicable_when` is the single gating construct, valid on sections, groups and fields.
Not-applicable ⇒ not asked, not required, not counted in completion %, rendered
inactive/hidden by the UI. `required` (bool or `{when}`) is the **only** requiredness
encoding — v1's section `required[]` arrays and `required_state` are gone.

Conditions are evaluated lazily against current `field_answer` values (an answer given
earlier in the call, or intake data), re-checked whenever a referenced field changes.
The validator ensures every path resolves to a defined leaf field.

### 4.6 Tasks (section → LiveKit agentTask mapping)

```jsonc
{ "task_key": "financial", "title": "Financial Details",
  "intro": "Now let me ask about some financial details.",   // spoken verbatim on task entry
  "outro": "…",   // spoken verbatim on exit; also masks next-task spin-up latency
  "prompt": "…",  // supplied directly as the agent's task instructions
  "sections": ["deductibles", "out_of_pocket", "lifetime_maximum", "embryo_cryo_storage"],
  "applicable_when": Condition }                              // optional
```

Rules: every `collect` section belongs to exactly one task; `context`/`ui_only`
sections belong to none. At call initiation the runtime builds the task list in
document order; **each task's applicability is decided when it is about to start**
(conditions usually depend on answers from earlier tasks, e.g. `coverage_type`). A task
is skipped when its `applicable_when` is false or all of its sections are inapplicable
— so the male-partner task needs no condition of its own; its single section carries
`male_partner_in_scope`.

`intro`/`outro`/`prompt` map one-to-one onto a LiveKit AgentTask and may embed
`{{system_field_key}}` placeholders (validated; hydrated per patient form at task
creation). `sections` may be `[]` for ritual tasks that collect nothing. The schema
defines the form-collection tasks **and** the call-opening ritual (the
`introduction` task: verbatim introduction script + patient-membership
verification, with its outcome recorded in a `patient_verification` collect
section and a `patient_not_on_plan` flow rule). IVR navigation (provider-specific
playbooks + the generalized IVR-navigator prompt) and the gap-analysis phase remain
runtime stages; gap analysis is pinned between the last data task and `wrap_up`, so
the representative's name and reference number are collected after every gap is
cleared, and `wrap_up`'s outro is the goodbye (see the 2026-07-06 task-prompts
design for the full closing flow).

### 4.7 Codes

```jsonc
"codes": { "cpt": ["58340", ...], "icd10": ["Z31.41"], "speak_cpt": true }
```

Attachable to sections and groups. `speak_cpt: true` = the agent reads the codes aloud
when asking (diagnostic testing); otherwise codes are provide-if-asked context. This
replaces v1's half-metadata/half-prose split.

### 4.8 Flow rules and contradictions

```jsonc
{ "rule_key": "no_out_of_network_coverage",
  "when": Condition,
  "action": "terminate_call",
  "skip_to_task": "wrap_up",
  "note": "…" }
```

Evaluated after every recorded answer. `terminate_call` + `skip_to_task`: abandon the
remaining tasks, run the named task (still honoring conditions — for IBV that's the
rep-name/reference-number wrap-up), then end the call. The *mechanics* of ending (the
trigger phrase, `complete_phase` etiquette) stay in the prompt pipeline; the schema
states only the data-driven rule. `action` is a closed enum — v1's free-text effect
strings ("make this required" vs `terminate_call_when`) are gone.

**`contradictions`** declare cross-field *consistency* rules — combinations of
recorded answers that are almost certainly wrong, where the agent must push back and
re-clarify rather than silently accept:

```jsonc
{ "rule_key": "mandate_requires_infertility_coverage",
  "when": { "all": [
    { "field": "sections.benefit_coverage.infertility_plan_mandate", "op": "eq", "value": "Yes" },
    { "field": "sections.infertility_treatment.infertility_tx_covered", "op": "eq", "value": "No" } ] },
  "fields": [                              // what to re-clarify, in order (ask/confirm leaves)
    "sections.infertility_treatment.infertility_tx_covered",
    "sections.benefit_coverage.infertility_plan_mandate" ],
  "reason": "If the plan or state mandates infertility benefits, infertility services must be covered under the plan.",
  "clarify": "…" }                         // optional pushback utterance; compiler composes
                                           // one from reason + the members' asks if absent
```

Semantics: evaluated after every recorded answer, like flow rules — including **across
tasks** (a rule may pair an `insurance_basics` answer with a `coverage` answer; the
clarification happens in whatever task is active when the second answer lands, re-asking
member fields even outside their home task, as the gap phase does). The agent pushes
back **once per rule per call**: speak `clarify`, re-ask the member `fields`, record
whatever the rep now says. If the condition still holds after re-clarification, the
values stand as given and the rule fires a *flag* instead of a loop — surfaced to the
review UI (dispute pipeline) as an unresolved contradiction. Conditions compare
recorded answers, so a rule naturally stays dormant until every referenced field has a
value. Distinct from `flow_rules` on purpose: flow rules steer the *call*,
contradictions guard the *data* — and they double as post-call form validation for
human-entered edits.

### 4.9 Removed from the schema, and where each concern now lives

| v1 construct | Disposition |
|---|---|
| `constraint_library` / `constraint_ref` | deleted; inline `values` only (builder may keep a palette on its side) |
| `field_list_order` | deleted; document order |
| `phase_order`, `global_policies`, `section_policies`, `source` | deleted; persona/guardrails/turn-taking/value-normalization/rejection handling belong to the prompt pipeline templates |
| IVR scripts (Aetna/Cigna/UHC), IVR persona | deleted; IVR navigator + per-provider playbooks, selected at call time |
| `verbatim_prompt` XML blobs | replaced by `prompt.ask` / `prompt.confirm` / `prompt.hints` + structured conditions |
| `prompt_role` (3 vocabularies) | replaced by section/field `role` |
| `required[]` + `required_state` + "make this required" rules | replaced by `required: bool \| {when}` |
| `group_integrity`, `metadata` | replaced by `integrity: all\|any` (defined completion semantics) and `codes` |
| Behavioral directives inside `description` | forbidden; validator-lintable (descriptions are human help text) |

### 4.10 Validation rules (implemented in the v2 validator)

Duplicate JSON keys; key/`section_key`/`task_key` regex; known `type`/`role`/`op`/
`action` values; enum⇔`values` pairing; `prompt.ask` present on ask fields and
`prompt.confirm` on confirm fields; groups non-empty; every condition path resolves to
a defined leaf field; `ref` targets exist; every collect section in exactly one task;
task/section role compatibility; `confirm_in_task` targets exist; paths ≤ 255 chars;
`inapplicable_value` only where self or an ancestor is gated by `applicable_when`;
`tags` are snake_case strings; `integrity ∈ {all, any}` and only on groups; section
`prompt` supports only `intro`; `sections` is an object (no redundant inner
`section_key`); `validation.pattern` compiles as a regex and sits only on non-enum,
non-group fields; `validation.range` is numeric `min`/`max` (min ≤ max) only on
currency/percent/integer; `system_fields` handles are snake_case and their paths
resolve to defined leaf fields; `ask_groups` members resolve to `ask`-role leaves of
their own section, at least two per group, with no field in more than one group;
`alternatives` members resolve to leaves or groups of their own section (leaf members
`ask`-role, ≥ 2 per entry, no member in two entries); task `intro`/`outro` are strings;
`contradictions` have unique rule_keys, a valid `when`, a non-empty `reason`, and
`fields` resolving to `ask`/`confirm` leaves (only re-askable fields can be
clarified).
- Every `{{token}}` in a task's `intro`/`outro`/`prompt` must be a defined
  `system_fields` key.
- `stt_key_terms`: ≤ 100 terms, each non-empty and trimmed, no case-insensitive
  duplicates, no `{{placeholders}}` (static vocabulary — never hydrated).

A scratch implementation lives at the session scratchpad (`validate_dsl_v2.py`); it
should be productized as a pydantic model + CI check in the migration (§9).

## 5. Consumer contracts

**UI renderer.** Walk `sections` in order; render by section role (`ui_only`/`context`
editable, `readonly` display-only, collect sections live-updating). Widget from `type`
(+ `ui.widget` override); options from `values` (+ `special_values` accepted on typed
fields); `*` marker and completion % from `required` ∧ `applicable_when`; conditional
visibility from `applicable_when` (v1 had none). `ui.layout: "table"` renders a
group-per-row matrix (replaces the frontend's structural guessing heuristic).

**Task builder (call initiation).** Tasks in document order → LiveKit agentTasks;
lazy applicability evaluation (§4.6); IVR/gap/closing stages composed around them.
The builder hydrates task-text placeholders from `system_fields` → intake answers
(field `default` when unanswered) and passes `stt_key_terms` to the STT component
(`deepgram.STTv2(keyterms=...)`) once per session.

**Prompt compiler (per task).** Persona + global behavior from prompt-pipeline
templates (not the schema); a context block from every `context`-role field's current
answer (intake/human `field_answer` rows — annotation C17); the task's `intro`; per
section → per field in document order: `ask`/`confirm` text, enum vocabulary as the
expected answer set, `hints`, `codes` (spoken vs on-request), with `ask_groups`
substituting one combined question for its members' individual asks on the first pass
(§4.3); conditions rendered as skip instructions and enforced by the task's tool
contract.

**Extractor (observer agent).** For each active section/task, the target set =
applicable `ask`/`confirm` leaf fields (+ `confirm_in_task` fields attached to the
task). The filter is the `role` key alone — never the presence of `prompt` — and since
every compiled leaf carries an explicit role, it is literally
`role in ("ask", "confirm")`. Each answer → one `field_answer` row: `field_path` = schema path,
`source=ai_call`, value validated against `type`/`values`/`special_values`. Skipped
not-applicable fields get their `inapplicable_value` recorded if declared, otherwise no
row. `derive` fields are written by the runtime when their condition fires.
Gap-analysis re-ask list = required ∧ applicable ∧ unanswered, honoring group
`integrity: any`. Two conversational rules operate on top of the static schema:
**blanket-answer propagation** ("same benefits across the board" → copy the value to
the same-named child of the remaining sibling groups in the section and stop re-asking
— the homogeneous group structure makes the targets identifiable) and **tag-scoped
suppression** (e.g. the prior-auth-department tool filling all remaining
`tags: ["prior_auth"]` fields, §4.4).

**Intake / review / completion (existing code).** Same dotted-path contract;
`missing_required`/`completion_pct` switch from `required[]`+`required_state` to
`required` ∧ `applicable_when` (see §9).

## 6. The generated v2 IBV schema

`vera-backend/data/form_schemas/ibv_form_standard_v2.json` (dsl_version 2.1) — 23
sections, 209 leaf fields, 43 groups, 6 tasks (`insurance_basics`, `coverage`,
`financial`, `male_partner`, `closing_admin`, `wrap_up`), 4 shared conditions, 11
ask_groups, 29 alternatives, 1 flow rule, 2 contradictions. Highlights:

- All 9 v1 blobs exploded into discrete fields; every prompt-collected datapoint now
  has a path — including `infertility_tx_covered`, the full deductible/OOP/lifetime-max
  matrices, `rep_name`, `call_reference_number`, and existence gates
  (`tpa_exists`, `pbm_exists`, `isp_exists`, `enrollment_required`).
- Business rules encoded, not prosed: family deductible/OOP groups and spouse
  confirmation gated on `coverage_type = Family`; male-partner section on Family ∧
  spouse male; auth-department contact on `any_service_requires_prior_auth`;
  PBM/ISP/TPA details on their existence gates; per-service sub-fields on
  `covered = Yes`; `embryo_cryo_storage` on embryo-cryo coverage; effective date
  derived for calendar-year plans; out-of-network early termination as a flow rule.
- Deliberate corrections beyond the prompt (which contradicted itself): OOP and
  deductible-remaining are required; family financials are conditional; auth-department
  requiredness follows the prior-auth condition; `lifetime_cycle_max`/`cycles_used`
  moved from the closing phase into the `lifetime_maximum` section; per-service
  required sub-fields are uniform across all 8 treatments.
- v1 field keys kept wherever they were sound (`patient_name`, `spouse_partner_name`,
  `coverage_type`, …) to minimize intake-payload churn; renames only where the
  annotations/flaws demanded (`third_party` → `third_party_administrator`,
  `health_plan` → `plan_type` + `cob_status`, blob explosions, `hospital_information.
  name` → `hospital_name`).
- Use-case round (2026-07-03): deductible/OOP `met_amount`/`remaining` skipped when the
  total is `$0`/`None`/`No Deductible`/`Unlimited`/`No Limit`; PCP referral asked only
  for HMO plans; skipped infertility services auto-fill defaults via
  `inapplicable_value` (covered No, copay $0, coinsurance 0%, rest N/A) and skipped
  male-partner services record N/A; all 14 `prior_auth` fields tagged and
  `"Prior auth department"` legalized as a fill value; curated CPT codes folded in from
  `vera-frontend/scripts/transform_ibv_percpt.py` (office visits 99211, ASC 58555,
  semen analysis 89320, sperm cryo 89259 — transcribed from legacy screenshots there
  and still marked "CONFIRM these"; verify before production).
- Vera 1.0 mapping round (2026-07-03, see §13): `embryo_cryo_storage` restored to the
  full service shape v1.0 collected (covered/copay/coinsurance/prior_auth +
  storage_time_coverage; its `prior_auth` is the 15th path in
  `any_service_requires_prior_auth`); `system_fields` block added (replaces v1.0's
  primary/IVR/system mapping blocks); `default: "N/A"` applied to the 13 fields v1.0
  defaulted; `pattern` applied to tax ID and both NPIs.
- Combined-ask round (2026-07-03): `ask_groups` construct added (§4.3) and applied 7
  times — plan type + COB, group name + number, enrollment provider name + phone, auth
  department name + phone, TPA name + member ID, PBM name + phone, ISP name + phone.
- Annotation round (2026-07-04, dsl_version → 2.1): per the TL's review of the
  generated schema — `sections` became an object keyed by section_key and **every path
  is now root-anchored** (`sections.…`, including `field_answer.field_path`); per-CPT
  groups added **everywhere a code exists** (diagnostic 8, IUI 3, IVF 3, embryo cryo 2,
  embryo biopsy 2, plus single-code cpt groups for FET, both egg cryos, office visits,
  ASC ×2, semen analysis, sperm cryo; ovulation induction has no known code and stays
  service-level; `any_service_requires_prior_auth` now spans 27 paths);
  `embryo_cryo_storage` deduplicated to `storage_time_coverage` only, gated on the
  treatment's `cpt_89342.covered`; `alternatives` construct added (2 with-ask sets: ASC
  professional/facility, egg cryo elective/cancer + 27 ask-less copay/coinsurance
  pairs); `pattern` folded into `validation` + `range` added to copay/coinsurance;
  `plan_type` relaxed to text with canonical `special_values`; `cycle_limit` → text
  (holds "3 per month"); task `outro` added; `system_fields` handles renamed
  (`insurance_provider_name`, `insurance_provider_phone_number`).
- Annotation round 2 (2026-07-04): every compiled leaf now carries an **explicit
  `role`** (187 ask / 15 context / 4 input / 3 confirm) so the voice-agent collection
  filter is `role in (ask, confirm)`; `chart_number` → `role: input` (human-editable,
  never in the prompt); `callback_number` description added; outros set on the five
  mid-call tasks (played while the next LiveKit task spins up, masking switch latency).
- Contradictions round (2026-07-04): `contradictions` construct added (§4.8) with the
  two IBV rules — `small_group_self_insured_conflict` (Small Group ∧ Self Insured →
  re-clarify fund type + group size) and `mandate_requires_infertility_coverage`
  (mandate Yes ∧ infertility not covered → re-clarify coverage, cross-task).

## 7. Prompt ↔ schema parity under v2

With v2, the prompt becomes **compilable from the schema** (the ADR's stated intent):
every question, expected-answer vocabulary, skip rule, confirmation, code read-out and
task boundary in `ibv_standard_prompt.json` is either (a) derivable from the v2 schema,
or (b) explicitly assigned to a runtime layer (persona, turn-taking, IVR, gap analysis,
rejection handling, closing ritual). Nothing is left that exists only as prose in a
form field.

## 8. Annotation traceability (tech lead's jsonc comments)

| # | Comment (gist) | Resolution |
|---|---|---|
| C1/C7 | drop `constraint_library`/`constraint_ref` duplication | deleted; inline `values` (§4.4) |
| C2/C3 | chart number read-only, not in prompt | `role: input` (§4.4) |
| C4 | patient name context-only, provide if asked | `role: context` semantics (§4.4/§5) |
| C5 | rule conditions must carry full path | condition `field` = full path (§4.5) |
| C6 | remove rule `summary` | rules are self-describing; no summary key |
| C8 | male-partner section conditional (Family ∧ …) | section `applicable_when: male_partner_in_scope` |
| C9 | "verbatim from v1" descriptions | descriptions are human help text only (§4.9) |
| C10 | `group_integrity` undefined | dropped; group completeness implied (§4.4) |
| C11 | no dotted keys; real nesting | path grammar (§4.2), groups (§4.4) |
| C12 | rename `third_party` | `third_party_administrator` |
| C13–C15 | explode TPA/PBM/ISP blobs with existence gates | done, with `applicable_when` |
| C16 | missing rep name / reference number fields | `insurance_representative.rep_name` / `.call_reference_number` |
| C17 | provider section = context; prompt enriched from intake `field_answer` | `role: context` + prompt-compiler contract (§5) |
| C18/C19 | no-op marker for UI-only sections; bogus `intro_prose` | `role: ui_only`; `prompt_role` deleted |
| C20/C21 | remove `source` refs | deleted (§4.9) |
| C22 | schema = form truth only; rejection handling out | §4.9 |
| C23 | IVR out; dynamic navigator/playbooks | §4.6/§4.9 |
| C24 | derive order from schema; drop `field_list_order` | document order (§4.1) |

## 9. Migration plan (follow-up work, in order)

1. **Validator + authoring DSL + compiler** — ✅ DONE (2026-07-04):
   `vera_core.forms.dsl` (pydantic contract: models = grammar, document validator =
   §4.10, `compile_document`/`load_document`), `vera_core.forms.authoring` (macros:
   service items, per-CPT groups, money triplets), `vera_core.forms.catalog`
   (`ibv_standard.py` author source + registry), `scripts/compile_schemas.py`
   (+ `just compile-schemas`). The committed artifact is lockfile-style generated
   output: `tests/unit/forms/test_schema_dsl.py` asserts freshness (compile ==
   committed bytes) and round-trip (`load → compile` is the identity) under
   `just check`, so hand-edits of the compiled JSON now fail CI. A new insurance type
   = one catalog module + one registry entry.
2. **Backend readers**: update `forms/intake.py`, `forms/review.py`,
   `api/v1/patient_forms.py` (`missing_required`, `completion_pct`) from
   `required[]`/`required_state` to `fields` + `required`/`applicable_when` evaluation;
   version-gate on `dsl_version` so v1 documents keep working until cut-over.
3. **Seed cut-over**: point `data/form_schemas/manifest.json` at the v2 file (the
   seeder will publish a new `schema_version`; existing forms stay pinned to their
   version via `patient_form.schema_version_id`). Renamed paths mean new intake
   payload keys — coordinate the Apps Script (`docs/ibv-sheet-upload-setup.md`).
4. **Frontend**: serve the schema from the backend (or regenerate the bundled copy from
   the v2 file), replace the matrix-detection heuristic with `ui.layout`, implement
   `applicable_when` visibility, and render by role.
5. **Prompt compiler + task builder + extractor**: implement §5 against v2; retire the
   hardcoded `agent_worker/prompt.py` prompt and the unconsumed
   `composite_json` blob format.

## 10. Open questions

- **`verification_information.verified_at`** is typed `date`; if it should carry a
  time, add a `datetime` type when the first real need appears (YAGNI for now).
- **Spouse confirmation scope**: currently confirmed during `insurance_basics` for all
  Family policies (per the stated requirement). If product wants it only when the
  male-partner task will run, move `confirm_in_task` to `male_partner`.
- **Placeholder rejection** for `rep_name`/`call_reference_number` ("None", "N/A", …)
  is treated as runtime extraction policy, not schema; promote to a
  `validation.reject_placeholders` key only if other forms need to vary it.
- **Curated CPT codes** (office visits 99211, ASC 58555, semen analysis 89320, sperm
  cryo 89259) came from legacy screenshots via the frontend transform and are flagged
  "CONFIRM these" — verify with the clinic before production.

## 11. Use-case coverage matrix

How each stated use case maps to a DSL construct (schema = the generated IBV v2 file):

| Use case | Construct | In IBV v2 file |
|---|---|---|
| Family → confirm spouse name/DOB; Individual → skip | `role: confirm` + `applicable_when: family_coverage` + `confirm_in_task` | yes |
| Spouse male (+ Family) → male-partner section, else skip whole section | section `applicable_when: male_partner_in_scope` | yes |
| No service needs prior auth → skip auth dept name/phone | `applicable_when: {ref: any_service_requires_prior_auth}` (27-path `any`, CPT-level) | yes |
| Both doctor & facility out-of-network → ask OON questions | field `applicable_when: all[...]` | yes |
| Plan can't cover → collect rep name + reference, end call | `flow_rules`: `terminate_call` + `skip_to_task: wrap_up` | yes |
| Deductible/OOP total is $0/None/No Deductible/Unlimited/No Limit → skip met | total `special_values` + met/remaining `applicable_when: not_in [...]` | yes |
| Infertility not covered → skip all services AND fill defaults | group `applicable_when` + per-field `inapplicable_value` | yes |
| Service covered → ask copay/coinsurance/PA/cycle/notes, else skip | sub-field `applicable_when: covered = Yes` | yes |
| Male partner coverage No → skip related questions (record N/A) | group `applicable_when` + `inapplicable_value: "N/A"` | yes |
| Calendar Year → auto-set Jan 1, don't ask | `derive: {when, value: "01/01/{{current_year}}"}` | yes |
| Plan Year → no auto-fill; ask effective/renewal date | `derive.when` false path + `renewal_date` `applicable_when: Plan Year` | yes |
| Non-HMO → skip PCP question / HMO → ask it | `applicable_when: plan_type = HMO` | yes |
| Same benefit for remaining services → copy & stop asking | runtime blanket-answer propagation over homogeneous sibling groups (§5) | runtime rule |
| Separate PA department → stop PA questions for rest of call | runtime tool targeting `tags: ["prior_auth"]`, fill `"Prior auth department"` (§4.4) | tags in place |
| Enrollment required → ask details | `applicable_when: enrollment_required = Yes` | yes |
| TPA/PBM/ISP exists → ask name/phone | `applicable_when` on the `*_exists` gates | yes |
| Embryo cryo covered → ask storage time (CPT 89342) | `embryo_cryo_storage` section `applicable_when` + `codes` | yes |
| Lifetime max No Limit/Unlimited → skip met | `applicable_when: not_in ["No Limit", "Unlimited"]` | yes |
| Field types: Question / Context / No-Op | roles `ask`+`confirm` / `context` / `readonly`+`input` (+ section `ui_only`) | yes |
| Group integrity all/any for completion validation | group `integrity: "all" \| "any"` (default all) | construct available |

The two "runtime rule" rows are deliberately not static schema conditions: they trigger
on what the representative *says*, not on a recorded field value, so the schema's job
is to make their targets declarative (homogeneous groups, `tags`, legal fill values) —
which it now does.

## 12. Runtime alignment (voice-pipeline diagram)

Mapping of the DSL onto the intended runtime (global prompt → sequential active
sections → observer agent → agent state):

- **Global prompt** (goal, persona, restrictions, guardrails) — prompt-pipeline
  template; deliberately *not* in the schema (§4.9).
- **Per-section prompt + question list** (`prompt_version`) — compiled from each
  section: `prompt.intro`, field `prompt.ask`/`confirm`/`hints`, enum vocabularies,
  `codes`, and conditions rendered as skip instructions (§5). The conversation unit is
  flexible: run one LiveKit agentTask per **task** (grouped sections, as in the IBV
  file) or per **section** — the contracts are identical either way, since a task is an
  ordered list of sections.
- **`field_path` key list for the active section** — flatten the section's applicable
  `ask`/`confirm` leaves. Paths are root-anchored exactly as in the diagram
  (`sections.insurance_information.plan_type`) — since dsl_version 2.1 this prefixed
  form IS the `field_answer.field_path` value (decided 2026-07-04; no production rows
  predate it).
- **Observer agent (realtime form filling)** — the extractor contract (§5): watch the
  transcript, write `field_answer` rows (`source=ai_call`) for the active section's
  paths, apply `inapplicable_value`/`derive`/blanket/suppression rules.
- **Agent state** — the current answer map. Evaluating `applicable_when`,
  `required.when`, task applicability and `flow_rules` against this state after every
  answer *is* the "modify state to drive dynamic question asking" loop in the diagram.
- **Old-style constructs in the diagram** (`confirm_only` + `confirm_value:
  "{member_id}"`) are superseded by `role: confirm`, whose confirm value is the field's
  own pre-call answer — no placeholder namespace.

## 13. Vera 1.0 schema mapping

Verification against the original Vera 1.0 schema
(`smart-caller/scripts/form_types/ibv_form_standard.json` — JSON Schema draft 2020-12,
UI rendering + extraction only). **Every v1.0 leaf maps to a v2 path and every v1.0
construct has a v2 counterpart**; v2 is a strict superset (it adds the gate fields,
per-treatment cycle limits/notes, roles, tasks and flow rules v1.0 lacked).

### Construct mapping

| Vera 1.0 construct | DSL v2 counterpart |
|---|---|
| `form_metadata.name/description/version` | top-level `name`/`description`; versioning via `schema_version` rows |
| `form_metadata.instructions` (global persona baked into schema) | prompt-pipeline template — deliberately out of the schema (§4.9) |
| `form_metadata.skip_sections` | section `role: context` / `ui_only` |
| JSON Schema `$defs.serviceItem` + `allOf` composition | `group` with explicit children (compiled inline; the builder may keep `$defs`-style reuse on its side) |
| field `instructions: [...]` (ask text + behavior mixed) | `prompt.ask` + `prompt.hints`, split |
| `metadata.required: true` | `required: true` |
| `metadata.required: {"/covered": "Yes"}` (relative-pointer conditional) | `applicable_when`/`required.when` with absolute dotted paths — and v2 also *skips asking*, which v1.0 could not express |
| `metadata.readonly: true` (patient_name, spouse_gender, verified_by) | `role: context` (voice-equivalent; use `role: readonly` if UI must also lock the field) |
| `metadata.skip: true` (appointment_information) | section `role: context` |
| `metadata.patient_identity: true` (policy_number) | `role: confirm` |
| `default: "N/A"` (13 fields) | `default` (added this round, applied to the same 13) |
| `format: date` / `type: number` / `metadata.format: phone_number` | `type: date/currency/integer/phone` |
| `pattern` (tax ID, NPIs, phone) | `pattern` (added this round; phones covered by `type: phone`) |
| `const` `cpt_code`/`icd_10_code` pseudo-fields ("do not ask; provide when asked") | `codes` on sections/groups with provide-if-asked semantics |
| `primary_field_mapping` / `ivr_field_mapping` / `system_mappings` | top-level `system_fields` (one map; per-key IVR instructions were dead placeholders — IVR prose lives in the playbooks) |
| `ui_schema` (empty in v1.0) | inline `ui` hints |

### Notable field moves/renames (v1.0 path → v2 path)

`hospital_information.name/address` → `hospital_name`/`hospital_address` ·
`insurance_information.health_plan` → `plan_type` · `coordination_of_benefits` →
`cob_status` · `home_plan` → `policy_situs` · `benefit_coverage.plan_year_information`
→ `renewal_date` · `tele_health` → `telehealth_covered` · `coverages.general_coverage.
office_visit` → `general_coverage.office_visits` · `coverages.male_partner.*` →
`male_partner_coverage.*` · `coverages.infertility_treatment.*` →
`infertility_treatment.*` (same 8 service keys; child `auth_required` → `prior_auth`) ·
`financial_information.deductible.<metric>.<scope>` → `deductibles.<scope>.<metric>`
(axes transposed; same 12 leaves, ditto `out_of_pocket`) · `plan_coverage.ltm.ltm` →
`lifetime_maximum.total` · `enrollment_information` split → `enrollment` +
`authorization_department` · `pharmacy_information.*` → `pharmacy_benefit_manager.
{pbm_name,pbm_phone}` · `third_party_information.{lifetime_cycle_max,lifetime_cycle_
used}` → `lifetime_maximum.{lifetime_cycle_max,cycles_used}` · `third_party_
information.{member_services_pca,employer}` → `insurance_reference_information.*` ·
`insurance_representative.insurance_rep_name` → `rep_name` ·
`web_portal_ref_number` → `insurance_reference_information.web_portal_reference_number`
· `provider_reference_information.location` → `office_location`.

### Deliberate divergences

1. **Per-CPT granularity retained** (decided 2026-07-04, superseding an earlier
   consolidation): like v1.0, v2 collects covered/copay/coinsurance/prior_auth **per
   CPT code** wherever a code is known — `cpt_58340`-style groups under diagnostic
   testing, per-code groups inside treatments (IUI = 58323/58322/89261, IVF =
   58970/89280/89253, …) and single-code groups for the remaining services; ovulation
   induction (no known code) stays service-level. Conversation still flows per
   service/panel (`ask_groups` + group openers + blanket-answer propagation fan one
   answer out to the code rows); treatment-level `cycle_limit`/`additional_notes` sit
   beside the code groups, as v1.0 had them. One dedup versus v1.0: embryo cryo
   storage's serviceItem is not duplicated — CPT 89342 coverage lives once, under
   `embryo_cryopreservation.cpt_89342`, and the `embryo_cryo_storage` section keeps
   only `storage_time_coverage`, gated on it. (Since 2.1, all v2 paths in this section
   carry the `sections.` root prefix.)
2. v1.0's `cpt_code`/`icd_10_code` leaf fields (const-valued or
   "provide-from-patient-information", never asked) map to `codes` metadata, not to
   answer fields.
3. v1.0 duplicated `call_reference_number` in two sections; v2 keeps one
   (`insurance_representative.call_reference_number`).
4. v1.0's `skip_sections` includes `insurance_reference_information`; v2 upgrades it to
   a `collect` section with all-optional asks (the closing phase does ask these).
5. Exactly five v2 fields have no v1.0 counterpart, all deliberate additions: the
   coverage gates `infertility_tx_covered`, `male_partner_covered`, `pbm_exists`,
   `isp_exists`, and `tpa_name` (split out of v1.0's fused `third_party_administrator`
   yes/no+name field, which maps to `tpa_exists`).
