# Task Prompts DSL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved spec `docs/superpowers/specs/2026-07-06-task-prompts-dsl-design.md` — `{{system_field}}` placeholder validation on task text, a document-level `stt_key_terms` STT vocabulary, and the IBV catalog's new `introduction` task + `patient_verification` section + `patient_not_on_plan` flow rule + closing-flow content.

**Architecture:** Two small additions to the pydantic DSL validator in `dsl.py` (placeholder resolution, key-term rules), then pure content authoring in `catalog/ibv_standard.py`, then a recompile of the generated JSON artifact. No `dsl_version` bump; no frontend or intake/review changes (they never consume `tasks` or `stt_key_terms`). Runtime consumption (LiveKit AgentTask builder, `deepgram.STTv2(keyterms=...)`) is a later branch step — this plan delivers the schema side only.

**Tech Stack:** Python 3.12, pydantic v2, pytest, `just` (uv-backed), ruff + mypy --strict.

## Global Constraints

- **Never hand-edit `vera-backend/data/form_schemas/*.json`** — they are generated; run `just compile-schemas` after catalog changes. The freshness test fails CI on drift.
- `dsl_version` stays `"2.1"` everywhere.
- All backend commands run from `vera-backend/` (`cd /Users/tapusd/.supacode/repos/Vera2.0/feat/schema-to-prompt-generation/vera-backend`).
- Git commits: do NOT add any `Co-Authored-By` trailer.
- Placeholder namespace for task text = `system_fields` keys only. STT key-term cap = 100 (Deepgram keyterm-prompting limit).
- Code style: this repo runs `ruff` (with formatting via `just fmt`) and `mypy --strict`; PEP 695 typing.
- The final gate before claiming done: `just check` (lint + typecheck + test) green, then the code-simplifier pass (repo CLAUDE.md rule), then `just check` again.

---

### Task 1: Placeholder validation for task intro/outro/prompt

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/dsl.py` (constant near line 27; `Task` class at line 264; validator tasks-loop at lines 429–445)
- Test: `vera-backend/tests/unit/forms/test_schema_dsl.py` (add to `TestDocumentValidation`)

**Interfaces:**
- Consumes: existing `FormSchemaDoc._validate_document`, `minimal_doc()` test helper.
- Produces: module constant `PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")` (Task 2 reuses the `{{` convention; Task 3's catalog content must satisfy this rule). Validation error text: `task <key>.<attr>: unknown placeholder {{<token>}} (not a system_fields key)`.

- [ ] **Step 1: Write the failing tests**

Add to `TestDocumentValidation` in `vera-backend/tests/unit/forms/test_schema_dsl.py`:

```python
    def test_unknown_task_placeholder_rejected(self) -> None:
        doc = minimal_doc()
        doc["tasks"][0]["intro"] = "Calling about {{patient_name}}."
        with pytest.raises(ValidationError, match="unknown placeholder"):
            FormSchemaDoc.model_validate(doc)

    def test_known_task_placeholder_accepted(self) -> None:
        doc = minimal_doc(system_fields={"plan_type": "sections.basics.plan_type"})
        doc["tasks"][0]["prompt"] = "Mention {{plan_type}} when asked."
        FormSchemaDoc.model_validate(doc)

    def test_unclosed_braces_are_not_placeholders(self) -> None:
        doc = minimal_doc()
        doc["tasks"][0]["intro"] = "This {{ is not a placeholder."
        FormSchemaDoc.model_validate(doc)
```

- [ ] **Step 2: Run tests to verify the first fails**

Run (from `vera-backend/`):
`uv run pytest tests/unit/forms/test_schema_dsl.py::TestDocumentValidation -k placeholder -v`
Expected: `test_unknown_task_placeholder_rejected` FAILS (`DID NOT RAISE`); the other two PASS (they assert current permissive behavior stays).

- [ ] **Step 3: Implement the validator rule**

In `vera-backend/packages/vera_core/src/vera_core/forms/dsl.py`:

(a) Below `KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")` (line 27) add:

```python
# {{token}} placeholders in task-level text; token must be a system_fields key.
PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")
```

(b) Replace the `Task` class (currently lines 264–273, including the two-line comment above `prompt`) with:

```python
class Task(_Model):
    """One LiveKit AgentTask.

    ``intro``/``outro`` are spoken verbatim on task entry/exit (TTS-safe text);
    ``prompt`` is supplied directly as the agent's task instructions. All three
    may embed ``{{system_field_key}}`` placeholders, hydrated per patient form
    at task creation and validated against ``system_fields`` below. ``sections``
    may be empty for ritual tasks that collect nothing.
    """

    task_key: str
    title: str
    intro: str | None = None
    outro: str | None = None
    prompt: str | None = None
    sections: list[str]
    applicable_when: Condition | None = None
```

(c) In `_validate_document`, inside the existing `for task in self.tasks:` loop (lines 429–445), after the `for skey in task.sections:` block, add:

```python
            for attr in ("intro", "outro", "prompt"):
                text: str | None = getattr(task, attr)
                for token in PLACEHOLDER_RE.findall(text or ""):
                    if token not in (self.system_fields or {}):
                        errors.append(
                            f"task {task.task_key}.{attr}: unknown placeholder "
                            f"{{{{{token}}}}} (not a system_fields key)"
                        )
```

- [ ] **Step 4: Run the DSL test file**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py -v`
Expected: ALL PASS (both compiled schemas contain no task placeholders today, so freshness/round-trip stay green).

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/dsl.py tests/unit/forms/test_schema_dsl.py
git commit -m "feat(forms): validate {{placeholder}} tokens in task text against system_fields"
```

---

### Task 2: `stt_key_terms` document field + validation

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/dsl.py` (constant near line 29; `FormSchemaDoc` fields at lines 299–309; validator after the `# system fields` block at lines 486–490)
- Test: `vera-backend/tests/unit/forms/test_schema_dsl.py` (add to `TestDocumentValidation`)

**Interfaces:**
- Consumes: `minimal_doc()`; `_validate_document` `errors` list.
- Produces: `FormSchemaDoc.stt_key_terms: list[str] | None` (Task 3 authors it; the future session builder reads it into `deepgram.STTv2(keyterms=...)`). Constant `MAX_STT_KEY_TERMS = 100`. Error texts: `stt_key_terms: N terms exceeds limit of 100`, `stt_key_terms[i]: empty or untrimmed term ...`, `stt_key_terms[i]: placeholders are not allowed in key terms`, `stt_key_terms[i]: duplicate term ...`.

- [ ] **Step 1: Write the failing tests**

Add to `TestDocumentValidation`:

```python
    def test_stt_key_terms_valid_list_accepted(self) -> None:
        FormSchemaDoc.model_validate(minimal_doc(stt_key_terms=["coinsurance", "IVF"]))

    def test_stt_key_terms_duplicate_rejected(self) -> None:
        doc = minimal_doc(stt_key_terms=["IVF", "ivf"])
        with pytest.raises(ValidationError, match="duplicate term"):
            FormSchemaDoc.model_validate(doc)

    def test_stt_key_terms_empty_or_untrimmed_rejected(self) -> None:
        for bad in ["", " coinsurance", "coinsurance "]:
            with pytest.raises(ValidationError, match="empty or untrimmed"):
                FormSchemaDoc.model_validate(minimal_doc(stt_key_terms=[bad]))

    def test_stt_key_terms_placeholder_rejected(self) -> None:
        doc = minimal_doc(stt_key_terms=["{{patient_name}}"])
        with pytest.raises(ValidationError, match="placeholders are not allowed"):
            FormSchemaDoc.model_validate(doc)

    def test_stt_key_terms_cap_enforced(self) -> None:
        doc = minimal_doc(stt_key_terms=[f"term {i}" for i in range(101)])
        with pytest.raises(ValidationError, match="exceeds limit"):
            FormSchemaDoc.model_validate(doc)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py::TestDocumentValidation -k stt_key_terms -v`
Expected: ALL 5 FAIL — the first with `ValidationError` mentioning `extra_forbidden` (`_Model` is `extra="forbid"`, the field doesn't exist yet), the rest with `DID NOT RAISE` or `extra_forbidden`.

- [ ] **Step 3: Implement field + rules**

In `dsl.py`:

(a) Below `MAX_PATH_LENGTH = 255` (line 29) add:

```python
MAX_STT_KEY_TERMS = 100  # Deepgram keyterm-prompting limit
```

(b) In `FormSchemaDoc`, insert directly after `system_fields: dict[str, str] | None = None`:

```python
    # Session-wide STT vocabulary, fed verbatim to deepgram.STTv2(keyterms=...)
    # at voice-session build; applies to every task. Static domain terms only.
    stt_key_terms: list[str] | None = None
```

(c) In `_validate_document`, after the `# system fields` block (the `for handle, path in (self.system_fields or {}).items():` loop) and before `# flow rules`, add:

```python
        # stt key terms: bounded, unique, static vocabulary
        terms = self.stt_key_terms or []
        if len(terms) > MAX_STT_KEY_TERMS:
            errors.append(
                f"stt_key_terms: {len(terms)} terms exceeds limit of {MAX_STT_KEY_TERMS}"
            )
        seen_terms: set[str] = set()
        for i, term in enumerate(terms):
            where = f"stt_key_terms[{i}]"
            if not term or term != term.strip():
                errors.append(f"{where}: empty or untrimmed term {term!r}")
                continue
            if "{{" in term:
                errors.append(f"{where}: placeholders are not allowed in key terms")
            lowered = term.lower()
            if lowered in seen_terms:
                errors.append(f"{where}: duplicate term {term!r}")
            seen_terms.add(lowered)
```

- [ ] **Step 4: Run the DSL test file**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py -v`
Expected: ALL PASS. (Freshness stays green: `compile_document` uses `exclude_none=True`, so the absent `stt_key_terms` adds nothing to either committed artifact.)

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/dsl.py tests/unit/forms/test_schema_dsl.py
git commit -m "feat(forms): document-level stt_key_terms with bounded/unique/static validation"
```

---

### Task 3: IBV catalog — introduction task, patient_verification, flow rule, closing flow, key terms

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py` (section builders; `build_ibv_standard()` at line 958: sections dict, `system_fields`? — no change there; `tasks=[...]` at line 1032; `flow_rules=[...]` at line 1122)
- Regenerate: `vera-backend/data/form_schemas/ibv_form_standard_v2.json` (via `just compile-schemas` — never by hand)
- Test: `vera-backend/tests/unit/forms/test_schema_dsl.py` (add to `TestCompiledArtifacts`)

**Interfaces:**
- Consumes: `Section`, `Task`, `FlowRule`, `enum_ask`, `eq`, `YES_NO` (all already imported in `ibv_standard.py`); Task 1's placeholder validation (every `{{token}}` below is an existing `system_fields` key); Task 2's `stt_key_terms` field.
- Produces: field path `sections.patient_verification.patient_on_plan` (enum Yes/No); task key `introduction` (first task); flow-rule key `patient_not_on_plan`; populated `doc.stt_key_terms` (54 terms).

- [ ] **Step 1: Write the failing content test**

Add to `TestCompiledArtifacts` in `tests/unit/forms/test_schema_dsl.py`:

```python
    def test_ibv_call_opening_and_key_terms(self) -> None:
        doc = SCHEMAS["infertility_treatment"][1]()
        intro_task = doc.tasks[0]
        assert intro_task.task_key == "introduction"
        assert intro_task.sections == ["patient_verification"]
        assert "{{patient_name}}" in (intro_task.intro or "")
        assert "{{member_id}}" in (intro_task.prompt or "")
        assert intro_task.outro == "Great, let me pull up my questions..."
        rule_keys = [r.rule_key for r in doc.flow_rules or []]
        assert rule_keys[0] == "patient_not_on_plan"
        wrap_up = doc.tasks[-1]
        assert wrap_up.task_key == "wrap_up"
        assert wrap_up.intro is not None and wrap_up.outro is not None
        assert doc.stt_key_terms is not None
        assert "intrauterine insemination" in doc.stt_key_terms
        assert len(doc.stt_key_terms) <= 100
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py::TestCompiledArtifacts::test_ibv_call_opening_and_key_terms -v`
Expected: FAIL — `assert intro_task.task_key == "introduction"` (first task is currently `insurance_basics`).

- [ ] **Step 3: Add the `_patient_verification` section builder**

In `ibv_standard.py`, directly above `def build_ibv_standard()` (line ~958), add:

```python
def _patient_verification() -> Section:
    return Section(
        title="Patient Verification",
        description=(
            "Outcome of the call-opening membership check. Recorded during the "
            "introduction task; a denial terminates the call via the "
            "patient_not_on_plan flow rule."
        ),
        fields={
            "patient_on_plan": enum_ask(
                "Patient On Plan",
                "Can you confirm the patient is on this plan?",
                YES_NO,
            ),
        },
    )
```

Then in `build_ibv_standard()` insert it as the first **collect** section — between the context sections and insurance_information:

```python
    sections: dict[str, Section] = {
        **_context_sections(),
        "patient_verification": _patient_verification(),
        "insurance_information": _insurance_information(),
```

- [ ] **Step 4: Add the `introduction` task**

In the `tasks=[` list (line ~1032), insert BEFORE the `insurance_basics` Task:

```python
            Task(
                task_key="introduction",
                title="Introduction & Patient Verification",
                intro=(
                    "Hello, I'm VERA, an AI Virtual Assistant... calling from "
                    "{{hospital_name}}, on behalf of Dr. {{doctor_name}}. Before we "
                    "begin... I'd like to let you know that this call is being "
                    "recorded for quality and training purposes. Also, please note "
                    "that... this call is supervised by my human manager, "
                    "{{verified_by}}, who may intervene if necessary. I'm looking "
                    "at the details for... {{patient_name}}, date of birth "
                    "{{patient_dob}}. Could you let me know if this matches the "
                    "name on the plan?"
                ),
                prompt=(
                    "Deliver the introduction exactly once, calmly; if interrupted, "
                    "continue from where you left off — never restart it. Wait for "
                    "the representative to confirm they can see the patient AND "
                    "introduce themselves. 'Let me check', 'hold on', 'one moment', "
                    "'give me a second' and similar are NOT confirmations — say "
                    "'Take your time' once, then stay silent until they return. A "
                    "bare 'yes' without the representative introducing themselves "
                    "is NOT a confirmation — keep waiting. If the representative "
                    "cannot find the patient, provide the member ID {{member_id}} "
                    "and the insurance provider {{insurance_provider_name}}. If the "
                    "representative asks questions to verify the call is "
                    "legitimate, answer from these details: patient "
                    "{{patient_name}}, date of birth {{patient_dob}}, member ID "
                    "{{member_id}}, facility {{hospital_name}} at "
                    "{{hospital_address}}, facility NPI {{hospital_npi}}, tax ID "
                    "{{hospital_tax_id}}, ordering provider Dr. {{doctor_name}} "
                    "with NPI {{doctor_npi}}, callback number {{callback_number}}. "
                    "Record Patient On Plan as 'No' ONLY after those details have "
                    "been provided and the representative still denies the patient "
                    "is on the plan — then wrap up politely. After this task, never "
                    "re-introduce yourself for the rest of the call."
                ),
                outro="Great, let me pull up my questions...",
                sections=["patient_verification"],
            ),
```

- [ ] **Step 5: Update `closing_admin` outro and `wrap_up` intro/prompt/outro**

In the `closing_admin` Task, replace
`outro="Perfect, I have all the administrative details I need. One second please.",`
with:

```python
                outro=(
                    "Perfect, I have all the administrative details I need. Let me "
                    "take a quick moment to review my notes and make sure I haven't "
                    "missed anything. One moment please."
                ),
```

Replace the whole `wrap_up` Task with:

```python
            Task(
                task_key="wrap_up",
                title="Wrap Up",
                intro=(
                    "Thanks so much for your patience — that covers everything on "
                    "my list."
                ),
                prompt=(
                    "Always run last, even on early termination: capture the "
                    "representative's name and a call reference number before "
                    "ending the call politely. Both are critical fields and must be "
                    "actual values — never accept 'None', 'Unknown', 'Not provided' "
                    "or any placeholder; politely re-ask until the representative "
                    "provides them. Ask for them only once every remaining question "
                    "has been resolved."
                ),
                outro=(
                    "That's everything I need today. Thank you so much for all "
                    "your help — have a wonderful day!"
                ),
                sections=["insurance_representative"],
            ),
```

- [ ] **Step 6: Add the `patient_not_on_plan` flow rule**

In `flow_rules=[` (line ~1122), insert BEFORE the `no_out_of_network_coverage` rule (call order — it fires earliest):

```python
            FlowRule(
                rule_key="patient_not_on_plan",
                when=eq("sections.patient_verification.patient_on_plan", "No"),
                action="terminate_call",
                skip_to_task="wrap_up",
                note=(
                    "The representative denied the patient is on the plan even "
                    "after the member ID, insurance provider and verification "
                    "details were provided. Skip all remaining tasks, collect the "
                    "representative name and call reference number, then end the "
                    "call."
                ),
            ),
```

- [ ] **Step 7: Add `stt_key_terms` to the document**

In the `FormSchemaDoc(...)` call, insert directly after the `system_fields={...},` argument:

```python
        stt_key_terms=[
            # treatments
            "intrauterine insemination",
            "IUI",
            "in vitro fertilization",
            "IVF",
            "ovulation induction",
            "egg cryopreservation",
            "embryo cryopreservation",
            "frozen embryo transfer",
            "embryo biopsy",
            "semen analysis",
            "sperm cryopreservation",
            "infertility",
            # plan / benefits
            "coinsurance",
            "copay",
            "deductible",
            "out-of-pocket maximum",
            "lifetime maximum",
            "prior authorization",
            "coordination of benefits",
            "policy situs",
            "PPO",
            "HMO",
            "EPO",
            "POS",
            "self insured",
            "fully funded",
            "benefit year",
            "plan year",
            "telehealth",
            "PCP referral",
            "infertility plan mandate",
            "cycle limit",
            # admin
            "pharmacy benefit manager",
            "third party administrator",
            "specialty pharmacy",
            "member ID",
            "group number",
            "NPI",
            "tax ID",
            # common answers (prune first if live tuning shows over-recognition)
            "covered",
            "not covered",
            "in network",
            "out of network",
            "individual",
            "family",
            "spouse",
            "dependent",
            "primary",
            "secondary",
            "tertiary",
            "small group",
            "large group",
            "no limit",
            "unlimited",
        ],
```

- [ ] **Step 8: Run the content test, then recompile the artifact**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py::TestCompiledArtifacts::test_ibv_call_opening_and_key_terms -v`
Expected: PASS (the build itself also proves every placeholder resolves — Task 1's validator runs on construction).

Then regenerate the JSON:
`just compile-schemas`
Then verify the diff touches only the IBV artifact and only the expected keys:
`git diff --stat data/form_schemas/` (expect only `ibv_form_standard_v2.json`) and spot-check `git diff data/form_schemas/ibv_form_standard_v2.json | head -80` shows `stt_key_terms`, `patient_verification`, `introduction`.

- [ ] **Step 9: Run the whole DSL test file**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py -v`
Expected: ALL PASS (freshness + round-trip green against the regenerated artifact; `disease_only` artifact untouched).

- [ ] **Step 10: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py data/form_schemas/ibv_form_standard_v2.json tests/unit/forms/test_schema_dsl.py
git commit -m "feat(forms): IBV introduction task, patient_not_on_plan flow rule, closing flow, stt_key_terms"
```

---

### Task 4: Amend the v2 DSL design spec (grammar + contracts)

**Files:**
- Modify: `docs/superpowers/specs/2026-07-02-form-schema-dsl-v2-design.md` (repo root; §4.6 tasks, §4.10 validation list, §5 task-builder/prompt-compiler contracts)

**Interfaces:**
- Consumes: the approved delta spec `docs/superpowers/specs/2026-07-06-task-prompts-dsl-design.md`.
- Produces: the base spec stays the single source of truth for the grammar; future schema authors read it, not the delta.

- [ ] **Step 1: Update §4.6 (tasks)**

Locate the §4.6 code block and rules paragraph. Extend the JSONC example with the three text keys and a placeholder note — replace the example's first line block:

```jsonc
{ "task_key": "financial", "title": "Financial Details",
  "intro": "Now let me ask about some financial details.",   // spoken verbatim on task entry
  "outro": "…",   // spoken verbatim on exit; also masks next-task spin-up latency
  "prompt": "…",  // supplied directly as the agent's task instructions
  "sections": ["deductibles", "out_of_pocket", "lifetime_maximum", "embryo_cryo_storage"],
  "applicable_when": Condition }                              // optional
```

Then replace the paragraph beginning `The schema deliberately defines **only the form-collection tasks**.` (ends `(annotation C23).`) with:

```markdown
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
```

- [ ] **Step 2: Add `stt_key_terms` to the document grammar**

In the §4 document-shape listing (the JSONC block that contains `"tasks": [ Task, ... ],`), add directly after the `system_fields` line:

```jsonc
  "stt_key_terms": ["intrauterine insemination", ...],  // optional; session-wide STT vocabulary
```

- [ ] **Step 3: Extend the §4.10 validation-rule list**

Append two bullets to the validation rules list:

```markdown
- Every `{{token}}` in a task's `intro`/`outro`/`prompt` must be a defined
  `system_fields` key.
- `stt_key_terms`: ≤ 100 terms, each non-empty and trimmed, no case-insensitive
  duplicates, no `{{placeholders}}` (static vocabulary — never hydrated).
```

- [ ] **Step 4: Update the §5 task-builder contract**

In the `**Task builder (call initiation).**` paragraph, append:

```markdown
The builder hydrates task-text placeholders from `system_fields` → intake answers
(field `default` when unanswered) and passes `stt_key_terms` to the STT component
(`deepgram.STTv2(keyterms=...)`) once per session.
```

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-07-02-form-schema-dsl-v2-design.md
git commit -m "docs(forms): fold task-prompt placeholders, ritual tasks and stt_key_terms into the v2 DSL spec"
```

---

### Task 5: Full gate + code-simplifier pass

**Files:**
- No new files; touches whatever the simplifier refines in Tasks 1–3's files.

**Interfaces:**
- Consumes: everything above.
- Produces: a green `just check` and the repo-mandated simplifier pass — the definition of "done" for Vera 2.0.

- [ ] **Step 1: Format + full gate**

Run (from `vera-backend/`): `just fmt && just check`
Expected: ruff clean, mypy --strict clean, pytest all green. If `just fmt` reflows the catalog/test edits, re-run `just compile-schemas` IF ruff changed any authored string content (it won't reflow string literals' content, only code layout — artifact should be unchanged; verify with `git status data/form_schemas/`).

- [ ] **Step 2: Run the code-simplifier agent (repo CLAUDE.md rule — mandatory)**

Trigger the `code-simplifier` agent from `code-simplifier@claude-plugins-official` on the recently modified files:
`packages/vera_core/src/vera_core/forms/dsl.py`, `packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py`, `tests/unit/forms/test_schema_dsl.py`.
Behavior-preserving refinements only.

- [ ] **Step 3: Re-run the gate after refinements**

Run: `just check`; if the simplifier touched `ibv_standard.py`, also `just compile-schemas` then confirm `git status data/form_schemas/` is clean (content-identical) or re-stage the artifact.
Expected: all green.

- [ ] **Step 4: Commit any simplifier refinements**

```bash
git add -A packages/vera_core tests data/form_schemas
git commit -m "refactor(forms): simplifier pass over task-prompt DSL changes"
```

(Skip the commit if the simplifier changed nothing.)

---

## Self-Review Notes

- **Spec coverage:** §3/3.1 (contract + placeholder validation) → Task 1; §3.2 hydration is runtime-contract-only (non-goal); §4 + §4.1 (introduction task, patient_verification, flow rule) → Task 3 steps 3–6; §5 closing flow (closing_admin outro, wrap_up intro/prompt/outro, ordering note) → Task 3 step 5 (ordering itself is runtime, documented in Task 4); §6 stt_key_terms → Tasks 2 & 3 step 7; §7.3 base-spec amendment → Task 4; §7.5 gates + simplifier → Task 5. Seeding (`just seed-schemas`) is a deploy/local-DB step outside this plan's gate — the freshness test is the CI contract.
- **Type consistency:** `PLACEHOLDER_RE` (Task 1) used only in `dsl.py`; `stt_key_terms: list[str] | None` matches Task 3's list-literal kwarg and the tests' `minimal_doc(stt_key_terms=[...])` override path (top-level `doc.update`).
- **Order sensitivity:** Task 3 must land after Tasks 1–2 (it authors placeholders and `stt_key_terms` that need the model/validator). Tasks 4–5 are independent of each other but come last.
