# Prompt Compiler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `docs/superpowers/specs/2026-07-08-prompt-compiler-design.md` — runtime per-task prompt rendering from the schema DSL, the `PromptDocument` (literal session block + task overrides) stored in `prompt_version.composite_json`, the `ConfirmInTask` object grammar, the widened placeholder namespace, and the seeder/API rework.

**Architecture:** Pure rendering lives in `vera_core.forms` (`prompting.py` for models/orchestration, new `prompt_text.py` for condition-to-text): `render_task_prompts(doc, prompt_doc) -> RenderedPrompts` walks the schema models directly via the existing `leaf_gates` traversal (the dumped-JSON IR from `compile_prompt_document` loses the `Condition` model objects the text renderer needs, so that function is deleted once the seeder stops using it — this is the one deliberate deviation from the spec's "reuse as private IR" phrasing; the *reused* pieces are the `leaf_gates` walk and section/task grouping). The seeder bootstraps a factory v1 `PromptDocument` per schema and carries published documents forward across schema republishes. The prompts API validates drafts as typed `PromptDocument`s and gains a `preview` endpoint returning the rendered result.

**Tech Stack:** Python 3.12, pydantic v2, SQLAlchemy async, FastAPI, pytest, `just` (uv), ruff + mypy --strict.

## Global Constraints

- **Never hand-edit `vera-backend/data/form_schemas/*.json`** — run `just compile-schemas` after catalog changes; the freshness test gates drift.
- All backend commands run from `/Users/tapusd/.supacode/repos/Vera2.0/feat/schema-to-prompt-generation/vera-backend`.
- Git commits: NO `Co-Authored-By` trailer.
- `dsl_version` stays `"2.1"`.
- Rendering must be fully deterministic: no clocks, no randomness, no dict-iteration nondeterminism (all dicts here preserve insertion order).
- Control-plane API rules: `ResponseModel[T]` via `ok(...)`; errors via `CustomAPIException` subclasses (`NotFoundError`/`ConflictError`/`BadRequestError`), never `HTTPException`; declare `response_model` + `responses=CustomAPIResponse.custom(...)`; permissions `platform:prompts:read`/`platform:prompts:write` (already seeded); session via `platform_scoped_session`.
- PEP 695 typing; `asyncio` only; `pytest` async tests follow each file's existing patterns.
- Integration tests need local Postgres (`just up && just migrate`). **If they error with `ForeignKeyViolationError` on fixture cleanup, the dev DB has stale pre-existing rows — STOP and ask the user to reset it (`docker compose down -v && just up && just migrate && just seed`); do not delete DB rows yourself.**
- Final gate: `just check` green (unit scope at minimum; integration if DB available), then the code-simplifier pass, then `just check` again (repo CLAUDE.md rule).

---

### Task 1: `ConfirmInTask` object grammar + anchor validator rule

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/dsl.py` (condition classes ~line 44–80; `Leaf.confirm_in_task` line 168; validator lines ~377–470)
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py` (imports; spouse fields ~lines 186, 202)
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/prompting.py` (line 112, one-line type fix)
- Regenerate: `vera-backend/data/form_schemas/ibv_form_standard_v2.json` (via `just compile-schemas`)
- Test: `vera-backend/tests/unit/forms/test_schema_dsl.py`

**Interfaces:**
- Consumes: existing `_Model`, `Condition` classes, `COLLECTED_ROLES`, `minimal_doc()` test helper.
- Produces: `class ConfirmInTask(_Model)` with `task_key: str`, `confirm_immediate: bool = False`; `Leaf.confirm_in_task: ConfirmInTask | None`; module function `condition_field_paths(cond: Condition, shared: dict[str, Condition] | None, depth: int = 0) -> Iterator[str]` (Task 5 reuses it); validator error text `…: confirm_immediate=true needs an anchor — the gate chain must reference a collectable leaf inside task '<key>'`.

- [ ] **Step 1: Write the failing tests**

Add to `TestDocumentValidation` in `vera-backend/tests/unit/forms/test_schema_dsl.py` (module level: it already imports `pytest`, `ValidationError`, `FormSchemaDoc`, `minimal_doc`):

```python
    @staticmethod
    def _context_confirm(cit: object) -> dict[str, Any]:
        """minimal_doc + a context section holding one confirm_in_task field."""
        doc = minimal_doc()
        doc["sections"]["ctx"] = {
            "title": "Ctx",
            "role": "context",
            "fields": {
                "spouse": {
                    "type": "text",
                    "title": "Spouse",
                    "role": "confirm",
                    "applicable_when": {
                        "field": "sections.basics.plan_type",
                        "op": "eq",
                        "value": "PPO",
                    },
                    "confirm_in_task": cit,
                    "prompt": {"confirm": "Spouse is {{value}} — correct?"},
                }
            },
        }
        return doc

    def test_confirm_in_task_object_form_required(self) -> None:
        with pytest.raises(ValidationError):
            FormSchemaDoc.model_validate(self._context_confirm("main"))
        FormSchemaDoc.model_validate(
            self._context_confirm({"task_key": "main", "confirm_immediate": True})
        )

    def test_confirm_immediate_requires_in_task_anchor(self) -> None:
        doc = self._context_confirm({"task_key": "main", "confirm_immediate": True})
        del doc["sections"]["ctx"]["fields"]["spouse"]["applicable_when"]
        with pytest.raises(ValidationError, match="needs an anchor"):
            FormSchemaDoc.model_validate(doc)

    def test_confirm_at_task_end_needs_no_anchor(self) -> None:
        doc = self._context_confirm({"task_key": "main", "confirm_immediate": False})
        del doc["sections"]["ctx"]["fields"]["spouse"]["applicable_when"]
        FormSchemaDoc.model_validate(doc)

    def test_confirm_in_task_unknown_task_rejected(self) -> None:
        doc = self._context_confirm({"task_key": "ghost", "confirm_immediate": False})
        with pytest.raises(ValidationError, match="unknown task"):
            FormSchemaDoc.model_validate(doc)
```

(`Any` is already imported in the file.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py::TestDocumentValidation -k confirm -v`
Expected: `test_confirm_in_task_object_form_required` FAILS (object form currently rejected — the field is `str | None`); anchor tests fail similarly.

- [ ] **Step 3: Implement the grammar in `dsl.py`**

(a) Directly after the condition classes (after `NotCondition`/`RefCondition`, before the leaf/field models) add:

```python
def condition_field_paths(
    cond: Condition, shared: dict[str, Condition] | None, depth: int = 0
) -> Iterator[str]:
    """Every leaf path a condition references, shared refs expanded."""
    if depth > 10:
        return
    match cond:
        case Comparison(field=field):
            yield field
        case RefCondition(ref=ref):
            if shared and ref in shared:
                yield from condition_field_paths(shared[ref], shared, depth + 1)
        case AllCondition(all=subs) | AnyCondition(any=subs):
            for sub in subs:
                yield from condition_field_paths(sub, shared, depth + 1)
        case NotCondition(not_=sub):
            yield from condition_field_paths(sub, shared, depth + 1)


class ConfirmInTask(_Model):
    """Where and when a context-section confirm field is spoken (2026-07-08 spec §3.4)."""

    task_key: str = Field(description="The task during which this confirmation is spoken.")
    confirm_immediate: bool = Field(
        default=False,
        description=(
            "True: speak the confirmation immediately after the anchor question — "
            "the last collectable leaf in the named task referenced by this "
            "field's applicable_when gate chain — is answered and the gate holds. "
            "False: speak it at the end of the named task."
        ),
    )
```

(b) Change the `Leaf` field (line ~168): `confirm_in_task: ConfirmInTask | None = None` (the role guard at ~196 is a presence check — unchanged).

(c) In `_validate_document`, change `walk_fields` to carry the gate **chain** instead of the boolean (the boolean is derivable). Replace the current signature/body pieces:

```python
        immediate_confirms: list[tuple[str, ConfirmInTask, tuple[Condition, ...]]] = []

        def walk_fields(
            prefix: str,
            fields: dict[str, FormField],
            section: Section,
            chain: tuple[Condition, ...],
        ) -> None:
            for key, field in fields.items():
                path = f"{prefix}.{key}"
                check_key(path, key)
                if len(path) > MAX_PATH_LENGTH:
                    errors.append(f"{path}: exceeds {MAX_PATH_LENGTH} chars")
                field_chain = (
                    (*chain, field.applicable_when)
                    if field.applicable_when is not None
                    else chain
                )
                if field.applicable_when is not None:
                    check_condition(f"{path}.applicable_when", field.applicable_when)
                if isinstance(field, Group):
                    walk_fields(path, field.fields, section, field_chain)
                    continue
```

…keep the existing leaf rules, replacing `gated`/`field_gated` reads with `field_chain` truthiness (`if field.inapplicable_value is not None and not field_chain:`), change the task-reference check to `field.confirm_in_task.task_key not in task_keys`, and add after it:

```python
                if field.confirm_in_task is not None and field.confirm_in_task.confirm_immediate:
                    immediate_confirms.append((path, field.confirm_in_task, field_chain))
```

Update the call site:

```python
            walk_fields(
                f"{PATH_PREFIX}{section_key}",
                section.fields,
                section,
                (section.applicable_when,) if section.applicable_when is not None else (),
            )
```

(d) After the tasks block (after the `collect section … not assigned to any task` loop), add the anchor rule. Note `required.when` is deliberately NOT part of the chain — the anchor comes from `applicable_when` gates only:

```python
        # confirm_immediate needs a determinable anchor inside its task
        task_sections = {t.task_key: set(t.sections) for t in self.tasks}
        for path, cit, chain in immediate_confirms:
            in_task = task_sections.get(cit.task_key, set())
            refs = {ref for cond in chain for ref in condition_field_paths(cond, shared)}
            if not any(
                ref in leaves
                and leaves[ref].role in COLLECTED_ROLES
                and ref.split(".")[1] in in_task
                for ref in refs
            ):
                errors.append(
                    f"{path}: confirm_immediate=true needs an anchor — the gate chain "
                    f"must reference a collectable leaf inside task {cit.task_key!r}"
                )
```

- [ ] **Step 4: Update the catalog and `prompting.py`**

In `ibv_standard.py`: add `ConfirmInTask` to the `from vera_core.forms.dsl import (…)` list; change both spouse fields from `confirm_in_task="insurance_basics",` to:

```python
                    confirm_in_task=ConfirmInTask(
                        task_key="insurance_basics", confirm_immediate=True
                    ),
```

In `prompting.py` line ~112, change the routing key (behavioral split comes in Task 5; this keeps types correct):

```python
            confirms_by_task.setdefault(leaf.confirm_in_task.task_key, []).append(
```

- [ ] **Step 5: Recompile and run the forms test files**

Run: `just compile-schemas` then `git diff --stat data/form_schemas/` (expect only `ibv_form_standard_v2.json`; its diff shows the object form on the two spouse fields).
Run: `uv run pytest tests/unit/forms/ -v`
Expected: ALL PASS (freshness, round-trip, prompting composite, new grammar tests).

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/dsl.py packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py packages/vera_core/src/vera_core/forms/prompting.py data/form_schemas/ibv_form_standard_v2.json tests/unit/forms/test_schema_dsl.py
git commit -m "feat(forms): confirm_in_task object grammar with confirm_immediate + anchor rule"
```

---

### Task 2: Widen the placeholder namespace to context-leaf paths

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/dsl.py` (`PLACEHOLDER_RE` ~line 29; task-text placeholder loop in `_validate_document`)
- Test: `vera-backend/tests/unit/forms/test_schema_dsl.py`

**Interfaces:**
- Consumes: Task 1's file state.
- Produces: `PLACEHOLDER_RE = re.compile(r"\{\{([\w.]+)\}\}")` (Task 3's `validate_prompt_document` reuses it); task-text error text `task <key>.<attr>: unknown placeholder {{<token>}} (not a system_fields key or context-leaf path)`.

- [ ] **Step 1: Write the failing tests**

Add to `TestDocumentValidation`:

```python
    def test_context_leaf_path_placeholder_accepted(self) -> None:
        doc = minimal_doc()
        doc["sections"]["basics"]["fields"]["bg"] = {
            "type": "text",
            "title": "Background",
            "role": "context",
        }
        doc["tasks"][0]["intro"] = "About {{sections.basics.bg}}."
        FormSchemaDoc.model_validate(doc)

    def test_non_context_leaf_path_placeholder_rejected(self) -> None:
        doc = minimal_doc()
        doc["tasks"][0]["intro"] = "About {{sections.basics.plan_type}}."
        with pytest.raises(ValidationError, match="unknown placeholder"):
            FormSchemaDoc.model_validate(doc)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py -k "path_placeholder" -v`
Expected: `test_context_leaf_path_placeholder_accepted` FAILS (dotted token not matched by `\w+`, so it currently *passes validation silently* — assert carefully: with `\w+` the dotted token is NOT matched, so no error is raised and the test PASSES vacuously. Therefore the discriminating test is `test_non_context_leaf_path_placeholder_rejected`, which FAILS (`DID NOT RAISE`) until the regex widens.)

- [ ] **Step 3: Implement**

In `dsl.py`: change the constant to

```python
# {{token}} placeholders in task-level text; token = a system_fields key or the
# root-anchored path of a context-role leaf (2026-07-08 spec §4).
PLACEHOLDER_RE = re.compile(r"\{\{([\w.]+)\}\}")
```

In `_validate_document`, before the tasks loop compute once:

```python
        context_paths = {p for p, leaf in leaves.items() if leaf.role == "context"}
```

and change the placeholder check body to:

```python
                for token in PLACEHOLDER_RE.findall(text or ""):
                    if token not in (self.system_fields or {}) and token not in context_paths:
                        errors.append(
                            f"task {task.task_key}.{attr}: unknown placeholder "
                            f"{{{{{token}}}}} (not a system_fields key or context-leaf path)"
                        )
```

- [ ] **Step 4: Run the DSL tests**

Run: `uv run pytest tests/unit/forms/test_schema_dsl.py -v`
Expected: ALL PASS (compiled artifacts contain only system-handle tokens, so freshness is unaffected).

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/dsl.py tests/unit/forms/test_schema_dsl.py
git commit -m "feat(forms): placeholder namespace widened to context-leaf paths"
```

---

### Task 3: `PromptDocument` models, `FACTORY_SESSION`, `validate_prompt_document`

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/prompting.py` (add models near the top, after imports)
- Test: Create `vera-backend/tests/unit/forms/test_prompt_document.py`

**Interfaces:**
- Consumes: `PLACEHOLDER_RE`, `FormSchemaDoc` from `vera_core.forms.dsl`.
- Produces (all in `vera_core.forms.prompting`):
  - `class SessionBlock(BaseModel)` — `persona: str`, `goal: str`, `base_instructions: str` (all `min_length=1`, `Field(description=…)`)
  - `class TaskTextOverride(BaseModel)` — `intro/outro/prompt: str | None = None`
  - `class PromptDocument(BaseModel)` — `kind: Literal["prompt_document"]`, `session: SessionBlock`, `task_overrides: dict[str, TaskTextOverride]`
  - `FACTORY_SESSION: SessionBlock`
  - `def validate_prompt_document(doc: PromptDocument, schema_doc: FormSchemaDoc) -> list[str]`
  - `class RenderedTaskPrompt(BaseModel)` — `task_key, title: str; intro, outro: str | None; prompt: str`
  - `class RenderedPrompts(BaseModel)` — `name, insurance_type, dsl_version, persona, goal, base_instructions: str; tasks: list[RenderedTaskPrompt]`

- [ ] **Step 1: Write the failing tests**

Create `vera-backend/tests/unit/forms/test_prompt_document.py`:

```python
"""PromptDocument shape + content validation against a pinned schema."""

from typing import Any

import pytest
from pydantic import ValidationError

from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.prompting import (
    FACTORY_SESSION,
    PromptDocument,
    SessionBlock,
    validate_prompt_document,
)

SESSION: dict[str, Any] = {
    "persona": "You are VERA.",
    "goal": "Verify benefits.",
    "base_instructions": "Ask one question at a time.",
}


def schema_doc() -> FormSchemaDoc:
    return FormSchemaDoc.model_validate(
        {
            "dsl_version": "2.1",
            "name": "Test",
            "insurance_type": "infertility_treatment",
            "system_fields": {"member_id": "sections.basics.plan_type"},
            "sections": {
                "basics": {
                    "title": "Basics",
                    "fields": {
                        "plan_type": {
                            "type": "text",
                            "title": "Plan Type",
                            "role": "ask",
                            "required": True,
                            "prompt": {"ask": "What type of plan is this?"},
                        },
                        "bg": {"type": "text", "title": "Background", "role": "context"},
                    },
                }
            },
            "tasks": [{"task_key": "main", "title": "Main", "sections": ["basics"]}],
        }
    )


def prompt_doc(**overrides: Any) -> PromptDocument:
    data: dict[str, Any] = {"kind": "prompt_document", "session": SESSION, "task_overrides": {}}
    data.update(overrides)
    return PromptDocument.model_validate(data)


class TestShape:
    def test_valid_document(self) -> None:
        doc = prompt_doc(task_overrides={"main": {"prompt": "Do it politely."}})
        assert doc.task_overrides["main"].prompt == "Do it politely."

    def test_extra_keys_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PromptDocument.model_validate(
                {"kind": "prompt_document", "session": SESSION, "bogus": 1}
            )

    def test_empty_session_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PromptDocument.model_validate(
                {"kind": "prompt_document", "session": {**SESSION, "persona": ""}}
            )

    def test_factory_session_is_complete_and_placeholder_free(self) -> None:
        assert isinstance(FACTORY_SESSION, SessionBlock)
        for text in (
            FACTORY_SESSION.persona,
            FACTORY_SESSION.goal,
            FACTORY_SESSION.base_instructions,
        ):
            assert text and "{{" not in text


class TestContentValidation:
    def test_clean_document_has_no_errors(self) -> None:
        doc = prompt_doc(
            task_overrides={
                "main": {"intro": "About {{member_id}} and {{sections.basics.bg}}."}
            }
        )
        assert validate_prompt_document(doc, schema_doc()) == []

    def test_unknown_task_key(self) -> None:
        doc = prompt_doc(task_overrides={"ghost": {"prompt": "x"}})
        assert any("unknown task_key" in e for e in validate_prompt_document(doc, schema_doc()))

    def test_empty_override_entry(self) -> None:
        doc = prompt_doc(task_overrides={"main": {}})
        assert any("empty override" in e for e in validate_prompt_document(doc, schema_doc()))

    def test_bad_placeholder_in_session(self) -> None:
        doc = prompt_doc(session={**SESSION, "persona": "I serve {{patietn_name}}."})
        assert any("unknown placeholder" in e for e in validate_prompt_document(doc, schema_doc()))

    def test_value_token_exempt(self) -> None:
        doc = prompt_doc(task_overrides={"main": {"prompt": "Confirm {{value}} politely."}})
        assert validate_prompt_document(doc, schema_doc()) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/forms/test_prompt_document.py -v`
Expected: FAIL at import (`ImportError: cannot import name 'PromptDocument'`).

- [ ] **Step 3: Implement in `prompting.py`**

Add after the imports (extend imports with `from typing import Literal`, `from pydantic import BaseModel, ConfigDict, Field`, and add `PLACEHOLDER_RE` to the `vera_core.forms.dsl` import):

```python
class _Doc(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionBlock(_Doc):
    """Session-wide agent text applicable to every task. LITERAL content — consumed
    as-is; nothing underneath is overridden (2026-07-08 spec §4)."""

    persona: str = Field(
        min_length=1,
        description=(
            "Who the agent is: name (VERA), voice/temperament ('calm, professional, "
            "patient'), speech pacing habits, how it refers to itself, pronunciation "
            "tendencies. Vera 1.0's AGENT_PERSONA maps here."
        ),
    )
    goal: str = Field(
        min_length=1,
        description=(
            "What the call is for — e.g. 'verify infertility benefits for a patient "
            "with the payer's representative, completing every applicable question "
            "accurately' — the north star the LLM falls back on when the "
            "conversation drifts."
        ),
    )
    base_instructions: str = Field(
        min_length=1,
        description=(
            "Global behavior rules applied across every task: turn-taking "
            "discipline, value-recording rules ('record exactly what the rep "
            "says', 'never invent an answer'), background-noise/hold handling, "
            "role enforcement ('you ask the questions, don't answer benefits "
            "questions yourself'), anti-repetition, never re-introducing yourself. "
            "Vera 1.0's conversation/value-recording rule blocks map here."
        ),
    )


class TaskTextOverride(_Doc):
    """Sparse patch over one task's schema-authored text; set fields win."""

    intro: str | None = None
    outro: str | None = None
    prompt: str | None = None


class PromptDocument(_Doc):
    """prompt_version.composite_json — literal session block + task text patches."""

    kind: Literal["prompt_document"]
    session: SessionBlock
    task_overrides: dict[str, TaskTextOverride] = Field(default_factory=dict)


# Creation-time content for a schema's very first prompt_version (2026-07-08 spec
# §6.1). Placeholder-free so it is valid for every schema. After bootstrap the DB
# is authoritative — editing these constants never retrofits an existing schema.
FACTORY_SESSION = SessionBlock(
    persona=(
        "You are VERA, an AI virtual assistant calling on behalf of a medical "
        "practice's insurance verification team. You are calm, professional and "
        "patient. You speak clearly at a measured pace, slow down for medical "
        "terms and numbers, and never rush the representative. You refer to "
        "yourself as VERA."
    ),
    goal=(
        "Verify the patient's insurance benefits with the payer's representative, "
        "completing every applicable question on the verification form accurately "
        "and recording each answer exactly as stated."
    ),
    base_instructions=(
        "Ask one question at a time and wait for the answer before moving on. "
        "Record exactly what the representative says — never invent, assume or "
        "round an answer. If an answer is partial or ambiguous, read it back and "
        "ask for confirmation. If the representative asks you to hold, say 'take "
        "your time' once and stay silent until they return. You are the caller "
        "asking the questions: do not answer benefits questions yourself and do "
        "not volunteer information you were not asked for. Do not repeat a "
        "question that has already been answered. Never re-introduce yourself "
        "mid-call. If the representative cannot provide an answer after checking, "
        "note that and move on rather than pressing."
    ),
)


class RenderedTaskPrompt(_Doc):
    task_key: str
    title: str
    intro: str | None = None  # AgentTask entry speech — verbatim
    outro: str | None = None  # AgentTask exit speech — verbatim
    prompt: str  # compiled instruction text


class RenderedPrompts(_Doc):
    name: str
    insurance_type: str
    dsl_version: str
    persona: str  # literal from the session block
    goal: str
    base_instructions: str
    tasks: list[RenderedTaskPrompt]


def validate_prompt_document(doc: PromptDocument, schema_doc: FormSchemaDoc) -> list[str]:
    """Content errors of a prompt document against its pinned schema (spec §4).

    Shape errors are pydantic's job; this checks the parts that need the schema:
    task keys exist, overrides are non-empty, placeholders resolve. The exact
    token `value` is exempt (field-level confirm namespace)."""
    errors: list[str] = []
    valid_tokens = (
        set(schema_doc.system_fields or {})
        | {path for path, leaf in schema_doc.leaf_items() if leaf.role == "context"}
        | {"value"}
    )
    task_keys = {t.task_key for t in schema_doc.tasks}
    texts: list[tuple[str, str | None]] = [
        ("session.persona", doc.session.persona),
        ("session.goal", doc.session.goal),
        ("session.base_instructions", doc.session.base_instructions),
    ]
    for key, override in doc.task_overrides.items():
        if key not in task_keys:
            errors.append(f"task_overrides.{key}: unknown task_key")
        if override.intro is None and override.outro is None and override.prompt is None:
            errors.append(f"task_overrides.{key}: empty override entry")
        texts.extend(
            (f"task_overrides.{key}.{attr}", getattr(override, attr))
            for attr in ("intro", "outro", "prompt")
        )
    for where, text in texts:
        for token in PLACEHOLDER_RE.findall(text or ""):
            if token not in valid_tokens:
                errors.append(f"{where}: unknown placeholder {{{{{token}}}}}")
    return errors
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/forms/test_prompt_document.py tests/unit/forms/test_prompting.py -v`
Expected: ALL PASS (existing composite tests untouched).

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/prompting.py tests/unit/forms/test_prompt_document.py
git commit -m "feat(forms): PromptDocument models, factory session, content validation"
```

---

### Task 4: Condition-to-text renderer (`prompt_text.py`)

**Files:**
- Create: `vera-backend/packages/vera_core/src/vera_core/forms/prompt_text.py`
- Test: Create `vera-backend/tests/unit/forms/test_prompt_text.py`

**Interfaces:**
- Consumes: `dsl` condition classes, `FormSchemaDoc.leaf_items()`.
- Produces: `def build_condition_renderer(doc: FormSchemaDoc) -> Callable[[Condition], str]` — deterministic English for any condition; duplicate leaf titles disambiguated with the path in parens; shared refs expanded.

- [ ] **Step 1: Write the failing tests**

Create `vera-backend/tests/unit/forms/test_prompt_text.py`:

```python
"""Deterministic condition → English rendering."""

from typing import Any

from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.prompt_text import build_condition_renderer


def doc_with(conditions: dict[str, Any] | None = None) -> FormSchemaDoc:
    return FormSchemaDoc.model_validate(
        {
            "dsl_version": "2.1",
            "name": "T",
            "insurance_type": "infertility_treatment",
            "shared_conditions": conditions or {},
            "sections": {
                "a": {
                    "title": "A",
                    "fields": {
                        "x": {
                            "type": "text",
                            "title": "Plan Type",
                            "role": "ask",
                            "prompt": {"ask": "x?"},
                        },
                        "dup": {
                            "type": "text",
                            "title": "Copay",
                            "role": "ask",
                            "prompt": {"ask": "d?"},
                        },
                    },
                },
                "b": {
                    "title": "B",
                    "fields": {
                        "dup": {
                            "type": "text",
                            "title": "Copay",
                            "role": "ask",
                            "prompt": {"ask": "d?"},
                        }
                    },
                },
            },
            "tasks": [{"task_key": "main", "title": "Main", "sections": ["a", "b"]}],
        }
    )


def test_comparison_ops() -> None:
    render = build_condition_renderer(doc_with())
    eq = {"field": "sections.a.x", "op": "eq", "value": "PPO"}
    assert render(_cond(eq)) == '"Plan Type" is "PPO"'
    assert render(_cond({**eq, "op": "ne"})) == '"Plan Type" is not "PPO"'
    assert (
        render(_cond({"field": "sections.a.x", "op": "in", "value": ["PPO", "HMO"]}))
        == '"Plan Type" is one of "PPO", "HMO"'
    )
    assert (
        render(_cond({"field": "sections.a.x", "op": "not_in", "value": ["N/A"]}))
        == '"Plan Type" is none of "N/A"'
    )


def test_duplicate_titles_get_path_disambiguation() -> None:
    render = build_condition_renderer(doc_with())
    text = render(_cond({"field": "sections.b.dup", "op": "eq", "value": "1"}))
    assert text == '"Copay" (sections.b.dup) is "1"'


def test_nesting_and_ref_expansion() -> None:
    shared = {"fam": {"field": "sections.a.x", "op": "eq", "value": "Family"}}
    render = build_condition_renderer(doc_with(shared))
    cond = _cond(
        {
            "all": [
                {"ref": "fam"},
                {
                    "any": [
                        {"field": "sections.a.x", "op": "eq", "value": "PPO"},
                        {"not": {"field": "sections.a.x", "op": "eq", "value": "HMO"}},
                    ]
                },
            ]
        }
    )
    assert render(cond) == (
        '"Plan Type" is "Family" and ("Plan Type" is "PPO" or not ("Plan Type" is "HMO"))'
    )


def _cond(data: dict[str, Any]) -> Any:
    from pydantic import TypeAdapter

    from vera_core.forms.dsl import Condition

    return TypeAdapter(Condition).validate_python(data)
```

Note: if `Condition` cannot be validated via `TypeAdapter` because it is an `Annotated` union alias, use `TypeAdapter(Condition)` exactly as shown — pydantic supports adapters over annotated unions. If the alias name differs, check `dsl.py` for the union's exported name.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/forms/test_prompt_text.py -v`
Expected: FAIL at import (`No module named 'vera_core.forms.prompt_text'`).

- [ ] **Step 3: Implement `prompt_text.py`**

```python
"""Deterministic condition → English rendering for compiled task prompts.

One function renders every Condition so wording is uniform across question
gates, task applicability, flow rules and contradictions (2026-07-08 spec §3.2).
"""

from collections import Counter
from collections.abc import Callable

from vera_core.forms.dsl import (
    AllCondition,
    AnyCondition,
    Comparison,
    Condition,
    FormSchemaDoc,
    NotCondition,
    RefCondition,
)


def build_condition_renderer(doc: FormSchemaDoc) -> Callable[[Condition], str]:
    """A renderer bound to one document (title lookup + shared-ref expansion)."""
    leaves = dict(doc.leaf_items())
    title_counts = Counter(leaf.title for leaf in leaves.values())
    shared = doc.shared_conditions or {}

    def label(path: str) -> str:
        leaf = leaves.get(path)
        if leaf is None:
            return path
        if title_counts[leaf.title] > 1:
            return f'"{leaf.title}" ({path})'
        return f'"{leaf.title}"'

    def wrap(sub: Condition) -> str:
        text = render(sub)
        return f"({text})" if isinstance(sub, (AllCondition, AnyCondition)) else text

    def render(cond: Condition) -> str:
        match cond:
            case Comparison(field=field, op="eq", value=value):
                return f'{label(field)} is "{value}"'
            case Comparison(field=field, op="ne", value=value):
                return f'{label(field)} is not "{value}"'
            case Comparison(field=field, op="in", value=value):
                options = ", ".join(f'"{v}"' for v in value)
                return f"{label(field)} is one of {options}"
            case Comparison(field=field, op="not_in", value=value):
                options = ", ".join(f'"{v}"' for v in value)
                return f"{label(field)} is none of {options}"
            case RefCondition(ref=ref):
                return render(shared[ref]) if ref in shared else ref
            case AllCondition(all=subs):
                return " and ".join(wrap(sub) for sub in subs)
            case AnyCondition(any=subs):
                return " or ".join(wrap(sub) for sub in subs)
            case NotCondition(not_=sub):
                return f"not ({render(sub)})"
        return ""  # unreachable: the match is exhaustive over Condition

    return render
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/forms/test_prompt_text.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/prompt_text.py tests/unit/forms/test_prompt_text.py
git commit -m "feat(forms): condition-to-text renderer"
```

---

### Task 5: `render_task_prompts` — per-task prompt text

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/prompting.py` (add renderer below the Task-3 models; keep `compile_prompt_document` intact — Task 6 deletes it)
- Rewrite: `vera-backend/tests/unit/forms/test_prompting.py`
- Create: `vera-backend/tests/unit/forms/snapshots/ibv_introduction.prompt.txt` and `ibv_insurance_basics.prompt.txt` (generated, then reviewed + committed)

**Interfaces:**
- Consumes: Task 1's `condition_field_paths`, `ConfirmInTask`; Task 3's models; Task 4's `build_condition_renderer`; `leaf_gates` from `vera_core.forms.conditions`.
- Produces: `def render_task_prompts(doc: FormSchemaDoc, prompt_doc: PromptDocument | None = None) -> RenderedPrompts` — Task 6 (seeder note) and Task 7 (preview endpoint) call it.

- [ ] **Step 1: Rewrite `test_prompting.py` (failing first)**

Replace the file's entire contents with:

```python
"""render_task_prompts: schema document (+ prompt document) → per-task prompt text."""

import logging
import os
from pathlib import Path

from vera_core.forms.dsl import FormSchemaDoc, load_document
from vera_core.forms.prompting import (
    FACTORY_SESSION,
    PromptDocument,
    RenderedPrompts,
    SessionBlock,
    TaskTextOverride,
    render_task_prompts,
)

FORM_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "data" / "form_schemas"
SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"

IBV: FormSchemaDoc = load_document(
    (FORM_SCHEMA_DIR / "ibv_form_standard_v2.json").read_text(encoding="utf-8")
)
RENDERED: RenderedPrompts = render_task_prompts(IBV)


def task(key: str):  # noqa: ANN201 - test helper
    return next(t for t in RENDERED.tasks if t.task_key == key)


class TestSession:
    def test_factory_fallback_with_warning(self, caplog) -> None:  # noqa: ANN001
        with caplog.at_level(logging.WARNING):
            out = render_task_prompts(IBV, None)
        assert out.persona == FACTORY_SESSION.persona
        assert any("factory session" in r.message for r in caplog.records)

    def test_session_text_is_literal(self) -> None:
        doc = PromptDocument(
            kind="prompt_document",
            session=SessionBlock(persona="P.", goal="G.", base_instructions="B."),
        )
        out = render_task_prompts(IBV, doc)
        assert (out.persona, out.goal, out.base_instructions) == ("P.", "G.", "B.")
        # session text is never folded into task prompts
        assert all("P." not in t.prompt for t in out.tasks)


class TestTaskText:
    def test_task_order_and_meta(self) -> None:
        assert [t.task_key for t in RENDERED.tasks] == [t.task_key for t in IBV.tasks]
        assert RENDERED.name == "Infertility"
        assert RENDERED.dsl_version == "2.1"

    def test_intro_outro_pass_through(self) -> None:
        intro_task = task("introduction")
        assert intro_task.intro is not None and "{{patient_name}}" in intro_task.intro
        assert intro_task.outro == "Great, let me pull up my questions..."

    def test_override_merge_field_level(self) -> None:
        doc = PromptDocument(
            kind="prompt_document",
            session=FACTORY_SESSION,
            task_overrides={"introduction": TaskTextOverride(intro="Hi. {{member_id}}.")},
        )
        out = render_task_prompts(IBV, doc)
        intro_task = next(t for t in out.tasks if t.task_key == "introduction")
        assert intro_task.intro == "Hi. {{member_id}}."
        # outro not overridden → schema default survives
        assert intro_task.outro == "Great, let me pull up my questions..."

    def test_unknown_override_key_ignored(self) -> None:
        doc = PromptDocument(
            kind="prompt_document",
            session=FACTORY_SESSION,
            task_overrides={"ghost": TaskTextOverride(prompt="x")},
        )
        assert render_task_prompts(IBV, doc).tasks  # no raise

    def test_questions_render_with_vocab_and_gates(self) -> None:
        basics = task("insurance_basics").prompt
        assert "Is the doctor inside the insurance network?" in basics
        assert "Answers: Yes | No" in basics
        assert "Ask only if" in basics
        assert '"Doctor Inside Network" is "No"' in basics

    def test_immediate_confirm_attaches_to_anchor(self) -> None:
        basics = task("insurance_basics").prompt
        assert "Immediately after this answer" in basics
        assert "spouse listed" in basics  # spouse name confirm text
        assert "Before finishing this task" not in basics or "spouse" not in basics.split(
            "Before finishing this task"
        )[-1]

    def test_flow_rules_attach_to_firing_task(self) -> None:
        assert "TERMINATION RULE — patient_not_on_plan" in task("introduction").prompt
        assert "TERMINATION RULE — no_out_of_network_coverage" in task("insurance_basics").prompt
        assert "TERMINATION RULE" not in task("coverage").prompt

    def test_contradictions_attach_to_last_field_task(self) -> None:
        assert "CONSISTENCY CHECK — small_group_self_insured_conflict" in task(
            "insurance_basics"
        ).prompt
        assert "CONSISTENCY CHECK — mandate_requires_infertility_coverage" in task(
            "coverage"
        ).prompt

    def test_derive_note_renders(self) -> None:
        basics = task("insurance_basics").prompt
        assert 'record "01/01/{{current_year}}" without asking' in basics

    def test_every_catalog_schema_renders(self) -> None:
        disease = load_document(
            (FORM_SCHEMA_DIR / "disease_only_verification.json").read_text(encoding="utf-8")
        )
        out = render_task_prompts(disease)
        assert out.tasks and all(t.prompt for t in out.tasks)


class TestSnapshots:
    """Golden files lock wording. To update intentionally:
    UPDATE_SNAPSHOTS=1 uv run pytest tests/unit/forms/test_prompting.py -k Snapshots
    then review the diff and commit."""

    def _check(self, name: str, text: str) -> None:
        path = SNAPSHOT_DIR / name
        if os.environ.get("UPDATE_SNAPSHOTS") == "1":
            path.parent.mkdir(exist_ok=True)
            path.write_text(text, encoding="utf-8")
        assert text == path.read_text(encoding="utf-8"), f"{name} stale — see docstring"

    def test_introduction_snapshot(self) -> None:
        self._check("ibv_introduction.prompt.txt", task("introduction").prompt)

    def test_insurance_basics_snapshot(self) -> None:
        self._check("ibv_insurance_basics.prompt.txt", task("insurance_basics").prompt)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/forms/test_prompting.py -v`
Expected: FAIL at import (`cannot import name 'render_task_prompts'`).

- [ ] **Step 3: Implement the renderer in `prompting.py`**

Add `import logging` and extend the dsl import with `ConfirmInTask, Contradiction, FlowRule, Task, condition_field_paths` (keep existing names); add `from vera_core.forms.prompt_text import build_condition_renderer`. Then append below the Task-3 models:

```python
logger = logging.getLogger(__name__)

_QuestionItem = tuple[str, Leaf, tuple[Condition, ...]]


def render_task_prompts(
    doc: FormSchemaDoc, prompt_doc: PromptDocument | None = None
) -> RenderedPrompts:
    """Session text + one compiled instruction prompt per task (spec §3).

    Deterministic: same doc + same prompt_doc = byte-identical output. `intro`/
    `outro` pass through (override ?? schema default) — they are AgentTask
    entry/exit speech, never folded into the instruction text."""
    if prompt_doc is None:
        logger.warning(
            "no prompt document for insurance_type=%s — using factory session text",
            doc.insurance_type,
        )
    session = prompt_doc.session if prompt_doc is not None else FACTORY_SESSION
    overrides = prompt_doc.task_overrides if prompt_doc is not None else {}

    render_cond = build_condition_renderer(doc)
    shared = doc.shared_conditions or {}
    leaves = dict(doc.leaf_items())
    order = {path: i for i, path in enumerate(leaves)}
    section_to_task = {s: t.task_key for t in doc.tasks for s in t.sections}
    titles = {path: field.title for path, field in doc._iter_fields()}  # noqa: SLF001

    questions: dict[str, list[_QuestionItem]] = {}
    immediate_by_anchor: dict[str, list[_QuestionItem]] = {}
    end_confirms: dict[str, list[_QuestionItem]] = {}
    for path, leaf, gates in leaf_gates(doc):
        cit = leaf.confirm_in_task
        if cit is not None:
            anchor = _anchor(cit, gates, shared, leaves, order, section_to_task)
            if cit.confirm_immediate and anchor is not None:
                immediate_by_anchor.setdefault(anchor, []).append((path, leaf, gates))
            else:
                end_confirms.setdefault(cit.task_key, []).append((path, leaf, gates))
        elif leaf.role in ("ask", "confirm"):
            questions.setdefault(path.split(".")[1], []).append((path, leaf, gates))

    flow_by_task: dict[str, list[FlowRule]] = {}
    for rule in doc.flow_rules or []:
        key = _last_ref_task(rule.when, shared, order, section_to_task)
        if key is not None:
            flow_by_task.setdefault(key, []).append(rule)
    contra_by_task: dict[str, list[Contradiction]] = {}
    for contra in doc.contradictions or []:
        key = _last_ref_task(contra.when, shared, order, section_to_task)
        if key is not None:
            contra_by_task.setdefault(key, []).append(contra)

    tasks_out: list[RenderedTaskPrompt] = []
    for task in doc.tasks:
        override = overrides.get(task.task_key, TaskTextOverride())
        tasks_out.append(
            RenderedTaskPrompt(
                task_key=task.task_key,
                title=task.title,
                intro=override.intro or task.intro,
                outro=override.outro or task.outro,
                prompt=_task_text(
                    doc,
                    task,
                    override,
                    render_cond,
                    questions,
                    immediate_by_anchor,
                    end_confirms.get(task.task_key, []),
                    flow_by_task.get(task.task_key, []),
                    contra_by_task.get(task.task_key, []),
                    titles,
                    leaves,
                ),
            )
        )
    return RenderedPrompts(
        name=doc.name,
        insurance_type=doc.insurance_type,
        dsl_version=doc.dsl_version,
        persona=session.persona,
        goal=session.goal,
        base_instructions=session.base_instructions,
        tasks=tasks_out,
    )


def _anchor(
    cit: ConfirmInTask,
    gates: tuple[Condition, ...],
    shared: dict[str, Condition],
    leaves: dict[str, Leaf],
    order: dict[str, int],
    section_to_task: dict[str, str],
) -> str | None:
    """Last document-order collectable leaf in the named task that the gate chain
    references — the question the immediate confirmation attaches to. The
    validator guarantees one exists for confirm_immediate leaves; None routes the
    confirm to the end-of-task block (defense in depth)."""
    if not cit.confirm_immediate:
        return None
    best: str | None = None
    for cond in gates:
        for ref in condition_field_paths(cond, shared):
            leaf = leaves.get(ref)
            if leaf is None or leaf.role not in ("ask", "confirm"):
                continue
            if section_to_task.get(ref.split(".")[1]) != cit.task_key:
                continue
            if best is None or order[ref] > order[best]:
                best = ref
    return best


def _last_ref_task(
    cond: Condition,
    shared: dict[str, Condition],
    order: dict[str, int],
    section_to_task: dict[str, str],
) -> str | None:
    """The task where a rule can fire: task of the last-answered referenced field."""
    best: tuple[int, str] | None = None
    for ref in condition_field_paths(cond, shared):
        task_key = section_to_task.get(ref.split(".")[1])
        if task_key is None or ref not in order:
            continue
        if best is None or order[ref] > best[0]:
            best = (order[ref], task_key)
    return best[1] if best else None


def _question_lines(
    idx: int,
    path: str,
    leaf: Leaf,
    gates: tuple[Condition, ...],
    render_cond: Callable[[Condition], str],
    immediate: list[_QuestionItem],
) -> list[str]:
    text = leaf.prompt.ask if leaf.role == "ask" else leaf.prompt.confirm  # type: ignore[union-attr]
    lines = [f"{idx}. {text}"]
    if leaf.values:
        lines.append(f"   - Answers: {' | '.join(leaf.values)}")
    if leaf.special_values:
        lines.append(f"   - Also accepted: {', '.join(leaf.special_values)}")
    for hint in (leaf.prompt.hints if leaf.prompt and leaf.prompt.hints else []):
        lines.append(f"   - Hint: {hint}")
    if leaf.validation is not None and leaf.validation.date_format is not None:
        lines.append(f"   - Expected date format: {leaf.validation.date_format}")
    if gates:
        conds = " and ".join(render_cond(g) for g in gates)
        skip = (
            f' If skipped, record "{leaf.inapplicable_value}".'
            if leaf.inapplicable_value is not None
            else ""
        )
        lines.append(f"   - Ask only if {conds}.{skip}")
    if leaf.derive is not None:
        lines.append(
            f'   - When {render_cond(leaf.derive.when)}: record "{leaf.derive.value}" '
            "without asking."
        )
    if leaf.required is False:
        lines.append("   - Optional; skip gracefully if the representative has nothing.")
    elif not isinstance(leaf.required, bool):
        lines.append(f"   - Required only when {render_cond(leaf.required.when)}.")
    if leaf.codes is not None and leaf.codes.cpt:
        lines.append(f"   - CPT: {', '.join(leaf.codes.cpt)}")
    if immediate:
        lines.append("   - Immediately after this answer:")
        for cpath, cleaf, cgates in immediate:
            cond_txt = " and ".join(render_cond(g) for g in cgates)
            ctext = cleaf.prompt.confirm if cleaf.prompt else cleaf.title  # type: ignore[union-attr]
            lines.append(f"     * If {cond_txt}: confirm — {ctext}")
    return lines


def _task_text(
    doc: FormSchemaDoc,
    task: Task,
    override: TaskTextOverride,
    render_cond: Callable[[Condition], str],
    questions: dict[str, list[_QuestionItem]],
    immediate_by_anchor: dict[str, list[_QuestionItem]],
    end_confirms: list[_QuestionItem],
    flow_rules: list[FlowRule],
    contradictions: list[Contradiction],
    titles: dict[str, str],
    leaves: dict[str, Leaf],
) -> str:
    blocks: list[str] = []
    if task.applicable_when is not None:
        blocks.append(f"This task runs only when {render_cond(task.applicable_when)}.")
    instructions = override.prompt or task.prompt
    if instructions:
        blocks.append(instructions)

    n = 1
    for section_key in task.sections:
        section = doc.sections[section_key]
        lines = [f"### {section.title}"]
        if section.prompt is not None:
            lines.append(section.prompt.intro)
        if section.codes is not None and section.codes.cpt:
            speak = (
                "Read these CPT codes aloud when asking"
                if section.codes.speak_cpt
                else "Provide these codes only if the representative asks"
            )
            lines.append(f"{speak}: {', '.join(section.codes.cpt)}.")
        for path, leaf, gates in questions.get(section_key, []):
            lines.extend(
                _question_lines(
                    n, path, leaf, gates, render_cond, immediate_by_anchor.get(path, [])
                )
            )
            n += 1
        for group in section.ask_groups or []:
            members = ", ".join(titles.get(m, m) for m in group.fields)
            lines.append(f'Ask together on the first pass: "{group.ask}" (covers: {members}).')
        for alt in section.alternatives or []:
            members = ", ".join(titles.get(m, m) for m in alt.members)
            lines.append(
                f'Either/or — once one of these is answered, record "N/A" for the '
                f"rest: {members}."
            )
        blocks.append("\n".join(lines))

    if end_confirms:
        lines = ["Before finishing this task, confirm:"]
        for _path, leaf, gates in end_confirms:
            text = leaf.prompt.confirm if leaf.prompt else leaf.title  # type: ignore[union-attr]
            only = (
                f" (only if {' and '.join(render_cond(g) for g in gates)})" if gates else ""
            )
            lines.append(f"- {text}{only}")
        blocks.append("\n".join(lines))

    task_titles = {t.task_key: t.title for t in doc.tasks}
    for rule in flow_rules:
        target = (
            f' Stop the remaining questions and move to "{task_titles[rule.skip_to_task]}".'
            if rule.skip_to_task is not None
            else " End the call politely."
        )
        note = f" {rule.note}" if rule.note else ""
        blocks.append(
            f"TERMINATION RULE — {rule.rule_key}:\n"
            f"If {render_cond(rule.when)}:{note}{target}"
        )
    for contra in contradictions:
        fields = ", ".join(titles.get(p, p) for p in contra.fields)
        clarify = (
            f' Push back once, saying: "{contra.clarify}"'
            if contra.clarify
            else " Push back once and re-clarify."
        )
        blocks.append(
            f"CONSISTENCY CHECK — {contra.rule_key}:\n"
            f"If {render_cond(contra.when)}: {contra.reason}{clarify} "
            f"Then re-confirm: {fields}."
        )
    return "\n\n".join(blocks)
```

Notes for the implementer: `Callable` comes from `collections.abc`; check `dsl.Validation`'s attribute is named `date_format` (it is — `validation: {"date_format": "M/D/YYYY"}` round-trips) and that `Codes` has `cpt: list[str] | None` and `speak_cpt: bool` — adjust attribute access to the real model if names differ. The two `# type: ignore[union-attr]` are legitimate: the document validator guarantees ask/confirm leaves carry the matching prompt text.

- [ ] **Step 4: Generate snapshots, review, run tests**

Run: `mkdir -p tests/unit/forms/snapshots && UPDATE_SNAPSHOTS=1 uv run pytest tests/unit/forms/test_prompting.py -v`
Then READ both generated `.txt` files end-to-end — they are the actual agent instructions; check the introduction prompt contains the verification behavior and the insurance_basics prompt shows numbered questions, the OON gate, the immediate spouse confirms on the Coverage Type question, the termination rule, and the small-group consistency check. Then:
Run: `uv run pytest tests/unit/forms/ -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/prompting.py tests/unit/forms/test_prompting.py tests/unit/forms/snapshots/
git commit -m "feat(forms): render_task_prompts — per-task prompt text with rules folded in"
```

---

### Task 6: Seeder rework — factory bootstrap + carry-forward; delete the composite compiler

**Files:**
- Modify: `vera-backend/scripts/seed.py` (`_seed_prompts`, lines ~300–380; imports)
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/prompting.py` (delete `compile_prompt_document`, `_task_entry`, `_question`, `_dump` — last consumer gone)
- Test: `vera-backend/tests/integration/db/test_seed_prompts.py`

**Interfaces:**
- Consumes: Task 3's `PromptDocument`, `FACTORY_SESSION`, `TaskTextOverride`.
- Produces: `_seed_prompts` summary lines: `<type> '<name>' v<N> (published — factory)`, `… (published — carried forward[, dropped: k1, k2])`, `… v<N> (current)`, `<type> (skipped — no published schema)`.

- [ ] **Step 1: Update the integration tests (failing first)**

In `test_seed_prompts.py`: keep `_wipe`, `clean_prompts`, `_counts`, `_published_schema_version_id`, `test_skips_when_no_published_schema`, and `test_second_published_version_rejected` as they are. Extend `test_seed_binds_published_schema_and_is_idempotent`: after the `version.schema_version_id` assertion add:

```python
        assert version.composite_json["kind"] == "prompt_document"
        assert version.composite_json["session"]["persona"]
        assert version.composite_json["task_overrides"] == {}
```

Add a new test at the end of the file:

```python
async def test_carry_forward_on_schema_republish(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    clean_prompts: None,
) -> None:
    async with admin_sessionmaker() as session, session.begin():
        await _seed_form_schemas(session)
        await _seed_prompts(session)

    # Simulate an operator edit on the published document (tests may shortcut
    # the immutable-versions application rule) with one real and one orphaned key.
    async with admin_sessionmaker() as session, session.begin():
        version = (
            await session.execute(
                select(PromptVersion)
                .join(Prompt, PromptVersion.prompt_id == Prompt.id)
                .join(FormSchema, Prompt.schema_id == FormSchema.id)
                .where(FormSchema.insurance_type == INSURANCE_TYPE)
            )
        ).scalar_one()
        version.composite_json = {
            **version.composite_json,
            "task_overrides": {
                "wrap_up": {"outro": "Edited goodbye."},
                "ghost": {"prompt": "orphan"},
            },
        }

    # Republish the schema as v2 (same content, new version row) so the prompt
    # seed sees a schema_version_id mismatch and carries the document forward.
    async with admin_sessionmaker() as session, session.begin():
        published = (
            await session.execute(
                select(SchemaVersion)
                .join(FormSchema, SchemaVersion.schema_id == FormSchema.id)
                .where(
                    FormSchema.insurance_type == INSURANCE_TYPE,
                    SchemaVersion.status == VersionStatus.PUBLISHED,
                )
            )
        ).scalar_one()
        published.status = VersionStatus.DRAFT
        await session.flush()
        session.add(
            SchemaVersion(
                schema_id=published.schema_id,
                version=published.version + 1,
                schema_json=published.schema_json,
                status=VersionStatus.PUBLISHED,
            )
        )

    async with admin_sessionmaker() as session, session.begin():
        summary = await _seed_prompts(session)
    assert any("carried forward" in line and "ghost" in line for line in summary)

    async with admin_sessionmaker() as session:
        prompts, total, published_count = await _counts(session)
        assert (prompts, total, published_count) == (1, 2, 1)
        current = (
            await session.execute(
                select(PromptVersion)
                .join(Prompt, PromptVersion.prompt_id == Prompt.id)
                .join(FormSchema, Prompt.schema_id == FormSchema.id)
                .where(
                    FormSchema.insurance_type == INSURANCE_TYPE,
                    PromptVersion.status == VersionStatus.PUBLISHED,
                )
            )
        ).scalar_one()
        assert current.version == 2
        assert current.schema_version_id == await _published_schema_version_id(session)
        assert current.composite_json["task_overrides"] == {
            "wrap_up": {"intro": None, "outro": "Edited goodbye.", "prompt": None}
        }
```

Note: `SchemaVersion` construction must match the model's required columns — mirror how `test_seed_form_schemas.py` builds versions if a field is missing. If the dev DB is unreachable these tests skip (conftest behavior) — run what you can and say so in your report.

Also update the module docstring's first paragraph to describe the new behavior (bootstrap + carry-forward instead of "generated composite").

- [ ] **Step 2: Rework `_seed_prompts` in `scripts/seed.py`**

Replace the import of `compile_prompt_document` with `from vera_core.forms.prompting import FACTORY_SESSION, PromptDocument`. Replace the function body between the `schema_doc = …` line and the `max_version` block with:

```python
        schema_doc = FormSchemaDoc.model_validate(published_schema.schema_json)
        name = f"{schema_doc.name} Prompt"

        prompt = (
            await session.execute(
                select(Prompt).where(Prompt.schema_id == schema.id, Prompt.name == name)
            )
        ).scalar_one_or_none()
        if prompt is None:
            prompt = Prompt(schema_id=schema.id, name=name)
            session.add(prompt)
            await session.flush()

        published = (
            await session.execute(
                select(PromptVersion).where(
                    PromptVersion.prompt_id == prompt.id,
                    PromptVersion.status == VersionStatus.PUBLISHED,
                )
            )
        ).scalar_one_or_none()
        if published is not None and published.schema_version_id == published_schema.id:
            summary.append(f"{insurance_type} '{name}' v{published.version} (current)")
            continue

        if published is None:
            # Factory bootstrap (spec §6.1): code-authored session content, once.
            doc_model = PromptDocument(
                kind="prompt_document", session=FACTORY_SESSION, task_overrides={}
            )
            note = "factory"
        else:
            # Carry the operator-owned document to the new schema version, pruning
            # overrides whose task no longer exists.
            prior = PromptDocument.model_validate(published.composite_json)
            task_keys = {t.task_key for t in schema_doc.tasks}
            dropped = sorted(set(prior.task_overrides) - task_keys)
            doc_model = prior.model_copy(
                update={
                    "task_overrides": {
                        k: v for k, v in prior.task_overrides.items() if k in task_keys
                    }
                }
            )
            note = "carried forward" + (f", dropped: {', '.join(dropped)}" if dropped else "")
        doc = doc_model.model_dump(mode="json")
```

Keep the existing `max_version` / demote / insert block exactly as-is (it already writes `composite_json=doc`), and change the final summary line to:

```python
        summary.append(f"{insurance_type} '{name}' v{next_version} (published — {note})")
```

Update `_seed_prompts`'s docstring to: bootstrap-or-carry-forward semantics, no rendered text stored (rendering happens at call time via `render_task_prompts`).

- [ ] **Step 3: Delete the composite compiler from `prompting.py`**

Delete `compile_prompt_document`, `_task_entry`, `_question`, and `_dump` (the renderer replaced them; `grep -rn compile_prompt_document` must return nothing). Update the module docstring to describe the runtime-rendering contract (spec §1) instead of seed-time compilation.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/forms/ -v` (expect ALL PASS)
Run: `uv run pytest tests/integration/db/test_seed_prompts.py tests/integration/db/test_seed_form_schemas.py -v` (expect ALL PASS with a reachable DB; skips without — say which happened. See Global Constraints if fixtures error on stale rows.)

- [ ] **Step 5: Commit**

```bash
git add scripts/seed.py packages/vera_core/src/vera_core/forms/prompting.py tests/integration/db/test_seed_prompts.py
git commit -m "feat(forms): seed prompts as factory/carried-forward PromptDocuments; drop composite compiler"
```

---

### Task 7: Prompts API — typed draft validation + preview endpoint

**Files:**
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/prompts.py`
- Test: `vera-backend/tests/integration/control_plane/test_prompts.py`

**Interfaces:**
- Consumes: `PromptDocument`, `validate_prompt_document`, `render_task_prompts`, `RenderedPrompts` from `vera_core.forms.prompting`; `FormSchemaDoc` from `vera_core.forms.dsl`.
- Produces: `POST /prompts/{prompt_id}/versions` body is `PromptDocument` (raises `BadRequestError` listing content errors); `GET /prompts/{prompt_id}/preview?version_id=<uuid optional>` → `ResponseModel[RenderedPrompts]`.

- [ ] **Step 1: Update the world fixture + add failing tests**

In `test_prompts.py`, find where the fixture seeds the published `PromptVersion` and set its `composite_json` to a valid prompt document (and ensure the seeded `SchemaVersion.schema_json` is a valid v2.1 document — if it currently stubs something smaller, replace it with the `schema_doc()` dict shape from `tests/unit/forms/test_prompt_document.py`, including `system_fields` and the `bg` context leaf):

```python
VALID_PROMPT_DOC: dict[str, Any] = {
    "kind": "prompt_document",
    "session": {
        "persona": "You are VERA.",
        "goal": "Verify benefits.",
        "base_instructions": "Ask one question at a time.",
    },
    "task_overrides": {},
}
```

Add tests (reuse the file's `prompts_world` fixture and `_auth` helper; match the URL prefix used by the existing tests in this file):

```python
async def test_create_draft_validates_document(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, world, ids = prompts_world
    url = f"/api/v1/prompts/{ids.prompt_id}/versions"
    headers = _auth(world.super_token)

    # not a prompt document at all → 422 (pydantic body validation)
    resp = await client.post(url, headers=headers, json={"composite_json": {}})
    assert resp.status_code == 422

    # unknown task key → 400
    bad_key = {**VALID_PROMPT_DOC, "task_overrides": {"ghost": {"prompt": "x"}}}
    resp = await client.post(url, headers=headers, json=bad_key)
    assert resp.status_code == 400
    assert "unknown task_key" in resp.text

    # unknown placeholder → 400
    bad_ph = {
        **VALID_PROMPT_DOC,
        "session": {**VALID_PROMPT_DOC["session"], "persona": "Hi {{patietn}}."},
    }
    resp = await client.post(url, headers=headers, json=bad_ph)
    assert resp.status_code == 400
    assert "unknown placeholder" in resp.text

    # valid document → 201 draft
    resp = await client.post(url, headers=headers, json=VALID_PROMPT_DOC)
    assert resp.status_code == 201


async def test_preview_renders_published_and_named_draft(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, world, ids = prompts_world
    headers = _auth(world.super_token)

    resp = await client.get(f"/api/v1/prompts/{ids.prompt_id}/preview", headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["persona"] == "You are VERA."
    assert data["tasks"] and all(t["prompt"] for t in data["tasks"])

    # a draft with an override previews differently when named explicitly
    draft_doc = {
        **VALID_PROMPT_DOC,
        "task_overrides": {"main": {"prompt": "OVERRIDDEN INSTRUCTIONS."}},
    }
    created = await client.post(
        f"/api/v1/prompts/{ids.prompt_id}/versions", headers=headers, json=draft_doc
    )
    draft_id = created.json()["data"]["id"]
    resp = await client.get(
        f"/api/v1/prompts/{ids.prompt_id}/preview",
        headers=headers,
        params={"version_id": draft_id},
    )
    assert resp.status_code == 200
    main = next(t for t in resp.json()["data"]["tasks"] if t["task_key"] == "main")
    assert main["prompt"].startswith("OVERRIDDEN INSTRUCTIONS.")


async def test_preview_forbidden_for_tenant(
    prompts_world: tuple[httpx.AsyncClient, World, PromptIds],
) -> None:
    client, world, ids = prompts_world
    resp = await client.get(
        f"/api/v1/prompts/{ids.prompt_id}/preview", headers=_auth(world.tenant_admin_token)
    )
    assert resp.status_code == 403
```

Adjust: the fixture's seeded schema must have task_key `"main"` for the override test — if the fixture seeds the real IBV document instead, use `"introduction"` and assert accordingly. Keep existing tests passing: any old test posting a free-form `composite_json` body now expects 422 — update those assertions to the new contract rather than deleting them.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/integration/control_plane/test_prompts.py -v`
Expected: new tests FAIL (404 on `/preview`; draft accepts anything).

- [ ] **Step 3: Implement in `prompts.py`**

Imports: add `from vera_core.forms.dsl import FormSchemaDoc` and `from vera_core.forms.prompting import PromptDocument, RenderedPrompts, render_task_prompts, validate_prompt_document`; add `BadRequestError` to the exceptions import. Delete `CreateDraftRequest`.

`create_draft` changes: body type becomes `PromptDocument`; fetch the full published `SchemaVersion` row (not just the id) so the document can be validated:

```python
async def create_draft(
    prompt_id: UUID,
    body: PromptDocument,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _WRITE],
) -> ResponseModel[PromptVersionDetail]:
    prompt = await _require_prompt(session, prompt_id)
    published_schema = (
        await session.execute(
            select(SchemaVersion).where(
                SchemaVersion.schema_id == prompt.schema_id,
                SchemaVersion.status == VersionStatus.PUBLISHED,
            )
        )
    ).scalar_one_or_none()
    if published_schema is None:
        raise ConflictError(message="no published schema to bind the prompt to")
    schema_doc = FormSchemaDoc.model_validate(published_schema.schema_json)
    content_errors = validate_prompt_document(body, schema_doc)
    if content_errors:
        raise BadRequestError(message="; ".join(content_errors))
```

…then the existing max_version/insert block with `schema_version_id=published_schema.id` and `composite_json=body.model_dump(mode="json")`. Add `DefaultExceptionCode.BAD_REQUEST` (or the file's equivalent member — check `DefaultExceptionCode`) to the route's `responses=`.

New preview endpoint (after `get_version`):

```python
@router.get(
    "/{prompt_id}/preview",
    response_model=ResponseModel[RenderedPrompts],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.CONFLICT,
    ),
)
async def preview_prompt(
    prompt_id: UUID,
    session: PlatformSession,
    _caller: Annotated[VerifiedIdentity, _READ],
    version_id: UUID | None = None,
) -> ResponseModel[RenderedPrompts]:
    """Effective rendered prompts: the named version's document (or the published
    one; none → factory + no overrides) over the schema document it pins."""
    prompt = await _require_prompt(session, prompt_id)
    version: PromptVersion | None
    if version_id is not None:
        version = (
            await session.execute(
                select(PromptVersion).where(
                    PromptVersion.id == version_id, PromptVersion.prompt_id == prompt.id
                )
            )
        ).scalar_one_or_none()
        if version is None:
            raise NotFoundError(message="unknown prompt version")
    else:
        version = (
            await session.execute(
                select(PromptVersion).where(
                    PromptVersion.prompt_id == prompt.id,
                    PromptVersion.status == VersionStatus.PUBLISHED,
                )
            )
        ).scalar_one_or_none()
    if version is not None:
        schema_version = (
            await session.execute(
                select(SchemaVersion).where(SchemaVersion.id == version.schema_version_id)
            )
        ).scalar_one()
        prompt_doc = PromptDocument.model_validate(version.composite_json)
    else:
        schema_version = (
            await session.execute(
                select(SchemaVersion).where(
                    SchemaVersion.schema_id == prompt.schema_id,
                    SchemaVersion.status == VersionStatus.PUBLISHED,
                )
            )
        ).scalar_one_or_none()
        if schema_version is None:
            raise ConflictError(message="no published schema to render against")
        prompt_doc = None
    schema_doc = FormSchemaDoc.model_validate(schema_version.schema_json)
    return ok(render_task_prompts(schema_doc, prompt_doc))
```

Route ordering caveat: FastAPI matches in declaration order — declare `/{prompt_id}/preview` BEFORE any `/{prompt_id}/versions/{version_id}` route only if path shapes could collide (they don't here, but keep preview above the POST publish route for readability).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/integration/control_plane/test_prompts.py -v`
Expected: ALL PASS (with a reachable DB).

- [ ] **Step 5: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/prompts.py tests/integration/control_plane/test_prompts.py
git commit -m "feat(api): typed prompt-document drafts with validation + rendered preview endpoint"
```

---

### Task 8: Full gate + code-simplifier pass

**Files:** whatever the simplifier refines in Tasks 1–7's files.

**Interfaces:** none new — this is the repo's definition of "done".

- [ ] **Step 1: Format + full gate**

Run (from `vera-backend/`): `just fmt && just check`
Expected: ruff format/lint clean, mypy --strict clean, unit tests green; integration tests green with a reachable DB (see Global Constraints for the stale-DB caveat — pre-existing fixture FK errors are not caused by this work, verify by checking the error matches the `prompt_version → schema_version` FK pattern and report it).
If `just fmt` reflowed code, re-run `uv run pytest tests/unit/forms/ -q` and `just compile-schemas` + `git status data/form_schemas/` (must be clean).

- [ ] **Step 2: Run the code-simplifier agent (repo CLAUDE.md rule — mandatory)**

Trigger the `code-simplifier` agent from `code-simplifier@claude-plugins-official` on: `packages/vera_core/src/vera_core/forms/dsl.py`, `packages/vera_core/src/vera_core/forms/prompting.py`, `packages/vera_core/src/vera_core/forms/prompt_text.py`, `packages/vera_core/src/vera_core/forms/catalog/ibv_standard.py`, `scripts/seed.py`, `apps/control_plane/src/control_plane/api/v1/prompts.py`, and the touched test files. Constraints for it: behavior-preserving only; snapshot files and all spoken/rendered text strings are contracts — never reword; error-message strings are asserted by tests.

- [ ] **Step 3: Re-run the gate; commit refinements**

Run: `just check` (plus `just compile-schemas` + clean `git status data/form_schemas/` if the catalog was touched).

```bash
git add -A packages/vera_core apps/control_plane scripts tests data/form_schemas
git commit -m "refactor(forms): simplifier pass over prompt-compiler changes"
```

(Skip the commit if nothing changed.)

---

## Self-Review Notes

- **Spec coverage:** §3 renderer + §3.1 assembly → Task 5; §3.2 condition-to-text → Task 4; §3.3 attachment → Task 5 (`_last_ref_task`); §3.4 ConfirmInTask → Task 1; §4 PromptDocument + placeholder widening + save-time validation → Tasks 2, 3, 7; §5 store semantics → Tasks 6, 7; §6/§6.1/§6.2 seeder bootstrap/carry-forward → Task 6; §7 API → Task 7; §8 consumption contract → deliberately not built (spec non-goal); §9 tests → distributed per task incl. golden snapshots (Task 5); §10 edge cases → factory fallback (Task 5 test), carry-forward drop (Task 6 test), ritual `sections: []` renders instructions-only (covered by introduction task having one section + disease_only render test; the empty-sections shape itself is validated in the existing dsl test).
- **Deviation (named):** the dumped-JSON IR (`compile_prompt_document`) is deleted in Task 6 rather than kept as the renderer's internal representation — the text renderer needs `Condition` model objects, which the dumped IR loses; the reused logic is the `leaf_gates` traversal and grouping. Recorded in the plan header.
- **Type consistency:** `render_task_prompts(doc, prompt_doc=None)` used identically in Tasks 5–7; `PromptDocument`/`TaskTextOverride`/`FACTORY_SESSION` names match across Tasks 3, 6, 7; `condition_field_paths(cond, shared)` signature matches between Task 1 (dsl) and Task 5 (renderer); summary strings in Task 6's code match its test assertions ("carried forward", "current", "factory").
- **Known adaptation points (explicitly delegated to the implementer, with the target named):** exact `dsl.Validation`/`Codes` attribute names (Task 5 note); `SchemaVersion` constructor columns in the carry-forward test (mirror `test_seed_form_schemas.py`); the `prompts_world` fixture's seeded schema/task_key (Task 7 note); `DefaultExceptionCode` member for 400 (Task 7).
