# Retry / Partial Gap-Resolution Prompt — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a form is called again after a prior call captured a `call_reference_no`, ask only the still-missing/invalid fields instead of running the full script from scratch.

**Architecture:** Two pure functions in `vera_core/forms/` — `compute_gaps` (what's still needed, validation-aware) and `compose_gap_prompt` (a follow-up prompt for just those gaps) — plus thin control-plane wiring that ships the composed prompt to the worker via LiveKit dispatch metadata (`PersonaTweak.instructions_override`). No new worker→DB path; no schema-format change.

**Tech Stack:** Python 3.12, SQLAlchemy async, Pydantic v2, pytest. PEP 695 type params (`def f[T]`), ruff + mypy strict.

**Design spec:** `docs/superpowers/specs/2026-07-01-retry-gap-resolution-prompt-design.md`

## Global Constraints

- PEP 695 type params only — no `Generic[T]`/`TypeVar` (ruff rejects them).
- `GapReport` carries **paths and counts only, never field values** — it must be safe to log/audit.
- Gap engine + renderer are **pure**: no LLM, no DB, no I/O. Deterministic, order-preserving.
- Reuse `vera_core/forms/intake.py` helpers (`_is_empty`, `iter_leaf_answers`) — do not re-implement "emptiness".
- **Synthetic / role-play data only** for this feature until the real `PHIBoundary` is wired (it is currently the passthrough stub).
- Call→DB answer write-back is **out of scope** — `prior_call_answers` defaults to `{}`.
- Run `just check` (ruff + mypy + pytest) before each commit. After the feature is complete, run `/simplify` then `just check` again.

---

### Task 1: Field validation — `_passes_constraint`

**Files:**
- Create: `vera-backend/packages/vera_core/src/vera_core/forms/gaps.py`
- Test: `vera-backend/tests/unit/forms/test_gaps.py`

**Interfaces:**
- Consumes: nothing (new module).
- Produces: `_passes_constraint(value: object, field: dict, library: dict) -> bool` — True if `value` satisfies the field's enum / format constraint (or the field has none).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/forms/test_gaps.py
from vera_core.forms.gaps import _passes_constraint

LIBRARY = {"YESNO": {"kind": "enum", "values": ["Yes", "No"]},
           "PHONE": {"kind": "format", "regex": r"^\d{10}$"}}

class TestPassesConstraint:
    def test_inline_enum_member_passes(self):
        assert _passes_constraint("Female", {"enum": ["Female", "Male"]}, {}) is True

    def test_inline_enum_non_member_fails(self):
        assert _passes_constraint("Maybe", {"enum": ["Female", "Male"]}, {}) is False

    def test_enum_is_case_and_space_insensitive(self):
        assert _passes_constraint("  yes ", {"constraint_ref": "YESNO"}, LIBRARY) is True

    def test_format_match_passes_mismatch_fails(self):
        assert _passes_constraint("5551234567", {"constraint_ref": "PHONE"}, LIBRARY) is True
        assert _passes_constraint("555-1234", {"constraint_ref": "PHONE"}, LIBRARY) is False

    def test_no_constraint_accepts_any_nonempty(self):
        assert _passes_constraint("free text", {"type": "string"}, {}) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `just test tests/unit/forms/test_gaps.py -q` (or `pytest ... -q`)
Expected: FAIL — `ImportError: cannot import name '_passes_constraint'`.

- [ ] **Step 3: Implement `_passes_constraint`**

```python
"""Gap detection over a form schema + stored values (pure, no LLM, no DB).

Ports the vera-schema-builder POC computeGaps to the production schema shape
(sections[].properties{} with required_state, rules[], constraint_ref/enum).
"""

from __future__ import annotations

import re
from typing import Any


def _enum_values(field: dict[str, Any], library: dict[str, Any]) -> list[str] | None:
    if isinstance(field.get("enum"), list):
        return [str(v) for v in field["enum"]]
    ref = field.get("constraint_ref")
    entry = library.get(ref) if ref else None
    if isinstance(entry, dict) and entry.get("kind") == "enum" and entry.get("values"):
        return [str(v) for v in entry["values"]]
    return None


def _format_regex(field: dict[str, Any], library: dict[str, Any]) -> str | None:
    ref = field.get("constraint_ref")
    entry = library.get(ref) if ref else None
    if isinstance(entry, dict) and entry.get("kind") == "format":
        return entry.get("regex")
    return None


def _passes_constraint(value: object, field: dict[str, Any], library: dict[str, Any]) -> bool:
    """True if a NON-EMPTY value satisfies the field's enum/format constraint.

    Free-text fields (no constraint) always pass. Enum comparison is trimmed +
    case-insensitive; format is a full regex search on the string value.
    """
    text = str(value).strip()
    values = _enum_values(field, library)
    if values is not None:
        return text.casefold() in {v.strip().casefold() for v in values}
    regex = _format_regex(field, library)
    if regex is not None:
        return re.search(regex, text) is not None
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `just test tests/unit/forms/test_gaps.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/gaps.py tests/unit/forms/test_gaps.py
git commit -m "feat(forms): constraint validation for gap detection"
```

---

### Task 2: Condition evaluator + `is_filled`

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/gaps.py`
- Test: `vera-backend/tests/unit/forms/test_gaps.py`

**Interfaces:**
- Consumes: `_passes_constraint` (Task 1), `vera_core.forms.intake._is_empty`.
- Produces:
  - `is_filled(value, field, library) -> bool`
  - `rule_matches(rule: dict, known: dict) -> bool` — evaluates a schema `rules[]` condition block against known values.

- [ ] **Step 1: Write the failing tests**

```python
from vera_core.forms.gaps import is_filled, rule_matches

REQUIRED_WHEN_FAMILY = {
    "effect": "make this required", "match": "all of these",
    "conditions": [{"field": "coverage_type", "comparison": "is", "value": "Family"}],
}

class TestIsFilled:
    def test_empty_is_not_filled(self):
        assert is_filled("", {"type": "string"}, {}) is False
        assert is_filled("N/A", {"type": "string"}, {}) is False

    def test_present_but_invalid_is_not_filled(self):
        assert is_filled("Maybe", {"enum": ["Yes", "No"]}, {}) is False

    def test_present_and_valid_is_filled(self):
        assert is_filled("Yes", {"enum": ["Yes", "No"]}, {}) is True

class TestRuleMatches:
    def test_all_of_these_true_when_condition_holds(self):
        assert rule_matches(REQUIRED_WHEN_FAMILY, {"coverage_type": "Family"}) is True

    def test_all_of_these_false_when_condition_absent(self):
        assert rule_matches(REQUIRED_WHEN_FAMILY, {"coverage_type": "Individual"}) is False

    def test_any_of_these_true_when_one_holds(self):
        rule = {"match": "any of these", "conditions": [
            {"field": "a", "comparison": "is", "value": "1"},
            {"field": "b", "comparison": "is", "value": "2"}]}
        assert rule_matches(rule, {"b": "2"}) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `just test tests/unit/forms/test_gaps.py -q`
Expected: FAIL — `ImportError` for `is_filled` / `rule_matches`.

- [ ] **Step 3: Implement `is_filled` and `rule_matches`**

```python
from vera_core.forms.intake import _is_empty


def is_filled(value: object, field: dict[str, Any], library: dict[str, Any]) -> bool:
    """A value is 'filled' only if non-empty AND it passes its constraint."""
    if _is_empty(value):
        return False
    return _passes_constraint(value, field, library)


def _cond_holds(cond: dict[str, Any], known: dict[str, Any]) -> bool:
    field = cond.get("field")
    expected = cond.get("value")
    actual = known.get(field)
    negate = cond.get("comparison") == "is not"
    equal = actual is not None and str(actual).strip().casefold() == str(expected).strip().casefold()
    return (not equal) if negate else equal


def rule_matches(rule: dict[str, Any], known: dict[str, Any]) -> bool:
    """Evaluate a schema rules[] condition block against known values.
    Supports match='all of these' (AND) and 'any of these' (OR)."""
    conds = [c for c in rule.get("conditions", []) if isinstance(c, dict)]
    if not conds:
        return False
    results = [_cond_holds(c, known) for c in conds]
    return any(results) if rule.get("match") == "any of these" else all(results)
```

Note: `known` is the flattened `{field_key: value}` view — Task 3 builds it via `iter_leaf_answers` (last path segment as the key, matching how schema conditions reference fields by bare name).

- [ ] **Step 4: Run tests to verify they pass**

Run: `just test tests/unit/forms/test_gaps.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/gaps.py tests/unit/forms/test_gaps.py
git commit -m "feat(forms): condition evaluator + validation-aware is_filled"
```

---

### Task 3: `compute_gaps` — the engine

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/gaps.py`
- Test: `vera-backend/tests/unit/forms/test_gaps.py`

**Interfaces:**
- Consumes: `is_filled`, `rule_matches` (Task 2).
- Produces:
  - `@dataclass(frozen=True) class Gap: path: str; kind: str; group_key: str | None`
  - `@dataclass(frozen=True) class GapReport: missing: list[Gap]; groups_to_rerender: list[str]; skipped: list[tuple[str, str]]; always_ask: list[str]`
  - `compute_gaps(schema_json: dict, known_values: dict, *, constraint_library: dict | None = None) -> GapReport`

- [ ] **Step 1: Write the failing tests**

```python
from vera_core.forms.gaps import compute_gaps, Gap

SCHEMA = {"sections": [{"section_key": "benefit_coverage", "properties": {
    "coverage_type": {"required_state": "required", "enum": ["Individual", "Family"]},
    "spouse_name": {"rules": [{"effect": "make this required", "match": "all of these",
        "conditions": [{"field": "coverage_type", "comparison": "is", "value": "Family"}]}]},
    "notes": {"type": "string"},  # optional, never a gap
}}]}

class TestComputeGaps:
    def test_missing_required_field_is_a_gap(self):
        report = compute_gaps(SCHEMA, {})
        paths = [g.path for g in report.missing]
        assert "benefit_coverage.coverage_type" in paths
        assert "benefit_coverage.notes" not in paths  # optional

    def test_conditional_required_fires_when_condition_holds(self):
        report = compute_gaps(SCHEMA, {"benefit_coverage": {"coverage_type": "Family"}})
        paths = [g.path for g in report.missing]
        assert "benefit_coverage.spouse_name" in paths

    def test_conditional_required_silent_when_condition_absent(self):
        report = compute_gaps(SCHEMA, {"benefit_coverage": {"coverage_type": "Individual"}})
        paths = [g.path for g in report.missing]
        assert "benefit_coverage.spouse_name" not in paths

    def test_invalid_value_is_a_gap(self):
        report = compute_gaps(SCHEMA, {"benefit_coverage": {"coverage_type": "Maybe"}})
        assert "benefit_coverage.coverage_type" in [g.path for g in report.missing]

    def test_full_valid_fill_returns_empty(self):
        report = compute_gaps(SCHEMA, {"benefit_coverage": {"coverage_type": "Individual"}})
        assert report.missing == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `just test tests/unit/forms/test_gaps.py -q`
Expected: FAIL — `ImportError: cannot import name 'compute_gaps'`.

- [ ] **Step 3: Implement `compute_gaps` (flat-field cases)**

```python
from dataclasses import dataclass, field as dc_field


@dataclass(frozen=True)
class Gap:
    path: str
    kind: str  # "field" | "group" | "gate"
    group_key: str | None = None


@dataclass(frozen=True)
class GapReport:
    missing: list[Gap] = dc_field(default_factory=list)
    groups_to_rerender: list[str] = dc_field(default_factory=list)
    skipped: list[tuple[str, str]] = dc_field(default_factory=list)
    always_ask: list[str] = dc_field(default_factory=list)


def _flatten(known: dict[str, Any]) -> dict[str, Any]:
    """Flatten nested known values to {leaf_key: value} — schema conditions
    reference fields by bare name, so the last path segment is the key."""
    from vera_core.forms.intake import iter_leaf_answers
    return {path.split(".")[-1]: value for path, value in iter_leaf_answers(known)}


def _is_required(fld: dict[str, Any], flat: dict[str, Any]) -> bool:
    if fld.get("required_state") == "required":
        return True
    for rule in fld.get("rules", []):
        if isinstance(rule, dict) and "required" in (rule.get("effect") or "").lower():
            if rule_matches(rule, flat):
                return True
    return False


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
            if not _is_required(fld, flat):
                continue
            if not is_filled(flat.get(name), fld, library):
                report.missing.append(Gap(path=f"{skey}.{name}", kind="field"))
    return report
```

Note: service-group (`all_or_nothing`) handling is added in Task 4; this task covers flat + conditional-required + validation, which the tests above exercise.

- [ ] **Step 4: Run tests to verify they pass**

Run: `just test tests/unit/forms/test_gaps.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/gaps.py tests/unit/forms/test_gaps.py
git commit -m "feat(forms): compute_gaps engine (flat + conditional-required)"
```

---

### Task 4: Service-group integrity + `always_ask`

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/forms/gaps.py`
- Test: `vera-backend/tests/unit/forms/test_gaps.py`

**Interfaces:**
- Consumes: everything from Task 3.
- Produces: `compute_gaps` now emits `kind="group"` gaps + `groups_to_rerender` for service objects where any child is missing, and populates `always_ask`.

- [ ] **Step 1: Write the failing tests**

```python
SERVICE_SCHEMA = {"sections": [{"section_key": "infertility", "properties": {
    "iui": {"category": "service", "required_state": "required", "sub_fields": [
        {"name": "covered", "enum": ["Yes", "No"], "required_state": "required"},
        {"name": "copay", "type": "string", "required_state": "required"}]},
    "call_reference_number": {"always_ask": True, "type": "string"},
}}]}

class TestServiceGroups:
    def test_partial_service_re_asks_whole_group(self):
        known = {"infertility": {"iui": {"covered": "Yes"}}}  # copay missing
        report = compute_gaps(SERVICE_SCHEMA, known)
        assert Gap("infertility.iui", "group", "iui") in report.missing
        assert "iui" in report.groups_to_rerender

    def test_complete_service_is_not_a_gap(self):
        known = {"infertility": {"iui": {"covered": "Yes", "copay": "$25"}}}
        report = compute_gaps(SERVICE_SCHEMA, known)
        assert report.groups_to_rerender == []

    def test_always_ask_field_is_surfaced_separately(self):
        report = compute_gaps(SERVICE_SCHEMA, {})
        assert "call_reference_number" in report.always_ask
        assert all(g.path != "infertility.call_reference_number" for g in report.missing)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `just test tests/unit/forms/test_gaps.py -q`
Expected: FAIL — group not emitted / `groups_to_rerender` empty.

- [ ] **Step 3: Extend `compute_gaps` with service handling**

Inside the field loop in `compute_gaps`, before the flat-leaf check, add:

```python
            if fld.get("category") == "service" or fld.get("sub_fields"):
                sub = flat.get(name) if isinstance(flat.get(name), dict) else \
                    (known_values.get(skey) or {}).get(name) or {}
                child_missing = [
                    sf.get("name") for sf in fld.get("sub_fields", [])
                    if isinstance(sf, dict) and _is_required(sf, flat)
                    and not is_filled(sub.get(sf.get("name")), sf, library)
                ]
                if child_missing:
                    report.groups_to_rerender.append(name)
                    report.missing.append(Gap(f"{skey}.{name}", "group", name))
                continue
```

(Default integrity is `all_or_nothing` for services — the whole group re-asks when any required child is missing.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `just test tests/unit/forms/test_gaps.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/gaps.py tests/unit/forms/test_gaps.py
git commit -m "feat(forms): service-group integrity + always_ask in compute_gaps"
```

---

### Task 5: `compose_gap_prompt` — the renderer

**Files:**
- Create: `vera-backend/packages/vera_core/src/vera_core/forms/gap_prompt.py`
- Test: `vera-backend/tests/unit/forms/test_gap_prompt.py`

**Interfaces:**
- Consumes: `GapReport`, `Gap` (Task 3).
- Produces: `compose_gap_prompt(schema_json: dict, report: GapReport, known_values: dict) -> str`

- [ ] **Step 1: Write the failing tests**

```python
from vera_core.forms.gaps import compute_gaps
from vera_core.forms.gap_prompt import compose_gap_prompt

SCHEMA = {"sections": [{"section_key": "benefit_coverage", "title": "Benefit Coverage",
    "properties": {
        "coverage_type": {"required_state": "required", "enum": ["Individual", "Family"],
                          "prompt": {"ask": "Is coverage individual or family?"}},
        "call_reference_number": {"always_ask": True, "prompt": {"ask": "Reference number?"}},
    }}]}

class TestComposeGapPrompt:
    def test_context_block_and_no_reintroduce(self):
        report = compute_gaps(SCHEMA, {})
        out = compose_gap_prompt(SCHEMA, report, {})
        assert "<gap_resolution_context>" in out
        assert "not re-introduce" in out.lower() or "do not re-introduce" in out.lower()

    def test_missing_field_is_rendered(self):
        report = compute_gaps(SCHEMA, {})
        out = compose_gap_prompt(SCHEMA, report, {})
        assert "Is coverage individual or family?" in out

    def test_always_ask_lands_in_closing_block(self):
        report = compute_gaps(SCHEMA, {})
        out = compose_gap_prompt(SCHEMA, report, {})
        assert "<closing_block>" in out and "Reference number?" in out

    def test_nothing_missing_returns_marker(self):
        report = compute_gaps(SCHEMA, {"benefit_coverage": {"coverage_type": "Individual"}})
        out = compose_gap_prompt(SCHEMA, report, {"benefit_coverage": {"coverage_type": "Individual"}})
        assert "no gap follow-up needed" in out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `just test tests/unit/forms/test_gap_prompt.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `compose_gap_prompt`**

```python
"""Render a follow-up ('gap resolution') prompt that re-asks only the gaps.
Pure; consumes a GapReport from vera_core.forms.gaps."""

from __future__ import annotations

from typing import Any

from vera_core.forms.gaps import GapReport


def _field_by_path(schema_json: dict[str, Any], path: str) -> dict[str, Any]:
    skey, _, name = path.partition(".")
    for section in schema_json.get("sections", []):
        if section.get("section_key") == skey:
            return (section.get("properties") or {}).get(name, {})
    return {}


def _ask_text(fld: dict[str, Any], name: str) -> str:
    prompt = fld.get("prompt") or {}
    return prompt.get("ask") or fld.get("description") or fld.get("title") or name


def _render_field(schema_json: dict[str, Any], path: str) -> str:
    fld = _field_by_path(schema_json, path)
    return f'<field name="{path}"><ask>{_ask_text(fld, path.split(".")[-1])}</ask></field>'


def compose_gap_prompt(
    schema_json: dict[str, Any], report: GapReport, known_values: dict[str, Any]
) -> str:
    if not report.missing and not report.always_ask:
        return "<gap_resolution_context>All required questions are answered. No gap follow-up needed.</gap_resolution_context>"

    lines = [
        "<gap_resolution_context>",
        "You are following up on a call where some questions were missed or unclear.",
        "Do not re-introduce yourself. Pick up naturally with the representative.",
        "</gap_resolution_context>",
        '<section mode="gap_resolution">',
    ]
    for gap in report.missing:
        lines.append(_render_field(schema_json, gap.path))
    lines.append("</section>")
    if report.always_ask:
        lines.append("<closing_block>")
        for name in report.always_ask:
            for section in schema_json.get("sections", []):
                if name in (section.get("properties") or {}):
                    lines.append(_render_field(schema_json, f'{section["section_key"]}.{name}'))
        lines.append("</closing_block>")
    return "\n".join(lines)
```

Note: the `<gap_resolution_context>` known-facts listing (already-confirmed values) is added when the write-back prereq lands; today `known_values` beyond intake is empty, so the block stays generic. This keeps raw stored values out of the prompt until the PHIBoundary gate is resolved.

- [ ] **Step 4: Run tests to verify they pass**

Run: `just test tests/unit/forms/test_gap_prompt.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/forms/gap_prompt.py tests/unit/forms/test_gap_prompt.py
git commit -m "feat(forms): compose_gap_prompt renderer"
```

---

### Task 6: `PersonaTweak.instructions_override` + worker honors it

**Files:**
- Modify: `vera-backend/packages/vera_core/src/vera_core/schemas` (the `PersonaTweak` model)
- Modify: `vera-backend/apps/agent_worker/src/agent_worker/prompt.py:92` (`build_instructions`)
- Test: `vera-backend/apps/agent_worker/tests/unit/test_prompt.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `PersonaTweak.instructions_override: str | None`; `build_instructions` returns the override verbatim (+ Cartesia guide) when set.

- [ ] **Step 1: Write the failing tests**

```python
# apps/agent_worker/tests/unit/test_prompt.py
from agent_worker.prompt import build_instructions
from vera_core.schemas import PersonaTweak

def test_instructions_override_replaces_base_prompt():
    out = build_instructions(PersonaTweak(instructions_override="<gap_resolution_context>X</gap_resolution_context>"))
    assert "<gap_resolution_context>X</gap_resolution_context>" in out
    assert "You are a voice bot verifying insurance" not in out  # base prompt NOT used

def test_no_override_uses_base_prompt():
    out = build_instructions(PersonaTweak())
    assert "You are a voice bot verifying insurance" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `just test apps/agent_worker/tests/unit/test_prompt.py -q`
Expected: FAIL — `PersonaTweak` has no `instructions_override`.

- [ ] **Step 3: Add the field + honor it**

In the `PersonaTweak` model, add:

```python
    instructions_override: str | None = None
```

In `agent_worker/prompt.py`, change `build_instructions`:

```python
def build_instructions(tweak: PersonaTweak | None = None) -> str:
    """Base persona (+ tenant extra instructions), or a full override (retry/gap
    mode), followed by the Cartesia readback guide."""
    if tweak is not None and tweak.instructions_override:
        return "\n\n".join([tweak.instructions_override, CARTESIA_MARKUP_GUIDE])
    parts = [SYSTEM_PROMPT]
    if tweak is not None and tweak.extra_instructions:
        parts.append(tweak.extra_instructions)
    parts.append(CARTESIA_MARKUP_GUIDE)
    return "\n\n".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `just test apps/agent_worker/tests/unit/test_prompt.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/schemas* apps/agent_worker/src/agent_worker/prompt.py apps/agent_worker/tests/unit/test_prompt.py
git commit -m "feat(worker): PersonaTweak.instructions_override for retry/gap prompt"
```

---

### Task 7: Trigger + delivery in the control plane

**Files:**
- Create: `vera-backend/apps/control_plane/src/control_plane/services/retry_prompt.py`
- Modify: `vera-backend/apps/control_plane/src/control_plane/api/v1/calls.py:74-81` (`start_call`, where the metadata dict is built)
- Test: `vera-backend/tests/integration/control_plane/test_retry_prompt.py`

**Interfaces:**
- Consumes: `compute_gaps`, `compose_gap_prompt`, `PersonaTweak`, the `Call` and `PatientForm` + `SchemaVersion` models.
- Produces: `async def gap_prompt_for_form(session, form) -> str | None` — returns the composed gap prompt when the form's most recent terminal call has a `call_reference_no`, else `None`.

- [ ] **Step 1: Write the failing integration test**

```python
# Seed a form + a terminal Call with call_reference_no set, then assert
# gap_prompt_for_form returns a "<gap_resolution_context>" string; with no
# reference number it returns None. (Runs against real Postgres + RLS.)
```

Write the seed using existing form/call fixtures in `tests/integration/control_plane/`; assert both branches of `gap_prompt_for_form`.

- [ ] **Step 2: Run test to verify it fails**

Run: `just test tests/integration/control_plane/test_retry_prompt.py -q`
Expected: FAIL — module/function not found.

- [ ] **Step 3: Implement `gap_prompt_for_form`**

```python
"""Retry/partial prompt: compose a gap-resolution prompt when a form is called
again after a prior call captured a call_reference_no. See design spec 2026-07-01."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.forms.gap_prompt import compose_gap_prompt
from vera_core.forms.gaps import compute_gaps
from vera_core.models import Call, PatientForm, SchemaVersion


async def gap_prompt_for_form(session: AsyncSession, form: PatientForm) -> str | None:
    last_call = (
        await session.execute(
            select(Call).where(Call.form_id == form.id).order_by(Call.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if last_call is None or not last_call.call_reference_no:
        return None
    schema_json = (
        await session.execute(
            select(SchemaVersion.schema_json).where(SchemaVersion.id == form.schema_version_id)
        )
    ).scalar_one()
    # known_values = intake_payload merged with prior-call answers ({} until write-back lands).
    known_values = dict(form.intake_payload or {})
    report = compute_gaps(schema_json, known_values)
    return compose_gap_prompt(schema_json, report, known_values)
```

- [ ] **Step 4: Wire it into `start_call`**

In `calls.py`, after loading `form` and before building `metadata` (around line 79), replace the persona construction with:

```python
    override = await gap_prompt_for_form(session, form)
    tweak = PersonaTweak.model_validate(persona) if persona is not None else PersonaTweak()
    if override is not None:
        tweak = tweak.model_copy(update={"instructions_override": override})
    metadata = tweak.model_dump(exclude_none=True)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `just test tests/integration/control_plane/test_retry_prompt.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/control_plane/src/control_plane/services/retry_prompt.py apps/control_plane/src/control_plane/api/v1/calls.py tests/integration/control_plane/test_retry_prompt.py
git commit -m "feat(calls): trigger + deliver gap-resolution prompt on retry"
```

---

### Task 8: Compliance note + full check

**Files:**
- Modify: `vera-backend/adr/devops-todo.md`

- [ ] **Step 1: Add the PHI-gating row**

Add a row: "Retry gap prompt embeds stored values (incl. PHI) into the system prompt + LiveKit dispatch metadata, bypassing the worker redact() seam. Gate real-PHI use behind the real PHIBoundary (or seed the session vault with the known values so tokens stay consistent). Synthetic/role-play data only until then."

- [ ] **Step 2: Run `/simplify` on the changed code, then the full gate**

Run `/simplify` (quality pass on the new modules), then:

Run: `just check`
Expected: ruff + mypy --strict + pytest all green.

- [ ] **Step 3: Commit**

```bash
git add adr/devops-todo.md
git commit -m "docs(adr): PHI gating note for retry gap prompt"
```

---

## Self-Review

- **Spec coverage:** §5 engine → Tasks 3–4; §6 validation → Tasks 1–2; §7 renderer → Task 5; §8 trigger/delivery → Tasks 6–7; §9 PHI → Task 8; §2 write-back prereq → `prior_call_answers`/`known_values` default `{}` throughout. Covered.
- **Placeholder scan:** every code step shows real code; the two "when write-back lands" notes are documented deferrals (spec §2/§13), not implementation gaps.
- **Type consistency:** `compute_gaps` / `Gap` / `GapReport` / `compose_gap_prompt` / `gap_prompt_for_form` / `PersonaTweak.instructions_override` names match across tasks.

## Notes for the reviewer / team

- **Ordering:** Tasks 1→5 are pure functions with zero dependencies on the rest of the app — reviewable and mergeable on their own. Tasks 6–8 wire them in.
- **Verify at implementation:** the exact `PersonaTweak` file path (`vera_core/schemas`), the `SchemaVersion.schema_json` column name, and the real `constraint_library` location in `ibv_form_standard.json` — confirm against the code before writing Task 3/7 (these are reads, not decisions).
- **End-to-end** only lights up once the call→DB write-back prerequisite lands (design §2).
