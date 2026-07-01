# Retry / Partial Gap-Resolution Prompt — Implementation Plan (v2, phased)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Given a form's stored answers, produce the *minimal* set of still-needed questions and a follow-up prompt that asks only those — so a second call to the same form doesn't restart from scratch.

**Architecture:** Two pure functions in `vera_core/forms/` — `compute_gaps` (what's still needed, validation-aware) and `compose_gap_prompt` (a follow-up prompt for just those gaps). A deferred, gated wiring layer ships the prompt to the worker via `PersonaTweak.instructions_override` in LiveKit dispatch metadata. No schema-format change; no new worker→DB path.

**Tech Stack:** Python 3.12, SQLAlchemy async, Pydantic v2, pytest. PEP 695 type params, ruff + mypy --strict.

**Design spec:** `docs/superpowers/specs/2026-07-01-retry-gap-resolution-prompt-design.md`

**The core insight driving this v2 structure:** the only thing that matters is that `compute_gaps` is *correct against the real schema*. Everything downstream (prompt, wiring) is worthless if it's wrong, and blocked/low-value until the answer write-back prerequisite exists. So we ground-truth first, build the engine tested against the real schema, and stop honestly at the write-back boundary.

## Where the data lives

- **Schema (disk / test fixture):** `data/form_schemas/ibv_form_standard.json` (+ `manifest.json`).
- **Schema (runtime):** `schema_version.schema_json` (JSONB) — model `SchemaVersion` (`packages/vera_core/src/vera_core/models/authoring.py:44`); a `PatientForm` binds via `schema_version_id`.
- **Stored answers:** `patient_form.intake_payload` (JSONB) today holds intake context only; call-captured answers arrive later via the write-back prereq.
- **Existing helpers to reuse:** `vera_core/forms/intake.py` — `_is_empty`, `iter_leaf_answers`, `required_intake_fields`.

## Global Constraints

- PEP 695 type params only — no `Generic[T]`/`TypeVar` (ruff rejects them).
- `GapReport` carries **paths and counts only, never field values** — safe to log/audit.
- Gap engine + renderer are **pure**: no LLM, no DB, no I/O. Deterministic, order-preserving.
- Reuse `intake.py._is_empty` for emptiness — do not re-implement.
- **Synthetic / role-play data only** until the real `PHIBoundary` is wired (currently the passthrough stub).
- Call→DB answer write-back is **out of scope** — the engine takes a merged `known_values` dict; `prior_call_answers` defaults to `{}`.
- Run `just check` before each commit. Run `/simplify` then `just check` before declaring the feature done.

---

## Phase 0 — Ground-truth spike (no shipping code)

**Why:** the v1 plan coded against assumed shapes and baked in bugs (checked `"kind"` when the real key is `"category"`; dropped the `auto-fill` effect; ported service-groups that don't exist). This phase replaces every assumption with a fact from the real schema before a line of engine code is written.

**Files:**
- Create: `tests/unit/forms/fixtures/ibv_gap_fixture.json` (a trimmed real-schema slice)
- Create: `docs/superpowers/notes/2026-07-01-gap-engine-ground-truth.md` (findings)

- [ ] **Step 1: Catalogue the real rule effects and their meaning.** From `data/form_schemas/ibv_form_standard.json`, the `rules[].effect` values are: `make this required`, `ask this question`, `auto-fill a value`, `terminate_call_when`. Write down, for each, what the gap engine must do:
  - `make this required` → field is a gap-candidate when the rule's conditions match.
  - `ask this question` → conditional-ask; field is NOT a gap when conditions are unmet (skip).
  - `auto-fill a value` → when conditions match, the listed value is treated as *already filled* (never a gap).
  - `terminate_call_when` → out of scope for gap detection (record, don't act on it here).
- [ ] **Step 2: Confirm `constraint_library` entry shapes.** Record the real keys (observed: `{"category": "enum", "values": [...]}`). Find and record the shape of any `format`/regex constraints (or confirm none exist). This fixes the validation code.
- [ ] **Step 3: Confirm field/answer shape.** Confirm there is **no** `category:"service"` / `sub_fields` (verified: categories are `enum/field/note/intro_prose/policy`) → service-group handling is cut. Record how `conditions[].field` references a field (bare name, e.g. `coverage_type`) and note the cross-section same-name collision assumption.
- [ ] **Step 4: Build the test fixture.** Extract 2–3 real sections into `ibv_gap_fixture.json`: one with a static-required field, one with a conditional-required rule, one with a conditional-ask rule, one with an `auto-fill` rule, one `always_ask` field (the call reference number), and the `constraint_library` entries they use. This is the fixture every Phase 1/2 test runs against.
- [ ] **Step 5: Write the findings note and commit.**

```bash
git add tests/unit/forms/fixtures/ibv_gap_fixture.json docs/superpowers/notes/2026-07-01-gap-engine-ground-truth.md
git commit -m "docs(retry): ground-truth spike — real schema shapes + gap fixture"
```

**Gate:** if any shape differs from the assumptions encoded in Phase 1 below, update Phase 1's code/tests to match before proceeding.

**Delivers:** a real-schema fixture + a findings note. No production code.

---

## Phase 1 — Pure gap engine (the core deliverable)

Home: `vera-backend/packages/vera_core/src/vera_core/forms/gaps.py`, tests in `tests/unit/forms/test_gaps.py`. All tests load `ibv_gap_fixture.json` from Phase 0 — no invented schema shapes.

### Task 1.1: Constraint validation — `_passes_constraint`

**Interfaces produced:** `_passes_constraint(value, field, library) -> bool`.

- [ ] **Step 1: Failing tests** (use real `constraint_library` category shape)

```python
from vera_core.forms.gaps import _passes_constraint

LIBRARY = {"YES_NO": {"category": "enum", "values": ["Yes", "No"]}}

def test_enum_member_passes():
    assert _passes_constraint("Yes", {"constraint_ref": "YES_NO"}, LIBRARY) is True

def test_enum_non_member_fails():
    assert _passes_constraint("Maybe", {"constraint_ref": "YES_NO"}, LIBRARY) is False

def test_enum_trim_and_case_insensitive():
    assert _passes_constraint("  no ", {"constraint_ref": "YES_NO"}, LIBRARY) is True

def test_inline_enum_list_supported():
    assert _passes_constraint("Female", {"enum": ["Female", "Male"]}, {}) is True

def test_no_constraint_accepts_any_nonempty():
    assert _passes_constraint("free text", {"type": "string"}, {}) is True

def test_unrecognized_constraint_is_permissive():
    # Conservative: an unparseable constraint must NOT cause a false re-ask.
    assert _passes_constraint("x", {"constraint_ref": "UNKNOWN_REF"}, {}) is True
```

- [ ] **Step 2: Run — expect FAIL** (`ImportError`). `just test tests/unit/forms/test_gaps.py -q`

- [ ] **Step 3: Implement** (note: `category`, not `kind`; permissive on unknown)

```python
"""Gap detection over a form schema + stored values (pure, no LLM, no DB).

Reads the production schema shape (sections[].properties{} with required_state,
rules[], constraint_ref/enum) and the constraint_library (category enum/format).
"""

from __future__ import annotations

import re
from typing import Any


def _enum_values(field: dict[str, Any], library: dict[str, Any]) -> list[str] | None:
    if isinstance(field.get("enum"), list):
        return [str(v) for v in field["enum"]]
    entry = library.get(field.get("constraint_ref"))
    if isinstance(entry, dict) and entry.get("category") == "enum" and entry.get("values"):
        return [str(v) for v in entry["values"]]
    return None


def _format_regex(field: dict[str, Any], library: dict[str, Any]) -> str | None:
    entry = library.get(field.get("constraint_ref"))
    if isinstance(entry, dict) and entry.get("category") == "format":
        return entry.get("regex") or entry.get("pattern")
    return None


def _passes_constraint(value: object, field: dict[str, Any], library: dict[str, Any]) -> bool:
    """True if a NON-EMPTY value satisfies its enum/format constraint. Conservative:
    a field with no *recognized* constraint always passes (never a false re-ask)."""
    text = str(value).strip()
    values = _enum_values(field, library)
    if values is not None:
        return text.casefold() in {v.strip().casefold() for v in values}
    regex = _format_regex(field, library)
    if regex is not None:
        return re.search(regex, text) is not None
    return True
```

- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(forms): constraint validation for gap detection`.

### Task 1.2: Condition evaluator + `is_filled`

**Interfaces produced:** `is_filled(value, field, library) -> bool`; `rule_matches(rule, known) -> bool`.

- [ ] **Step 1: Failing tests**

```python
from vera_core.forms.gaps import is_filled, rule_matches

REQ_WHEN_FAMILY = {"effect": "make this required", "match": "all of these",
    "conditions": [{"field": "coverage_type", "comparison": "is", "value": "Family"}]}

def test_empty_or_na_not_filled():
    assert is_filled("", {"type": "string"}, {}) is False
    assert is_filled("N/A", {"type": "string"}, {}) is False

def test_present_but_invalid_not_filled():
    assert is_filled("Maybe", {"enum": ["Yes", "No"]}, {}) is False

def test_present_and_valid_filled():
    assert is_filled("Yes", {"enum": ["Yes", "No"]}, {}) is True

def test_rule_all_of_these():
    assert rule_matches(REQ_WHEN_FAMILY, {"coverage_type": "Family"}) is True
    assert rule_matches(REQ_WHEN_FAMILY, {"coverage_type": "Individual"}) is False

def test_rule_any_of_these():
    rule = {"match": "any of these", "conditions": [
        {"field": "a", "comparison": "is", "value": "1"},
        {"field": "b", "comparison": "is", "value": "2"}]}
    assert rule_matches(rule, {"b": "2"}) is True
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement**

```python
from vera_core.forms.intake import _is_empty


def is_filled(value: object, field: dict[str, Any], library: dict[str, Any]) -> bool:
    """Non-empty AND passes its constraint."""
    if _is_empty(value):
        return False
    return _passes_constraint(value, field, library)


def _cond_holds(cond: dict[str, Any], known: dict[str, Any]) -> bool:
    actual = known.get(cond.get("field"))
    equal = actual is not None and \
        str(actual).strip().casefold() == str(cond.get("value")).strip().casefold()
    return (not equal) if cond.get("comparison") == "is not" else equal


def rule_matches(rule: dict[str, Any], known: dict[str, Any]) -> bool:
    conds = [c for c in rule.get("conditions", []) if isinstance(c, dict)]
    if not conds:
        return False
    results = [_cond_holds(c, known) for c in conds]
    return any(results) if rule.get("match") == "any of these" else all(results)
```

- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(forms): condition evaluator + validation-aware is_filled`.

### Task 1.3: `compute_gaps` — required, conditional-ask, auto-fill, always_ask

**Interfaces produced:** `Gap`, `GapReport`, `compute_gaps(schema_json, known_values, *, constraint_library=None) -> GapReport`.

- [ ] **Step 1: Failing tests — against the REAL fixture**

```python
import json, pathlib
from vera_core.forms.gaps import compute_gaps, Gap

FIXTURE = json.loads((pathlib.Path(__file__).parent / "fixtures" / "ibv_gap_fixture.json").read_text())

def _paths(report): return [g.path for g in report.missing]

def test_static_required_missing_is_a_gap():
    report = compute_gaps(FIXTURE, {})
    assert any(p.endswith(".coverage_type") for p in _paths(report))

def test_conditional_required_fires_and_silences():
    fired = compute_gaps(FIXTURE, {"benefit_coverage": {"coverage_type": "Family"}})
    silent = compute_gaps(FIXTURE, {"benefit_coverage": {"coverage_type": "Individual"}})
    assert any("spouse" in p for p in _paths(fired))
    assert not any("spouse" in p for p in _paths(silent))

def test_conditional_ask_unmet_is_not_a_gap():
    # a field gated by an "ask this question" rule whose condition is unmet
    report = compute_gaps(FIXTURE, {"insurance_information": {"doctor_inside_network": "Yes",
                                                              "facility_inside_network": "Yes"}})
    assert not any("out_of_network" in p for p in _paths(report))

def test_auto_fill_field_is_not_a_gap():
    # when the trigger condition matches, an auto-filled field is treated as filled
    report = compute_gaps(FIXTURE, {"benefit_coverage": {"benefit_year_type": "Calendar Year"}})
    assert not any("plan_effective_date" in p for p in _paths(report))

def test_always_ask_surfaced_separately():
    report = compute_gaps(FIXTURE, {})
    assert "call_reference_number" in report.always_ask
    assert not any("call_reference_number" in p for p in _paths(report))

def test_full_valid_fill_returns_empty_missing():
    # a fixture-complete answer set → no missing gaps (always_ask may still be set)
    report = compute_gaps(FIXTURE, json.loads(
        (pathlib.Path(__file__).parent / "fixtures" / "ibv_gap_fixture_complete.json").read_text()))
    assert report.missing == []
```

(Phase 0 Step 4 also produces `ibv_gap_fixture_complete.json`, a fully-valid answer set for the fixture.)

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** (flat fields; required-when, ask-when skip, auto-fill, always_ask; NO services)

```python
from dataclasses import dataclass, field as dc_field

from vera_core.forms.intake import iter_leaf_answers


@dataclass(frozen=True)
class Gap:
    path: str
    kind: str = "field"  # "field" | "gate"


@dataclass(frozen=True)
class GapReport:
    missing: list[Gap] = dc_field(default_factory=list)
    skipped: list[tuple[str, str]] = dc_field(default_factory=list)
    always_ask: list[str] = dc_field(default_factory=list)


def _flatten(known: dict[str, Any]) -> dict[str, Any]:
    """Flatten nested known values to {leaf_key: value}; schema conditions
    reference fields by bare name (last path segment)."""
    return {path.split(".")[-1]: value for path, value in iter_leaf_answers(known)}


def _rules(field: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in field.get("rules", []) if isinstance(r, dict)]


def _is_required(field: dict[str, Any], flat: dict[str, Any]) -> bool:
    if field.get("required_state") == "required":
        return True
    return any(r.get("effect") == "make this required" and rule_matches(r, flat) for r in _rules(field))


def _is_conditionally_skipped(field: dict[str, Any], flat: dict[str, Any]) -> bool:
    ask_rules = [r for r in _rules(field) if r.get("effect") == "ask this question"]
    return bool(ask_rules) and not any(rule_matches(r, flat) for r in ask_rules)


def _is_auto_filled(field: dict[str, Any], flat: dict[str, Any]) -> bool:
    return any(r.get("effect") == "auto-fill a value" and rule_matches(r, flat) for r in _rules(field))


def compute_gaps(
    schema_json: dict[str, Any],
    known_values: dict[str, Any],
    *,
    constraint_library: dict[str, Any] | None = None,
) -> GapReport:
    """Which fields still need asking, in schema order. Pure; names/counts only."""
    library = constraint_library or schema_json.get("constraint_library") or {}
    flat = _flatten(known_values)
    report = GapReport()
    for section in schema_json.get("sections", []):
        skey = section.get("section_key", "")
        for name, fld in (section.get("properties") or {}).items():
            if not isinstance(fld, dict):
                continue
            if fld.get("always_ask"):
                report.always_ask.append(name)
                continue
            if _is_conditionally_skipped(fld, flat):
                report.skipped.append((f"{skey}.{name}", "ask-when unmet"))
                continue
            if not _is_required(fld, flat):
                continue
            if _is_auto_filled(fld, flat):
                report.skipped.append((f"{skey}.{name}", "auto-filled"))
                continue
            if not is_filled(flat.get(name), fld, library):
                report.missing.append(Gap(path=f"{skey}.{name}"))
    return report
```

- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(forms): compute_gaps engine (required/ask/auto-fill/always_ask)`.

**Phase 1 delivers:** a correct, real-schema-validated `compute_gaps`. Independently mergeable; no app wiring.

---

## Phase 2 — Gap-resolution prompt renderer

Home: `vera-backend/packages/vera_core/src/vera_core/forms/gap_prompt.py`, tests in `tests/unit/forms/test_gap_prompt.py` (against the same fixture).

### Task 2.1: `compose_gap_prompt`

**Interfaces produced:** `compose_gap_prompt(schema_json, report, known_values) -> str`.

- [ ] **Step 1: Failing tests**

```python
import json, pathlib
from vera_core.forms.gaps import compute_gaps
from vera_core.forms.gap_prompt import compose_gap_prompt

FIXTURE = json.loads((pathlib.Path(__file__).parent / "fixtures" / "ibv_gap_fixture.json").read_text())

def test_context_block_says_do_not_reintroduce():
    out = compose_gap_prompt(FIXTURE, compute_gaps(FIXTURE, {}), {})
    assert "<gap_resolution_context>" in out
    assert "do not re-introduce" in out.lower()

def test_missing_field_question_rendered():
    out = compose_gap_prompt(FIXTURE, compute_gaps(FIXTURE, {}), {})
    assert "coverage_type" in out  # the missing field's <field name> appears

def test_always_ask_in_closing_block():
    out = compose_gap_prompt(FIXTURE, compute_gaps(FIXTURE, {}), {})
    assert "<closing_block>" in out and "call_reference_number" in out

def test_nothing_missing_returns_marker():
    complete = json.loads((pathlib.Path(__file__).parent / "fixtures" / "ibv_gap_fixture_complete.json").read_text())
    report = compute_gaps(FIXTURE, complete)
    # temporarily clear always_ask for this assertion, or assert on the no-missing marker
    out = compose_gap_prompt(FIXTURE, report, complete)
    assert "no gap follow-up needed" in out.lower() or "<closing_block>" in out
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** (reuse the production field wording via `prompt.ask`; keep raw stored values OUT of the prompt until the PHIBoundary gate is resolved)

```python
"""Render a follow-up ('gap resolution') prompt that re-asks only the gaps.
Pure; consumes a GapReport from vera_core.forms.gaps. Raw stored values are NOT
embedded (PHI gate, spec §9) — the known-facts context is added with write-back."""

from __future__ import annotations

from typing import Any

from vera_core.forms.gaps import GapReport


def _field_by_name(schema_json: dict[str, Any], name: str) -> tuple[str, dict[str, Any]] | None:
    for section in schema_json.get("sections", []):
        props = section.get("properties") or {}
        if name in props:
            return section.get("section_key", ""), props[name]
    return None


def _ask_text(field: dict[str, Any], name: str) -> str:
    prompt = field.get("prompt") or {}
    return prompt.get("ask") or field.get("description") or field.get("title") or name


def _render(schema_json: dict[str, Any], path_or_name: str) -> str:
    name = path_or_name.split(".")[-1]
    found = _field_by_name(schema_json, name)
    field = found[1] if found else {}
    return f'<field name="{path_or_name}"><ask>{_ask_text(field, name)}</ask></field>'


def compose_gap_prompt(
    schema_json: dict[str, Any], report: GapReport, known_values: dict[str, Any]
) -> str:
    if not report.missing and not report.always_ask:
        return ("<gap_resolution_context>All required questions are answered. "
                "No gap follow-up needed.</gap_resolution_context>")
    lines = [
        "<gap_resolution_context>",
        "You are following up on a call where some questions were missed or unclear.",
        "Do not re-introduce yourself. Pick up naturally with the representative.",
        "</gap_resolution_context>",
        '<section mode="gap_resolution">',
        *[_render(schema_json, g.path) for g in report.missing],
        "</section>",
    ]
    if report.always_ask:
        lines.append("<closing_block>")
        lines.extend(_render(schema_json, n) for n in report.always_ask)
        lines.append("</closing_block>")
    return "\n".join(lines)
```

- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(forms): compose_gap_prompt renderer`.

**Phase 2 delivers:** a follow-up prompt string from a `GapReport`. With Phase 1, the whole schema→gap→prompt path is done and unit-tested against the real schema.

---

## Phase 3 — Wiring & delivery (DEFERRED — gated on write-back)

**Do not build on the default call path until the call→DB answer write-back exists.** Build first as a **dry-run** (log only), so correctness is proven on real production forms before anything changes a live call.

### Task 3.1: `PersonaTweak.instructions_override` + worker honors it

**Files:** `vera_core/schemas` (PersonaTweak), `apps/agent_worker/src/agent_worker/prompt.py:92`, test `apps/agent_worker/tests/unit/test_prompt.py`.

- [ ] Add `instructions_override: str | None = None` to `PersonaTweak`.
- [ ] `build_instructions`: when `tweak.instructions_override` is set, return it (+ `CARTESIA_MARKUP_GUIDE`) instead of `SYSTEM_PROMPT`; else unchanged.
- [ ] Tests: override wins; absent → base; malformed metadata → base (fail-safe). Commit.

```python
def build_instructions(tweak: PersonaTweak | None = None) -> str:
    if tweak is not None and tweak.instructions_override:
        return "\n\n".join([tweak.instructions_override, CARTESIA_MARKUP_GUIDE])
    parts = [SYSTEM_PROMPT]
    if tweak is not None and tweak.extra_instructions:
        parts.append(tweak.extra_instructions)
    parts.append(CARTESIA_MARKUP_GUIDE)
    return "\n\n".join(parts)
```

### Task 3.2: Shared trigger helper + dry-run

**Files:** create `apps/control_plane/src/control_plane/services/retry_prompt.py`; test `tests/integration/control_plane/test_retry_prompt.py`.

- [ ] `async def gap_prompt_for_form(session, form) -> str | None`: find the form's most recent terminal `Call`; if `call_reference_no` is set, load `schema_version.schema_json`, build `known_values = dict(form.intake_payload or {})` (prior-call answers `{}` until write-back), run `compute_gaps` → `compose_gap_prompt`; else return `None`.
- [ ] **Dry-run first:** call `gap_prompt_for_form` at dispatch and `logger.info` the gap count + paths (no values) **without** attaching it to metadata. Ship this, watch real forms, confirm the gaps are sane.

```python
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from vera_core.forms.gap_prompt import compose_gap_prompt
from vera_core.forms.gaps import compute_gaps
from vera_core.models import Call, PatientForm, SchemaVersion


async def gap_prompt_for_form(session: AsyncSession, form: PatientForm) -> str | None:
    last = (await session.execute(
        select(Call).where(Call.form_id == form.id).order_by(Call.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if last is None or not last.call_reference_no:
        return None
    schema_json = (await session.execute(
        select(SchemaVersion.schema_json).where(SchemaVersion.id == form.schema_version_id)
    )).scalar_one()
    known = dict(form.intake_payload or {})
    return compose_gap_prompt(schema_json, compute_gaps(schema_json, known), known)
```

### Task 3.3: Attach to both call-creation paths (only after write-back)

- [ ] Call `gap_prompt_for_form` and, when non-None, set `tweak.instructions_override` before `metadata = tweak.model_dump(...)` — in **both** `calls.py::start_call` **and** the queue dispatcher's call-creation branch (PR #33). Keep the composition in the single shared helper; both paths call it.
- [ ] **Measure dispatch metadata size** with a real gap prompt. If it exceeds LiveKit's metadata limit, switch delivery to a worker-fetch-by-`call_id` internal endpoint (spec §8.3 alternative).

### Task 3.4: Compliance note

**Files:** `adr/devops-todo.md`.

- [ ] Add a row: retry gap prompt embeds stored values (incl. PHI) into the system prompt + dispatch metadata, bypassing the worker `redact()` seam; gate real-PHI use behind the real `PHIBoundary` (or seed the session vault with the known values so tokens stay consistent). Synthetic data only until then.
- [ ] Run `/simplify` on the new modules, then `just check` (ruff + mypy --strict + pytest). Commit.

**Phase 3 delivers (when unblocked):** the retry prompt actually reaching a live call, proven first via dry-run.

---

## Key decisions & tradeoffs

- **Conservative validation:** invalid→re-ask, but an *unrecognized* constraint treats the value as filled (`_passes_constraint` returns True). Favors call quality (no false re-asks from a parser gap) over strictness.
- **Bare-field-name condition keys:** matches the schema's `conditions[].field` shape; accepts a theoretical cross-section same-name collision (none observed). Documented in Phase 0.
- **Reuse production field wording** (`prompt.ask`) rather than a toy renderer — keeps the follow-up prompt faithful to the live script.
- **Values stay out of the prompt for now** — the known-facts context is deferred with write-back, sidestepping the PHI-in-prompt decision until the boundary is real.

## Risks & handling

| Risk | Handling |
|---|---|
| Can't run end-to-end without write-back | Phases 1–2 are independently valuable + tested; Phase 3 gated + dry-run first. |
| False re-asks from misparsed constraints/conditions | Conservative validation; **all tests run on the real schema**; dry-run before any live use. |
| Two call-creation paths (PR #33 coupling) | One shared `gap_prompt_for_form` helper; wire both `start_call` and the dispatcher; coordinate with Yash. |
| PHI in prompt/metadata | Synthetic-only + `devops-todo` row; real-PHI deferred to `PHIBoundary`. |
| Dispatch metadata too large for the full prompt | Measure in Phase 3; fall back to worker-fetch-by-`call_id`. |

## Explicitly out of scope

- Call→DB answer write-back (the prerequisite — separate spec).
- Real-PHI handling / tokenization in the prompt.
- Service-group re-ask (no `service`/`sub_fields` in the production schema).
- `terminate_call_when` / early-termination logic in the gap prompt.
- Non-IBV / tenant-specific schema variants.

## Self-Review

- **Spec coverage:** engine → Phase 1; validation → Tasks 1.1–1.2; renderer → Phase 2; trigger/delivery → Phase 3; PHI → Task 3.4; write-back prereq → `known_values` default `{}`. Covered.
- **Placeholder scan:** all code steps show real code; deferrals (known-facts context, prior-call answers) are documented, not gaps.
- **Type consistency:** `compute_gaps`/`Gap`/`GapReport`/`is_filled`/`rule_matches`/`compose_gap_prompt`/`gap_prompt_for_form`/`instructions_override` consistent across phases.

## Changes from v1

Added Phase 0 ground-truth spike (kills the assumed-shape bugs). Cut service-group integrity (not in the real schema). Added `auto-fill` + real 4-effect handling (v1 dropped auto-fill, a re-ask bug). Corrected constraint key `kind`→`category`. Moved all tests onto the real schema fixture. Demoted wiring to a deferred, gated Phase 3 with a dry-run stage. Made validation conservative. Unified the trigger into one helper called by both call paths.
